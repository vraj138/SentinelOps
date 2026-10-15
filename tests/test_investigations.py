"""Tests for automatic investigations: alert grouping, de-duplication, the concurrency limit,
the daily spend cap, the self-resolved case, restarts and the web controls (no model calls)."""

import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sentinelops.agent.investigator import InvestigationResult
from sentinelops.catalog import Catalog, Product
from sentinelops.controlplane.actions import ActionBackend
from sentinelops.controlplane.intake import AlertIntake
from sentinelops.controlplane.investigations import (
    InvestigationConflict,
    Investigations,
    SpendLedger,
    run_cost,
)
from sentinelops.controlplane.models import Alert, Incident, Inverse, State
from sentinelops.controlplane.service import ControlPlane
from sentinelops.controlplane.store import InMemoryStore
from sentinelops.controlplane.web import create_app
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile
from tests.test_controlplane import FakeRuntime, bad_payments_deploy, rca

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def stack(tmp_path: Path) -> tuple[DeployRepo, FlagFile, FakeRuntime]:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    repo.init()
    flags.init()
    return repo, flags, FakeRuntime()


class Inline:
    """An executor that runs each job at once, so tests are deterministic."""

    def __init__(self) -> None:
        self.deferred: list[tuple[Any, tuple]] = []
        self.defer = False

    def submit(self, fn, *args):
        future: Future = Future()
        if self.defer:
            self.deferred.append((fn, args))
            return future
        future.set_result(fn(*args))
        return future

    def run_deferred(self) -> None:
        jobs, self.deferred = self.deferred, []
        for fn, args in jobs:
            fn(*args)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


class FakeAgent:
    """Stands in for the model: records each run, writes one priced model call to the audit."""

    def __init__(self, plane: ControlPlane, kind: str = "rollback", cost: float = 0.12) -> None:
        self.plane = plane
        self.kind = kind
        self.cost = cost
        self.runs: list[tuple[str, float | None]] = []
        self.fail = False

    def __call__(self, incident: Incident, limit: float | None) -> InvestigationResult:
        self.runs.append((incident.id, limit))
        self.plane.audit(incident.id).record(
            "llm_call", model="fake", usage={"total": 100}, cost_usd=self.cost
        )
        if self.fail:
            raise RuntimeError("model unavailable")
        service = "payments" if self.kind != "escalate" else None
        return InvestigationResult(status="rca", rca=rca(self.kind, service), totals={})


def make_plane(tmp_path: Path, stack, catalog: Catalog | None = None) -> ControlPlane:
    repo, flags, runtime = stack
    bad_payments_deploy(repo)
    actions = ActionBackend(repo, flags, runtime)
    return ControlPlane(InMemoryStore(), actions, None, tmp_path / "runs", catalog)  # type: ignore[arg-type]


def make_investigations(
    plane: ControlPlane,
    agent: FakeAgent,
    tmp_path: Path,
    firing: bool | None = True,
    executor: Any = None,
    **kw: Any,
) -> Investigations:
    return Investigations(
        plane,
        agent,
        lambda incident: firing,
        SpendLedger(tmp_path / "spend.jsonl", now=lambda: NOW),
        executor=executor or Inline(),
        now=lambda: NOW,
        **kw,
    )


def alert(service: str, minutes: float = 0, name: str = "HighErrorRate") -> Alert:
    return Alert(name=name, service=service, since=NOW + timedelta(minutes=minutes))


# grouping


def test_related_alerts_become_one_incident_and_one_investigation(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path)
    alerts = [alert("gateway", 1), alert("orders", 0.5), alert("payments")]
    intake = AlertIntake(plane, lambda: ("ctx", alerts), now=lambda: NOW, on_open=runs.on_opened)

    opened = intake.tick()

    assert len(opened) == 1 and len(agent.runs) == 1
    incident = plane.store.get(opened[0])
    assert {a.service for a in incident.alerts} == {"gateway", "orders", "payments"}
    assert intake.tick() == [] and len(agent.runs) == 1  # still firing: nothing new


