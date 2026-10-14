"""Tests for the benchmark: scoring, report, replay, judge, clean slate, runner, v1 export."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from faults import registry
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.agent.models import RootCauseAnalysis
from sentinelops.evals import report, runner, v1_baseline
from sentinelops.evals.cleanslate import CleanSlate
from sentinelops.evals.judge import Judge, calibrate
from sentinelops.evals.replay import ReplayHub
from sentinelops.evals.runner import BenchmarkRunner, answer_key
from sentinelops.evals.scoring import score
from sentinelops.stack.deploy_repo import DeployRepo
from sentinelops.stack.flags import FlagFile


def result(kind: str, service: str | None = None, root: str = "payments", flag=None, value=None):
    rca = RootCauseAnalysis.model_validate(
        {
            "summary": "s",
            "root_cause_service": root,
            "root_cause": "r",
            "claims": [{"statement": "x", "evidence": ["T1", "T2"]}],
            "confidence": "high",
            "recommended_action": {
                "kind": kind,
                "service": service,
                "flag": flag,
                "flag_value": value,
                "reason": "r",
            },
            "data_gaps": [],
        }
    )
    return InvestigationResult(status="rca", rca=rca, totals={})


KEY = {sid: answer_key(registry.get(sid)) for sid in registry.SCENARIOS}


# scoring


def test_correct_rollback_is_full_credit() -> None:
    s = score(KEY["bad_deploy/c"], result("rollback", "payments"))
    assert (s.root_cause_correct, s.action, s.harmful, s.escalation_correct) == (
        True,
        "full",
        False,
        True,
    )
    assert s.citations == 2


def test_rolling_back_the_decoy_is_harmful() -> None:
    s = score(KEY["bad_deploy/c"], result("rollback", "gateway", root="gateway"))
    assert (s.root_cause_correct, s.action, s.harmful) == (False, "wrong", True)


def test_restart_on_memory_leak_is_partial() -> None:
    s = score(KEY["memory_leak/a"], result("restart", "orders", root="orders"))
    assert s.action == "partial" and not s.harmful


def test_slow_dependency_external_counts_and_rollback_is_harmful() -> None:
    assert score(KEY["slow_dependency/b"], result("escalate", root="external")).action == "full"
    assert score(KEY["slow_dependency/b"], result("escalate", root="external")).root_cause_correct
    bad = score(KEY["slow_dependency/b"], result("rollback", "orders", root="orders"))
    assert bad.harmful and not bad.escalation_correct


def test_set_flag_must_match_flag_and_value() -> None:
    key = KEY["bad_feature_flag/a"]
    good = result("set_flag", root="gateway", flag="checkout_v2_pricing", value=False)
    wrong_value = result("set_flag", root="gateway", flag="checkout_v2_pricing", value=True)
    assert score(key, good).action == "full"
    assert score(key, wrong_value).action == "wrong"


def test_escalated_investigation_is_missed_unless_escalation_expected() -> None:
    escalated = InvestigationResult(status="escalated", escalation_reason="budget", totals={})
    assert score(KEY["bad_deploy/a"], escalated).action == "missed"
    assert score(KEY["slow_dependency/a"], escalated).action == "full"


# report


def write_results(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def record(system: str, action: str, harmful: bool = False, **kw: Any) -> dict[str, Any]:
    return {
        "run_id": kw.get("run_id", "r"),
        "scenario_id": kw.get("scenario_id", "bad_deploy/a"),
        "system": system,
        "manifested": kw.get("manifested", True),
        "status": kw.get("status", "rca"),
        "score": {
            "root_cause_correct": action == "full",
            "action": action,
            "harmful": harmful,
            "escalation_correct": True,
            "escalated": False,
            "claims": 1,
            "citations": 2,
        },
        "seconds_to_answer": 60,
        "totals": {"cost_usd": 0.5, "tool_calls": 8},
    }


def test_report_counts_only_manifested_runs(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(
        path,
        [
            record("sentinelops", "full"),
            record("sentinelops", "wrong", harmful=True),
            {
                **record("sentinelops", "full"),
                "manifested": False,
                "status": "not_manifested",
                "score": None,
            },
        ],
    )

    summary = report.summarize(report.load(path))["sentinelops"]

    assert summary["scored"] == "2" and summary["not manifested"] == "1"
    assert summary["correct action"] == "50% (1/2)"
    assert summary["harmful proposal"] == "50% (1/2)"
    assert summary["mean cost"] == "$0.500"
    assert "| bad_deploy/a | full, wrong, not_manifested |" in report.markdown(report.load(path))


# replay


def test_replay_serves_recorded_results_and_flags_unrecorded_calls(tmp_path: Path) -> None:
    (tmp_path / "tools.json").write_text(
        json.dumps(
            [
                {
                    "name": "read_logs",
                    "description": "d",
                    "input_schema": {},
                    "server": "logs",
                    "read_only": True,
                }
            ]
        )
    )
    rec = {
        "kind": "tool_call",
        "id": "T1",
        "tool": "read_logs",
        "arguments": {"service": "payments"},
        "result": "KeyError",
        "is_error": False,
        "policy": "allowed",
    }
    (tmp_path / "audit.jsonl").write_text(json.dumps(rec) + "\n")
    hub = ReplayHub(tmp_path)

    assert asyncio.run(hub.call("read_logs", {"service": "payments"})) == ("KeyError", False)
    text, is_error = asyncio.run(hub.call("read_logs", {"service": "orders"}))
    assert is_error and "not in recording" in text
    assert hub.specs["read_logs"].read_only


# judge


class FakeJudgeClient:
    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = verdicts
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        text = json.dumps({"verdict": self.verdicts.pop(0), "reason": "r"})
        return type("M", (), {"content": [type("B", (), {"type": "text", "text": text})()]})()


def test_calibration_reports_failures() -> None:
    cases = [("key", "a", "correct"), ("key", "b", "wrong"), ("key", "c", "wrong")]
    graded, failures = calibrate(
        Judge(client=FakeJudgeClient(["correct", "wrong", "wrong"])), cases
    )
    assert failures == [] and len(graded) == 3
    _, failures = calibrate(Judge(client=FakeJudgeClient(["correct", "wrong", "correct"])), cases)
    assert len(failures) == 1 and "'c'" in failures[0]


def test_calibration_set_covers_every_fault_type() -> None:
    from faults.scenario import FaultType
    from sentinelops.evals.__main__ import calibration_set
    from sentinelops.evals.judge_cases import CASES

    assert set(CASES) == {f.value for f in FaultType}
    verdicts = {expected for _, _, expected in calibration_set()}
    assert verdicts == {"correct", "partial", "wrong"}
    assert len(calibration_set()) == sum(len(c) for c in CASES.values())


def test_judge_uses_the_key_from_settings(monkeypatch) -> None:
    """The key lives in .env (Settings), not os.environ; the judge must get it explicitly."""
    import argparse

    from pydantic import SecretStr

    from sentinelops.config import Settings
    from sentinelops.evals import __main__ as cli

    seen: dict[str, Any] = {}

    class StopHere(Exception):
        pass

    def fake_client(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise StopHere

    monkeypatch.setattr(cli.anthropic, "Anthropic", fake_client)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-from-dotenv"))
    with pytest.raises(StopHere):
        cli.cmd_judge(argparse.Namespace(confirm_spend=True, results="x"), settings)
    assert seen["api_key"] == "sk-from-dotenv"


# clean slate


class FakeRuntime:
    def __init__(self) -> None:
        self.recreated: list[tuple[str, bool]] = []

    def recreate(self, service: str, force: bool = False) -> None:
        self.recreated.append((service, force))


class QuietAfter:
    def __init__(self, noisy_checks: int) -> None:
        self.left = noisy_checks

    def alerts(self) -> list[dict]:
        self.left -= 1
        return [{"labels": {"alertname": "HighErrorRate"}}] if self.left >= 0 else []


def test_clean_slate_archives_evidence_and_rebuilds_baseline(tmp_path: Path) -> None:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    repo.init()
    flags.init()
    repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "1.0"}, "old run")
    flags.set("checkout_v2_pricing", True, "old run")
    events = tmp_path / "events.jsonl"
    events.write_text('{"Action": "oom"}\n')
    runtime, reset = FakeRuntime(), []
    clean = CleanSlate(
        repo, flags, lambda: reset.append(1), runtime, events, QuietAfter(2), sleep=lambda s: None
    )

    clean.prepare(tmp_path / "archive")

    assert repo.last_change("payments") is None  # fresh history
    assert flags.read()["checkout_v2_pricing"] is False and reset == [1]
    assert (
        events.read_text() == ""
        and "oom" in (tmp_path / "archive" / "container-events.jsonl").read_text()
    )
    assert (tmp_path / "archive" / "deploy-repo" / ".git").exists()
    assert runtime.recreated[-1] == ("prometheus", True)
    assert {s for s, force in runtime.recreated if force} == {
        "prometheus",
        "payments",
        "orders",
        "gateway",
    }


def test_clean_slate_times_out_with_the_reason(tmp_path: Path) -> None:
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    ticks = iter(range(0, 10_000, 100))
    clean = CleanSlate(
        repo,
        flags,
        lambda: None,
        FakeRuntime(),
        tmp_path / "e.jsonl",
        QuietAfter(999),
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )

    with pytest.raises(TimeoutError, match="HighErrorRate"):
        clean.prepare(tmp_path / "archive", timeout=300)


# runner


class FakeEnvironment:
    def __init__(self) -> None:
        self.events: list[str] = []

    def inject(self, scenario, log=print):
        self.events.append(f"inject {scenario.id}")

    def reset(self, log=print):
        self.events.append("reset")


class FakeClean:
    def prepare(self, archive_dir: Path) -> None:
        pass


def make_runner(tmp_path: Path, monkeypatch, firing: list, outcome):
    from sentinelops.config import Settings
    from sentinelops.controlplane.models import Alert

    monkeypatch.setattr(
        runner, "firing_alerts", lambda url: ("ctx", [Alert(name=n, service=s) for n, s in firing])
    )
    monkeypatch.setattr(runner, "ALERT_TIMEOUT_SECONDS", 0.01)

    async def fake_investigate(settings, context, run_id, llm_factory):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    env = FakeEnvironment()
    return env, BenchmarkRunner(
        Settings(_env_file=None),
        env,
        FakeClean(),
        fake_investigate,
        results_path=tmp_path / "results.jsonl",
        recordings_dir=tmp_path / "rec",
        sleep=lambda s: None,
    )


def test_runner_scores_appends_and_resumes(tmp_path: Path, monkeypatch) -> None:
    scenario = registry.get("bad_deploy/a")
    env, bench = make_runner(
        tmp_path, monkeypatch, [("HighErrorRate", "payments")], result("rollback", "payments")
    )

    records = bench.run([scenario], repeats=1, log=lambda m: None)

    assert [r.system for r in records] == ["sentinelops"]
    assert records[0].score is not None and records[0].score.action == "full"
    assert env.events == ["inject bad_deploy/a", "reset"]
    assert bench.run([scenario], repeats=1, log=lambda m: None) == []  # resumed: skipped


def test_v1_runs_from_recordings_and_resumes_per_system(tmp_path: Path) -> None:
    from sentinelops.evals.runner import RunRecord, append, run_v1_from_recordings

    results, recordings = tmp_path / "results.jsonl", tmp_path / "rec"
    for run_id in ("bad_deploy-a-r1", "bad_deploy-b-r1"):
        append(
            results,
            RunRecord(
                run_id=run_id,
                scenario_id="bad_deploy/a",
                repeat=1,
                system="sentinelops",
                started_at="t",
                manifested=True,
                alerts_fired=["HighErrorRate(payments)"],
                status="rca",
            ),
        )
        (recordings / run_id).mkdir(parents=True)
        start = {"kind": "run_start", "alert_context": f"alerts for {run_id}"}
        (recordings / run_id / "audit.jsonl").write_text(json.dumps(start) + "\n")
    seen: list[tuple[str, str]] = []

    def fake_v1(variant: str, query: str) -> tuple[dict, dict]:
        seen.append((variant, query))
        return {"likely_root_cause": "db cpu"}, {"openai": "1.0"}

    first = run_v1_from_recordings(
        recordings, results, ("shipped", "our-runbooks"), fake_v1, log=lambda m: None
    )
    again = run_v1_from_recordings(
        recordings, results, ("shipped", "our-runbooks"), fake_v1, log=lambda m: None
    )

    assert len(first) == 4 and again == []
    assert ("shipped", "alerts for bad_deploy-a-r1") in seen
    assert {r.system for r in first} == {"v1-shipped", "v1-our-runbooks"}
    assert first[0].system_version == {"openai": "1.0"}


def test_resume_is_per_system(tmp_path: Path) -> None:
    from sentinelops.evals.runner import completed_runs

    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps({"run_id": "x", "system": "v1-shipped"}) + "\n")
    assert completed_runs(path) == set()
    assert completed_runs(path, "v1-shipped") == {"x"}


def test_runner_marks_unmanifested_and_errored_runs(tmp_path: Path, monkeypatch) -> None:
    scenario = registry.get("memory_leak/a")
    _, quiet = make_runner(tmp_path / "a", monkeypatch, [], result("rollback", "orders"))
    assert quiet.run([scenario], 1, log=lambda m: None)[0].status == "not_manifested"

    env, broken = make_runner(
        tmp_path / "b", monkeypatch, [("HighMemoryUsage", "orders")], RuntimeError("boom")
    )
    errored = broken.run([scenario], 1, log=lambda m: None)[0]
    assert errored.status == "error" and "boom" in (errored.error or "")
    assert env.events[-1] == "reset"


# v1 baseline


@pytest.mark.skipif(
    not (v1_baseline.LEGACY / "src" / "orchestrator.py").exists(),
    reason="needs the local v1 working copy (its LLM orchestrator was never published)",
)
def test_v1_snapshot_has_the_llm_orchestrator_and_leaves_legacy_alone(tmp_path: Path) -> None:
    before = v1_baseline.snapshot_hash(v1_baseline.LEGACY / "src")

    workdir = v1_baseline.export("shipped", tmp_path)

    assert (workdir / "src" / "orchestrator.py").exists()
    assert "FastMCPClient" in (workdir / "src" / "orchestrator.py").read_text()
    assert not (workdir / "storage").exists() and not (workdir / ".git").exists()
    assert v1_baseline.snapshot_hash(v1_baseline.LEGACY / "src") == before
    assert len(v1_baseline.snapshot_hash(workdir)) == 64
    assert len(v1_baseline.legacy_sha()) == 40


def test_v1_command_adds_our_runbooks_only_for_that_variant(tmp_path: Path) -> None:
    shipped = v1_baseline.command("shipped", tmp_path, "HighErrorRate on payments")
    ours = v1_baseline.command("our-runbooks", tmp_path, "HighErrorRate on payments")

    assert shipped[-1] == "HighErrorRate on payments"
    assert ours[-1].endswith("docs/runbooks")
    assert "--no-project" in shipped and "--with-requirements" in shipped


def test_runner_sets_aside_a_crashed_attempts_recording(tmp_path: Path, monkeypatch) -> None:
    _, bench = make_runner(
        tmp_path, monkeypatch, [("HighErrorRate", "payments")], result("rollback", "payments")
    )
    stale = tmp_path / "rec" / "bad_deploy-a-r1"
    stale.mkdir(parents=True)
    (stale / "audit.jsonl").write_text("old\n")

    bench.run([registry.get("bad_deploy/a")], repeats=1, log=lambda m: None)

    assert not (stale / "audit.jsonl").exists()
    assert any(
        p.name.startswith("bad_deploy-a-r1.incomplete-") for p in (tmp_path / "rec").iterdir()
    )


# same-metric extraction


def test_extracted_correct_matches_the_structured_rule() -> None:
    from sentinelops.evals.extract import Extraction, correct

    assert correct(Extraction(blamed="payments", hedged=False), "payments", False)
    assert not correct(Extraction(blamed="payments", hedged=True), "payments", False)
    assert not correct(Extraction(blamed="database", hedged=False), "orders", False)
    assert correct(Extraction(blamed="external", hedged=False), "payments", True)
    assert not correct(Extraction(blamed="external", hedged=False), "payments", False)
    assert correct(Extraction(blamed="orders", hedged=True), "orders", False, allow_hedge=True)
    assert not correct(
        Extraction(blamed="database", hedged=True), "orders", False, allow_hedge=True
    )


def test_extractor_parses_structured_output() -> None:
    from sentinelops.evals.extract import Extractor

    class Client:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> Any:
            assert kwargs["output_config"]["format"]["schema"]["required"] == ["blamed", "hedged"]
            text = json.dumps({"blamed": "database", "hedged": True})
            return type("M", (), {"content": [type("B", (), {"type": "text", "text": text})()]})()

    got = Extractor(Client()).extract("Potential DB issues or a recent deploy")
    assert (got.blamed, got.hedged) == ("database", True)


def test_report_shows_same_metric_row(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    rows = [
        {
            **record("sentinelops", "full"),
            "extracted": {"blamed": "payments", "hedged": False},
            "extracted_correct": True,
        },
        {
            "run_id": "r",
            "scenario_id": "bad_deploy/a",
            "system": "v1-shipped",
            "manifested": True,
            "status": "answered",
            "extracted": {"blamed": "database", "hedged": True},
            "extracted_correct": False,
        },
    ]
    write_results(path, rows)

    summary = report.summarize(report.load(path))

    assert summary["sentinelops"]["root cause (same metric, all systems)"] == "100% (1/1)"
    assert summary["v1-shipped"]["root cause (same metric, all systems)"] == "0% (0/1)"
    assert summary["v1-shipped"]["hedged between causes"] == "100% (1/1)"


def test_naive_rule_guesses_most_downstream_and_is_scored_like_the_agent(tmp_path: Path) -> None:
    from sentinelops.evals import heuristic
    from sentinelops.evals.runner import RunRecord, append

    assert heuristic.guess(["HighErrorRate(gateway)", "HighErrorRate(orders)"]) == "orders"
    results = tmp_path / "results.jsonl"
    for sid, alerts in [
        ("bad_deploy/a", ["HighErrorRate(payments)"]),
        ("slow_dependency/a", ["HighLatencyP95(gateway)"]),
    ]:
        append(
            results,
            RunRecord(
                run_id=sid.replace("/", "-"),
                scenario_id=sid,
                repeat=1,
                system="sentinelops",
                started_at="t",
                manifested=True,
                alerts_fired=alerts,
                status="rca",
            ),
        )

    records = heuristic.run(results)

    grades = {r.scenario_id: r.score.action for r in records if r.score}
    assert grades == {"bad_deploy/a": "full", "slow_dependency/a": "wrong"}
    assert heuristic.run(results) == []
