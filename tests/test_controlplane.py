"""Tests for the control plane: actions, state machine, verification, web UI (no Docker)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from faults import registry
from faults.environment import Environment
from faults.provider import ProviderFile
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.agent.models import RootCauseAnalysis
from sentinelops.controlplane.actions import ActionBackend, ActionError
from sentinelops.controlplane.models import Alert, Incident, ProposedAction, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import InMemoryStore, StaleTransition
from sentinelops.controlplane.verify import Verifier, scope
from sentinelops.controlplane.web import create_app
from sentinelops.stack.deploy_repo import BASE_RELEASES, DeployRepo, DeployRepoError
from sentinelops.stack.flags import FlagFile


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def recreate(self, service: str) -> None:
        self.calls.append(("recreate", service))

    def restart(self, service: str) -> None:
        self.calls.append(("restart", service))


class FakeMetrics:
    """Answers verification queries: every service healthy unless listed in `failing`."""

    def __init__(self, failing: set[str] = frozenset()) -> None:
        self.failing = set(failing)

    def query(self, promql: str) -> list[dict]:
        service = next(s for s in ("gateway", "orders", "payments") if f'"{s}"' in promql)
        if promql.startswith("up"):
            value = "1"
        elif "5.." in promql:
            value = "0.4" if service in self.failing else "0.0"
        else:
            value = "0.1"
        return [{"metric": {}, "value": [0, value]}]


@pytest.fixture
def stack(tmp_path: Path) -> tuple[DeployRepo, FlagFile, FakeRuntime]:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    repo.init()
    flags.init()
    return repo, flags, FakeRuntime()


def plane(tmp_path: Path, stack, metrics: FakeMetrics | None = None) -> ControlPlane:
    repo, flags, runtime = stack
    verifier = Verifier(
        metrics or FakeMetrics(), settle_seconds=0, deadline_seconds=0, sleep=lambda s: None
    )
    return ControlPlane(
        InMemoryStore(), ActionBackend(repo, flags, runtime), verifier, tmp_path / "runs"
    )


def proposal(kind: str = "rollback", service: str = "payments", **kw: Any) -> ProposedAction:
    return ProposedAction(kind=kind, service=service, reason="test", proposed_by="operator", **kw)


def bad_payments_deploy(repo: DeployRepo) -> str:
    return repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "1.0"}, "Enable fraud checks")


def open_proposed(cp: ControlPlane, action: ProposedAction, alerts=("payments",)) -> Incident:
    incident = cp.open("HighErrorRate", [Alert(name="HighErrorRate", service=s) for s in alerts])
    return cp.propose(incident.id, action)


# deploy repo additions


def test_last_change_and_write_release(stack) -> None:
    repo, _, _ = stack
    assert repo.last_change("payments") is None
    sha = bad_payments_deploy(repo)

    change = repo.last_change("payments")

    assert change is not None and change.sha == sha
    assert change.before == BASE_RELEASES["payments"]
    assert repo.write_release("payments", repo.read("payments"), "noop") is None


def test_failed_revert_is_aborted_cleanly(stack) -> None:
    repo, _, _ = stack
    first = bad_payments_deploy(repo)
    repo.write_release("payments", dict(BASE_RELEASES["payments"]), "Rollback payments")
    repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "0.5"}, "Tune")

    with pytest.raises(DeployRepoError):
        repo.revert(first)

    assert not (repo.path / ".git" / "REVERT_HEAD").exists()
    assert "<<<<<<<" not in repo.env_file("payments").read_text()


# actions


def test_rollback_dry_run_shows_exact_change_with_secrets_redacted(stack, tmp_path: Path) -> None:
    repo, flags, runtime = stack
    repo.deploy("payments", {"PAYMENTS_API_KEY": "new-secret"}, "Rotate key")

    resolved = ActionBackend(repo, flags, runtime).resolve(proposal())

    assert "Rotate key" in resolved.dry_run
    assert "1.9.1 -> 1.9.0" in resolved.dry_run
    assert "PAYMENTS_API_KEY=[redacted]" in resolved.dry_run
    assert "new-secret" not in resolved.dry_run and "demo-provider-key" not in resolved.dry_run


def test_rollback_refuses_to_undo_an_undo(stack) -> None:
    repo, flags, runtime = stack
    bad_payments_deploy(repo)
    repo.write_release("payments", dict(BASE_RELEASES["payments"]), "Restore baseline release")

    with pytest.raises(ActionError, match="already an undo"):
        ActionBackend(repo, flags, runtime).resolve(proposal())


def test_rollback_without_deploys_refuses(stack) -> None:
    with pytest.raises(ActionError, match="no deploy to roll back"):
        ActionBackend(*stack).resolve(proposal())


def test_unknown_service_or_flag_refused(stack) -> None:
    backend = ActionBackend(*stack)
    with pytest.raises(ActionError):
        backend.resolve(proposal(service="postgres"))
    with pytest.raises(ActionError, match="unknown feature flag"):
        backend.resolve(proposal(kind="set_flag", service=None, flag="nope", flag_value=False))


# the full lifecycle


def test_rollback_resolves_incident(tmp_path: Path, stack) -> None:
    repo, _, runtime = stack
    bad_payments_deploy(repo)
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())

    cp.approve(incident.id, "vraj")
    done = cp.execute(incident.id)

    assert done.state == State.RESOLVED
    assert repo.read("payments") == BASE_RELEASES["payments"]
    assert runtime.calls == [("recreate", "payments")]
    assert repo.last_change("payments").message.startswith("Rollback payments to 1.9.0")  # type: ignore[union-attr]
    states = [h["to"] for h in done.history]
    assert states == ["PROPOSED", "APPROVED", "EXECUTING", "VERIFYING", "RESOLVED"]


def test_wrong_action_fails_verification_and_is_rolled_back(tmp_path: Path, stack) -> None:
    repo, _, runtime = stack
    bad_payments_deploy(repo)
    repo.deploy("gateway", {}, "Upgrade HTTP client libraries")
    gateway_before = repo.read("gateway")
    cp = plane(tmp_path, stack, FakeMetrics(failing={"payments", "orders", "gateway"}))
    incident = open_proposed(
        cp, proposal(service="gateway"), alerts=("payments", "orders", "gateway")
    )

    cp.approve(incident.id, "vraj")
    done = cp.execute(incident.id)

    assert done.state == State.ESCALATED and done.rolled_back
    assert "verification failed" in (done.escalation_reason or "")
    assert repo.read("gateway") == gateway_before
    assert runtime.calls == [("recreate", "gateway"), ("recreate", "gateway")]


def test_stale_approval_is_refused(tmp_path: Path, stack) -> None:
    repo, _, runtime = stack
    bad_payments_deploy(repo)
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")
    repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "0.5"}, "Someone else deploys")

    done = cp.execute(incident.id)

    assert done.state == State.ESCALATED
    assert "changed since approval" in (done.escalation_reason or "")
    assert runtime.calls == []


def test_set_flag_and_its_undo(tmp_path: Path, stack) -> None:
    _, flags, _ = stack
    flags.set("checkout_v2_pricing", True, "growth")
    cp = plane(tmp_path, stack, FakeMetrics(failing={"gateway"}))
    action = proposal(kind="set_flag", service=None, flag="checkout_v2_pricing", flag_value=False)
    incident = open_proposed(cp, action, alerts=("gateway",))

    cp.approve(incident.id, "vraj")
    done = cp.execute(incident.id)

    assert done.state == State.ESCALATED and done.rolled_back
    assert flags.read()["checkout_v2_pricing"] is True


def test_execute_twice_is_impossible(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")
    cp.execute(incident.id)

    with pytest.raises(StaleTransition):
        cp.execute(incident.id)


def test_cannot_execute_without_approval(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())

    with pytest.raises(StaleTransition):
        cp.execute(incident.id)


def test_reject_escalates(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())

    done = cp.reject(incident.id, "vraj", "wrong service")

    assert done.state == State.ESCALATED and "wrong service" in (done.escalation_reason or "")


def rca(kind: str, service: str | None) -> RootCauseAnalysis:
    return RootCauseAnalysis.model_validate(
        {
            "summary": "s",
            "root_cause_service": "payments",
            "root_cause": "r",
            "claims": [{"statement": "x", "evidence": ["T1"]}],
            "confidence": "high",
            "recommended_action": {
                "kind": kind,
                "service": service,
                "flag": None,
                "flag_value": None,
                "reason": "because",
            },
            "data_gaps": [],
        }
    )


def test_agent_result_becomes_a_proposal(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = cp.open("ctx", [Alert(name="HighErrorRate", service="payments")])
    cp.start_investigation(incident.id)

    result = InvestigationResult(status="rca", rca=rca("rollback", "payments"), totals={})
    done = cp.record_investigation(incident.id, result)

    assert done.state == State.PROPOSED and done.proposal.proposed_by == "agent"  # type: ignore[union-attr]


def test_agent_escalation_is_respected(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    incident = cp.open("ctx", [])
    cp.start_investigation(incident.id)

    done = cp.record_investigation(
        incident.id, InvestigationResult(status="rca", rca=rca("escalate", None), totals={})
    )

    assert done.state == State.ESCALATED and done.rca is not None


def test_audit_log_records_every_step(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")
    cp.execute(incident.id)

    lines = (tmp_path / "runs" / incident.id / "audit.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    for kind in (
        "incident_opened",
        "action_proposed",
        "approved",
        "action_executed",
        "verification_round",
    ):
        assert kind in kinds


def test_verification_scope_includes_alerting_services_and_target() -> None:
    incident = Incident(
        id="i", alert_context="", alerts=[Alert(name="HighErrorRate", service="orders")]
    )
    assert scope(incident) == ["orders"]


# reset after a remediation


def test_reset_restores_baseline_after_a_remediation(tmp_path: Path) -> None:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    provider = ProviderFile(tmp_path / "provider.json")
    for f in (repo, flags, provider):
        f.init()
    runtime = FakeRuntime()
    env = Environment(
        repo, flags, provider, runtime, tmp_path / "active.json", sleep=lambda s: None
    )
    env.inject(registry.get("bad_deploy/a"), log=lambda m: None)
    cp = ControlPlane(
        InMemoryStore(),
        ActionBackend(repo, flags, runtime),
        Verifier(FakeMetrics(), settle_seconds=0, deadline_seconds=0, sleep=lambda s: None),
        tmp_path / "runs",
    )
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")
    assert cp.execute(incident.id).state == State.RESOLVED

    env.reset(log=lambda m: None)

    assert repo.read("payments") == BASE_RELEASES["payments"]
    assert repo._git("status", "--porcelain") == ""
    assert env.active() is None


# web UI


def test_web_review_and_approve(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    client = TestClient(create_app(cp))

    page = client.get(f"/incidents/{incident.id}").text
    assert "Approve and execute" in page and "Enable fraud checks" in page

    response = client.post(
        f"/incidents/{incident.id}/approve",
        data={"approver": "vraj", "fingerprint": incident.action.fingerprint},  # type: ignore[union-attr]
    )

    assert response.status_code == 200
    assert cp.store.get(incident.id).state == State.RESOLVED
    assert "payload" not in client.get(f"/api/incidents/{incident.id}").json()["action"]


def test_web_escapes_untrusted_text(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    incident = cp.open("<script>alert(1)</script>", [])

    page = TestClient(create_app(cp)).get(f"/incidents/{incident.id}").text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_web_double_approval_conflicts(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    client = TestClient(create_app(cp))
    form = {"approver": "vraj", "fingerprint": incident.action.fingerprint}  # type: ignore[union-attr]
    client.post(f"/incidents/{incident.id}/approve", data=form)

    response = client.post(f"/incidents/{incident.id}/approve", data=form)

    assert response.status_code == 409


def test_error_ratio_treats_missing_5xx_series_as_zero_errors() -> None:
    """A restarted service has no 5xx series yet; that is zero errors, not 'no traffic'."""
    queries: list[str] = []

    class Metrics:
        def query(self, promql: str) -> list[dict]:
            queries.append(promql)
            return [{"metric": {}, "value": [0, "0"]}]

    incident = Incident(
        id="i", alert_context="", alerts=[Alert(name="HighErrorRate", service="payments")]
    )
    Verifier(Metrics()).round(incident)

    ratio = next(q for q in queries if "5.." in q)
    assert "or vector(0)" in ratio


def test_web_approval_needs_the_fingerprint_shown_on_the_page(tmp_path: Path, stack) -> None:
    """A forged cross-site POST can't know the fingerprint, so it can't approve."""
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    client = TestClient(create_app(cp))

    response = client.post(
        f"/incidents/{incident.id}/approve", data={"approver": "x", "fingerprint": "guess"}
    )

    assert response.status_code == 409
    assert cp.store.get(incident.id).state == State.PROPOSED


def test_secrets_never_leave_through_api_or_audit(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")
    cp.execute(incident.id)

    api = TestClient(create_app(cp)).get(f"/api/incidents/{incident.id}").text
    audit = (tmp_path / "runs" / incident.id / "audit.jsonl").read_text()

    assert "demo-provider-key" not in api
    assert "demo-provider-key" not in audit
    assert (
        cp.store.get(incident.id).inverse.data["settings"]["PAYMENTS_API_KEY"]
        == "demo-provider-key"
    )  # type: ignore[union-attr]


def test_crash_during_verification_undoes_the_action(tmp_path: Path, stack) -> None:
    repo, flags, runtime = stack
    bad_payments_deploy(repo)
    bad_release = repo.read("payments")

    class Broken:
        def query(self, promql: str) -> list[dict]:
            raise RuntimeError("unexpected")

    verifier = Verifier(Broken(), settle_seconds=0, deadline_seconds=0, sleep=lambda s: None)
    cp = ControlPlane(
        InMemoryStore(), ActionBackend(repo, flags, runtime), verifier, tmp_path / "runs"
    )
    incident = open_proposed(cp, proposal())
    cp.approve(incident.id, "vraj")

    done = cp.execute(incident.id)

    assert done.state == State.ESCALATED and done.rolled_back
    assert "could not run" in (done.escalation_reason or "")
    assert repo.read("payments") == bad_release


# dashboard


def write_agent_audit(path: Path, cost: float | None = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"ts": "2026-10-13T20:00:00.000+00:00", "kind": "run_start"},
        {
            "ts": "2026-10-13T20:00:01.000+00:00",
            "kind": "llm_call",
            "model": "claude-opus-5-5",
            "usage": {"total": 1200},
            "cost_usd": cost,
        },
        {
            "ts": "2026-10-13T20:00:02.000+00:00",
            "kind": "tool_call",
            "id": "T1",
            "tool": "list_alerts",
            "is_error": True,
            "result": "Prometheus is unreachable",
            "arguments": {},
        },
        {"ts": "2026-10-13T20:00:42.000+00:00", "kind": "run_end"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_incident_page_shows_investigation_cost(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    incident = cp.open("ctx", [])
    write_agent_audit(tmp_path / "runs" / incident.id / "audit.jsonl")
    client = TestClient(create_app(cp, results_path=tmp_path / "none.jsonl"))

    page = client.get(f"/incidents/{incident.id}").text
    index = client.get("/incidents").text

    assert "Investigation" in page and "1,200" in page and "$0.250" in page and "42s" in page
    assert "(1 failed)" in page
    assert "$0.250" in index


def test_unpriced_model_cost_is_shown_as_a_lower_bound(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    incident = cp.open("ctx", [])
    write_agent_audit(tmp_path / "runs" / incident.id / "audit.jsonl", cost=None)

    page = (
        TestClient(create_app(cp, results_path=tmp_path / "x"))
        .get(f"/incidents/{incident.id}")
        .text
    )

    assert "&ge;$0.000" in page


def test_benchmark_page_empty_and_with_results(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    results = tmp_path / "results.jsonl"
    client = TestClient(create_app(cp, results_path=results))
    assert "No benchmark results yet" in client.get("/benchmark").text

    record = {
        "run_id": "bad_deploy-c-r1",
        "scenario_id": "bad_deploy/c",
        "system": "sentinelops",
        "manifested": True,
        "status": "rca",
        "seconds_to_answer": 90,
        "totals": {"cost_usd": 0.4, "tool_calls": 9},
        "score": {
            "root_cause_correct": True,
            "action": "full",
            "harmful": False,
            "escalation_correct": True,
            "escalated": False,
            "claims": 3,
            "citations": 7,
        },
    }
    results.write_text(json.dumps(record) + "\n")

    page = client.get("/benchmark").text
    assert "correct action" in page and "100% (1/1)" in page
    assert "bad_deploy/c" in page and "<td>full</td>" in page


# fleet, routing and intake


class HealthMetrics:
    """Prometheus stand-in: payments is failing, everything else healthy."""

    def query(self, promql: str) -> list[dict]:
        if promql.startswith("up"):
            return [{"metric": {}, "value": [0, "1"]}]
        if "5.." in promql:
            return [{"metric": {}, "value": [0, "0.3" if '"payments"' in promql else "0"]}]
        return [{"metric": {}, "value": [0, "0.05"]}]


class DownMetrics:
    def query(self, promql: str) -> list[dict]:
        from mcp_servers.common import ToolError

        raise ToolError("Prometheus is unreachable")


def test_open_routes_to_likely_owner_and_lists_impacted(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    alerts = [Alert(name="HighErrorRate", service=s) for s in ("gateway", "orders")]

    incident = cp.open("ctx", alerts)

    assert (incident.product, incident.owner_source) == ("orders", "alerts")
    assert incident.impacted_products == ["edge", "orders"]


def test_investigation_reroutes_to_the_root_cause_owner(tmp_path: Path, stack) -> None:
    bad_payments_deploy(stack[0])
    cp = plane(tmp_path, stack)
    alerts = [Alert(name="HighErrorRate", service=s) for s in ("gateway", "orders")]
    incident = cp.open("ctx", alerts)
    cp.start_investigation(incident.id)

    done = cp.record_investigation(
        incident.id, InvestigationResult(status="rca", rca=rca("rollback", "payments"), totals={})
    )

    assert (done.product, done.owner_source) == ("payments", "investigation")
    lines = (tmp_path / "runs" / incident.id / "audit.jsonl").read_text()
    assert '"rerouted"' in lines and '"to_product": "payments"' in lines


def test_fleet_shows_live_health_per_product(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    page = (
        TestClient(create_app(cp, results_path=tmp_path / "x", metrics=HealthMetrics()))
        .get("/")
        .text
    )

    assert "Payments" in page and "payments-team" in page
    assert "degraded" in page and "30.0%" in page and "healthy" in page


def test_fleet_says_metrics_unavailable_instead_of_crashing(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    page = (
        TestClient(create_app(cp, results_path=tmp_path / "x", metrics=DownMetrics())).get("/").text
    )

    assert "metrics unavailable" in page


def test_incident_list_filters_by_product(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    a = cp.open("ctx", [Alert(name="HighErrorRate", service="payments")])
    b = cp.open("ctx", [Alert(name="HighErrorRate", service="gateway")])
    client = TestClient(create_app(cp, results_path=tmp_path / "x"))

    page = client.get("/incidents?product=payments").text

    assert a.id in page and b.id not in page
    assert client.get("/products/payments").status_code == 200
    assert client.get("/products/nope").status_code == 404


def test_investigate_button_needs_token_and_credentials(tmp_path: Path, stack) -> None:
    cp = plane(tmp_path, stack)
    incident = cp.open("ctx", [Alert(name="HighErrorRate", service="payments")])
    started: list[str] = []
    client = TestClient(create_app(cp, results_path=tmp_path / "x", investigate=started.append))

    assert "Investigate with the agent" in client.get(f"/incidents/{incident.id}").text
    forged = client.post(f"/incidents/{incident.id}/investigate", data={"token": "guess"})
    assert forged.status_code == 409 and started == []

    client.post(f"/incidents/{incident.id}/investigate", data={"token": incident.investigate_token})
    assert started == [incident.id]
    assert cp.store.get(incident.id).state == State.INVESTIGATING
    again = client.post(
        f"/incidents/{incident.id}/investigate", data={"token": incident.investigate_token}
    )
    assert again.status_code == 409 and started == [incident.id]

    no_key = TestClient(create_app(cp, results_path=tmp_path / "x", investigate=None))
    other = cp.open("ctx", [Alert(name="HighErrorRate", service="orders")])
    resp = no_key.post(
        f"/incidents/{other.id}/investigate", data={"token": other.investigate_token}
    )
    assert resp.status_code == 409


def test_intake_opens_once_and_respects_cooldown(tmp_path: Path, stack) -> None:
    from datetime import timedelta

    from sentinelops.controlplane.intake import AlertIntake

    cp = plane(tmp_path, stack)
    alerts = [Alert(name="HighErrorRate", service="payments")]
    clock = [datetime.now(UTC)]
    intake = AlertIntake(cp, lambda: ("ctx", alerts), now=lambda: clock[0])

    first = intake.tick()
    assert first is not None and intake.tick() is None  # still open: no duplicate

    cp.escalate(first, State.OPEN, "handled")
    assert intake.tick() is None  # just closed: within cooldown
    clock[0] += timedelta(minutes=11)
    assert intake.tick() is not None  # alerts still firing long after: new incident
    assert AlertIntake(cp, lambda: ("ctx", [])).tick() is None  # nothing firing


def test_old_incidents_without_routing_fields_still_load(tmp_path: Path, stack) -> None:
    legacy = Incident(id="INC-old", alert_context="ctx").model_dump(mode="json")
    for field in ("product", "owner_source", "impacted_products", "investigate_token"):
        legacy.pop(field)

    loaded = Incident.model_validate(legacy)

    assert loaded.product is None and loaded.impacted_products == []