def test_late_related_alert_joins_the_open_incident(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    firing = [alert("payments")]
    intake = AlertIntake(plane, lambda: ("ctx", firing), now=lambda: NOW)
    [incident_id] = intake.tick()

    firing.append(alert("gateway", 2))
    assert intake.tick() == []

    incident = plane.store.get(incident_id)
    assert {a.service for a in incident.alerts} == {"payments", "gateway"}
    assert "gateway" in incident.alert_context  # not investigated yet: its input is updated
    assert incident.impacted_products == ["edge", "payments"]
    audit = (tmp_path / "runs" / incident_id / "audit.jsonl").read_text()
    assert '"alerts_attached"' in audit


def test_related_alert_outside_the_window_opens_a_new_incident(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    firing = [alert("payments")]
    intake = AlertIntake(plane, lambda: ("ctx", firing), now=lambda: NOW)
    intake.tick()

    firing.append(alert("gateway", 20))

    assert len(intake.tick()) == 1


def test_unrelated_services_become_separate_incidents(tmp_path: Path, stack) -> None:
    catalog = Catalog(
        products=[
            Product(id="a", name="A", team="team-a", services=["payments"]),
            Product(id="b", name="B", team="team-b", services=["gateway"]),
        ]
    )
    plane = make_plane(tmp_path, stack, catalog)
    intake = AlertIntake(plane, lambda: ("ctx", [alert("payments"), alert("gateway")]))

    assert len(intake.tick()) == 2


# de-duplication


def test_an_incident_is_never_investigated_twice_automatically(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path)
    incident = plane.open("ctx", [alert("payments")])

    runs.on_opened(incident)
    runs.on_opened(plane.store.get(incident.id))

    assert len(agent.runs) == 1
    assert plane.store.get(incident.id).state == State.PROPOSED


def test_alerts_an_investigation_could_not_fix_are_not_rerun_for_a_day(
    tmp_path: Path,
    stack,
) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane, kind="escalate")
    runs = make_investigations(plane, agent, tmp_path)
    first = plane.open("ctx", [alert("orders", name="HighMemoryUsage")])
    runs.on_opened(first)
    assert plane.store.get(first.id).state == State.ESCALATED

    second = plane.open("ctx", [alert("orders", name="HighMemoryUsage")])
    runs.on_opened(second)

    waiting = plane.store.get(second.id).investigation
    assert len(agent.runs) == 1 and waiting.status == "waiting" and first.id in (waiting.note or "")
    runs.start_manual(second.id)  # a person can still ask
    assert len(agent.runs) == 2


# concurrency


def test_at_most_n_investigations_run_at_once(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    release = threading.Event()
    lock = threading.Lock()
    live = {"now": 0, "max": 0}
    agent = FakeAgent(plane, kind="escalate")

    def slow(incident: Incident, limit: float | None) -> InvestigationResult:
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        release.wait(5)
        with lock:
            live["now"] -= 1
        return agent(incident, limit)

    runs = Investigations(
        plane,
        slow,
        lambda i: True,
        SpendLedger(tmp_path / "spend.jsonl"),
        concurrency=2,
        daily_budget_usd=100,
    )
    incidents = [plane.open("ctx", [alert(s)]) for s in ("gateway", "orders", "payments")]
    for incident in incidents:
        runs.on_opened(incident)
    deadline = time.monotonic() + 5
    while runs.status()["running"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    states = [plane.store.get(i.id) for i in incidents]
    assert runs.status() == {**runs.status(), "running": 2, "queued": 1}
    assert sum(s.state == State.OPEN and s.investigation.status == "queued" for s in states) == 1

    release.set()
    while runs.status()["running"] + runs.status()["queued"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert live["max"] == 2
    assert all(plane.store.get(i.id).state == State.ESCALATED for i in incidents)
    runs.shutdown()


# spend cap


def test_spend_cap_makes_incidents_wait_for_a_manual_start(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path, daily_budget_usd=1.0)
    runs.ledger.record("INC-earlier", "auto", 1.0)
    incident = plane.open("ctx", [alert("payments")])

    runs.on_opened(incident)

    info = plane.store.get(incident.id).investigation
    assert agent.runs == [] and info.status == "waiting" and "budget" in (info.note or "")
    page = TestClient(create_app(plane, tmp_path / "x", investigations=runs))
    html = page.get(f"/incidents/{incident.id}").text
    assert "Waiting for a manual start" in html and "used up" in html
    assert "Start investigation" in html

    runs.start_manual(incident.id)  # manual runs aren't capped
    assert agent.runs == [(incident.id, None)]
    assert runs.ledger.spent_today() == pytest.approx(1.0)  # manual spend isn't counted


def test_automatic_runs_are_capped_by_what_is_left_of_the_budget(
    tmp_path: Path,
    stack,
) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path, daily_budget_usd=2.0, concurrency=2)

    runs.on_opened(plane.open("ctx", [alert("payments")]))
    runs.ledger.record("INC-earlier", "auto", 1.8)
    runs.on_opened(plane.open("ctx", [alert("orders", name="HighLatencyP95")]))

    assert [limit for _, limit in agent.runs] == [1.0, pytest.approx(0.08)]
    assert runs.ledger.spent_today() == pytest.approx(0.12 + 1.8 + 0.12)


def test_a_failed_run_is_escalated_and_its_cost_still_counts(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    agent.fail = True
    runs = make_investigations(plane, agent, tmp_path)
    incident = plane.open("ctx", [alert("payments")])

    runs.on_opened(incident)

    done = plane.store.get(incident.id)
    assert done.state == State.ESCALATED and done.investigation.status == "failed"
    assert "model unavailable" in (done.escalation_reason or "")
    assert runs.ledger.spent_today() == pytest.approx(0.12)
    assert run_cost(plane.runs_dir, incident.id) == pytest.approx(0.12)


# self-resolved


@pytest.mark.parametrize(
    ("firing", "state"),
    [(False, State.SELF_RESOLVED), (True, State.PROPOSED), (None, State.PROPOSED)],
)
def test_alert_that_cleared_during_the_investigation_self_resolves(
    tmp_path: Path,
    stack,
    firing: bool | None,
    state: State,
) -> None:
    plane = make_plane(tmp_path, stack)
    runs = make_investigations(plane, FakeAgent(plane), tmp_path, firing=firing)
    incident = plane.open("ctx", [alert("payments")])

    runs.on_opened(incident)

    done = plane.store.get(incident.id)
    assert done.state == state and done.rca is not None
    if state == State.SELF_RESOLVED:
        assert done.proposal is None and done.action is None


# audit and measurement


def test_audit_records_the_trigger_and_alert_to_rca_time(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    runs = make_investigations(plane, FakeAgent(plane), tmp_path)
    auto = plane.open("ctx", [alert("payments")])
    runs.on_opened(auto)
    manual = plane.open("ctx", [alert("orders", name="HighLatencyP95")])
    plane.set_investigation(manual.id, State.OPEN, "investigation_waiting", status="waiting")
    runs.start_manual(manual.id)

    for incident, trigger in ((auto, "auto"), (manual, "manual")):
        records = plane.audit(incident.id)
        lines = (records.path).read_text()
        assert f'"investigation_started", "trigger": "{trigger}"' in lines
        assert '"investigation_finished"' in lines and '"alert_to_rca_seconds"' in lines
        assert plane.store.get(incident.id).investigation.alert_to_rca_seconds is not None


# restarts, re-investigation and races


def test_restart_requeues_queued_and_escalates_interrupted_runs(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    agent = FakeAgent(plane)
    queued = plane.open("ctx", [alert("payments")])
    plane.set_investigation(queued.id, State.OPEN, "q", status="queued", trigger="auto")
    cut_off = plane.open("ctx", [alert("orders", name="HighLatencyP95")])
    plane.start_investigation(cut_off.id, "auto")
    untouched = plane.open("ctx", [alert("gateway", name="HighLatencyP95")])

    make_investigations(plane, agent, tmp_path).recover()

    assert [incident_id for incident_id, _ in agent.runs] == [queued.id]
    assert plane.store.get(cut_off.id).state == State.ESCALATED
    assert plane.store.get(untouched.id).state == State.OPEN  # never auto-started by a restart


def test_reinvestigate_clears_the_old_proposal_first(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    executor = Inline()
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path, executor=executor)
    incident = plane.open("ctx", [alert("payments")])
    runs.on_opened(incident)
    assert plane.store.get(incident.id).state == State.PROPOSED

    executor.defer = True
    runs.start_manual(incident.id)
    waiting = plane.store.get(incident.id)
    assert waiting.state == State.OPEN and waiting.action is None  # can't be approved meanwhile
    with pytest.raises(InvestigationConflict):
        runs.start_manual(incident.id)  # already queued

    executor.run_deferred()
    again = plane.store.get(incident.id)
    assert again.state == State.PROPOSED and again.investigation.runs == 2


def test_no_reinvestigation_after_an_action_ran(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    runs = make_investigations(plane, FakeAgent(plane), tmp_path)
    incident = plane.open("ctx", [alert("payments")])
    plane.escalate(incident.id, State.OPEN, "rolled back", inverse=Inverse(kind="none", target="x"))

    with pytest.raises(InvestigationConflict):
        runs.start_manual(incident.id)


def test_a_queued_run_is_skipped_if_the_incident_moved_on(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    executor = Inline()
    executor.defer = True
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path, executor=executor)
    incident = plane.open("ctx", [alert("payments")])
    runs.on_opened(incident)

    plane.escalate(incident.id, State.OPEN, "handled by hand")
    executor.run_deferred()

    done = plane.store.get(incident.id)
    assert agent.runs == [] and done.state == State.ESCALATED
    assert done.escalation_reason == "handled by hand" and "not started" in (
        done.investigation.note or ""
    )


# web


def test_web_shows_status_and_guards_manual_starts(tmp_path: Path, stack) -> None:
    plane = make_plane(tmp_path, stack)
    executor = Inline()
    executor.defer = True
    agent = FakeAgent(plane)
    runs = make_investigations(plane, agent, tmp_path, executor=executor, auto=False)
    incident = plane.open("ctx", [alert("payments")])
    runs.on_opened(incident)
    client = TestClient(create_app(plane, tmp_path / "x", investigations=runs))

    page = client.get(f"/incidents/{incident.id}").text
    assert "AUTO_INVESTIGATE=false" in page and "Start investigation" in page
    assert "Investigate with the agent" not in page

    forged = client.post(f"/incidents/{incident.id}/investigate", data={"token": "guess"})
    assert forged.status_code == 409 and executor.deferred == []
    client.post(f"/incidents/{incident.id}/investigate", data={"token": incident.investigate_token})
    assert "Queued for investigation" in client.get(f"/incidents/{incident.id}").text
    again = client.post(
        f"/incidents/{incident.id}/investigate", data={"token": incident.investigate_token}
    )
    assert again.status_code == 409  # one investigation at a time

    executor.run_deferred()
    done = client.get(f"/incidents/{incident.id}").text
    assert "Re-investigate" in done and "<dd>manual</dd>" in done

    no_agent = TestClient(create_app(plane, tmp_path / "x", investigations=None))
    other = plane.open("ctx", [alert("orders", name="HighLatencyP95")])
    resp = no_agent.post(
        f"/incidents/{other.id}/investigate", data={"token": other.investigate_token}
    )
    assert resp.status_code == 409
