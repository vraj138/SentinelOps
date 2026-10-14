"""Runs the benchmark: clean slate -> inject -> wait for the alert -> investigate -> score.

One JSON line per finished run goes to the results file, so an interrupted benchmark resumes
where it stopped (finished run ids are skipped).
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from faults.environment import Environment
from faults.scenario import Scenario
from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import InvestigationResult
from sentinelops.agent.llm import LLM
from sentinelops.config import Settings
from sentinelops.controlplane.alerts import firing_alerts
from sentinelops.controlplane.models import Alert
from sentinelops.evals.cleanslate import CleanSlate
from sentinelops.evals.scoring import AnswerKey, Score, score

ALERT_TIMEOUT_SECONDS = 300
SYSTEM_NAIVE = "naive-agent"


class NaiveAgent(BaseModel):
    """How to run the naive baseline: its investigate function and its auto-executor."""

    model_config = {"arbitrary_types_allowed": True}
    investigate: Any
    executor: Any


class RunRecord(BaseModel):
    run_id: str
    scenario_id: str
    repeat: int
    system: str  # "sentinelops" or a v1 variant
    started_at: str
    manifested: bool
    alerts_fired: list[str]
    seconds_to_alert: float | None = None
    status: str | None = None  # rca | escalated | error | not_manifested
    score: Score | None = None
    root_cause_text: str | None = None
    recommended_action: dict[str, Any] | None = None
    totals: dict[str, Any] = {}
    seconds_to_answer: float | None = None
    error: str | None = None
    host_free_memory_mb: float | None = None
    system_version: dict[str, Any] = {}
    # Naive agent only: its proposal is executed without approval, then health is measured.
    executed: bool | None = None
    execution_error: str | None = None
    fixed_after_action: bool | None = None
    failed_checks: list[str] = []


def answer_key(scenario: Scenario) -> AnswerKey:
    truth = scenario.ground_truth
    action = truth.expected_action
    return AnswerKey(
        scenario_id=scenario.id,
        root_cause_service=truth.root_cause_service,
        accepted_root_causes=truth.accepted,
        expected_kind=action.kind,
        expected_service=action.service,
        expected_flag=action.flag,
        expected_flag_value=action.flag_value,
        acceptable=[a.model_dump() for a in truth.acceptable_actions],
    )


def completed_runs(results_path: Path, system: str = "sentinelops") -> set[str]:
    """Run ids already finished by `system` (each system resumes independently).

    Errored runs don't count as finished, so resuming retries them (for example after an API
    outage or an empty credit balance).
    """
    if not results_path.exists():
        return set()
    records = [json.loads(line) for line in results_path.read_text().splitlines() if line]
    return {r["run_id"] for r in records if r["system"] == system and r.get("status") != "error"}


def host_free_memory_mb() -> float | None:
    """Free physical memory, recorded per run because timing alerts depend on host load."""
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (ValueError, OSError):
        return None


Investigate = Callable[[Settings, str, str, Callable[[AuditLog], LLM] | None], Any]


class BenchmarkRunner:
    def __init__(
        self,
        settings: Settings,
        environment: Environment,
        clean_slate: CleanSlate,
        investigate: Investigate,
        results_path: Path,
        recordings_dir: Path,
        llm_factory: Callable[[AuditLog], LLM] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        naive: NaiveAgent | None = None,
    ) -> None:
        """`naive`, if given, also runs the naive baseline agent on the same live fault."""
        self.settings = settings
        self.environment = environment
        self.clean_slate = clean_slate
        self.investigate = investigate
        self.results_path = results_path
        self.recordings_dir = recordings_dir
        self.llm_factory = llm_factory
        self.sleep = sleep
        self.naive = naive

    def run(
        self, scenarios: list[Scenario], repeats: int, log: Callable[[str], None] = print
    ) -> list[RunRecord]:
        done = completed_runs(self.results_path)
        records = []
        for repeat in range(1, repeats + 1):
            for scenario in scenarios:
                run_id = f"{scenario.id.replace('/', '-')}-r{repeat}"
                if run_id in done:
                    log(f"skip {run_id} (already in results)")
                    continue
                log(f"run {run_id}")
                for record in self.run_one(scenario, repeat, run_id, log):
                    append(self.results_path, record)
                    records.append(record)
                    graded = record.score.model_dump() if record.score else ""
                    log(f"  -> {record.system}: {record.status} {graded}")
        return records

    def run_one(
        self, scenario: Scenario, repeat: int, run_id: str, log: Callable[[str], None]
    ) -> list[RunRecord]:
        """SentinelOps, then the naive agent (if configured), on one live fault instance.

        SentinelOps goes first because it never changes the system; the naive agent acts. v1 runs
        later from the recording, since it never reads live state.
        """
        started = datetime.now(UTC).isoformat(timespec="seconds")
        run_dir = self.recordings_dir / run_id
        if run_dir.exists():  # an earlier attempt crashed before writing its result line
            run_dir.rename(run_dir.with_name(f"{run_id}.incomplete-{started.replace(':', '')}"))
        base = {
            "run_id": run_id,
            "scenario_id": scenario.id,
            "repeat": repeat,
            "system": "sentinelops",
            "started_at": started,
            "host_free_memory_mb": host_free_memory_mb(),
        }
        try:
            log("  clean slate")
            self.clean_slate.prepare(run_dir / "previous")
            log("  inject")
            self.environment.inject(scenario, log=lambda m: log(f"    {m}"))
            t0 = time.monotonic()
            fired = self._wait_for_alert(scenario)
            if not fired:
                return [
                    RunRecord(**base, manifested=False, alerts_fired=[], status="not_manifested")
                ]
            seconds_to_alert = time.monotonic() - t0
            context, alerts = firing_alerts(self.settings.prometheus_url)
            log(f"  investigate ({', '.join(fired)})")
            settings = self.settings.model_copy(update={"runs_dir": self.recordings_dir})
            t1 = time.monotonic()
            result: InvestigationResult = asyncio.run(
                self.investigate(settings, context, run_id, self.llm_factory)
            )
            rca = result.rca
            records = [
                RunRecord(
                    **base,
                    manifested=True,
                    alerts_fired=fired,
                    seconds_to_alert=round(seconds_to_alert, 1),
                    status=result.status,
                    score=score(answer_key(scenario), result),
                    root_cause_text=rca.root_cause if rca else result.escalation_reason,
                    recommended_action=rca.recommended_action.model_dump() if rca else None,
                    totals=result.totals,
                    seconds_to_answer=round(time.monotonic() - t1, 1),
                )
            ]
            if self.naive is not None:
                log("  naive agent")
                common = {
                    **base,
                    "system": SYSTEM_NAIVE,
                    "alerts_fired": fired,
                    "seconds_to_alert": round(seconds_to_alert, 1),
                }
                records.append(self._run_naive(scenario, settings, context, alerts, run_id, common))
            return records
        except Exception as exc:  # noqa: BLE001 - recorded as an errored run, never dropped
            return [
                RunRecord(
                    **base, manifested=False, alerts_fired=[], status="error", error=repr(exc)
                )
            ]
        finally:
            self.environment.reset(log=lambda m: None)

    def _run_naive(
        self,
        scenario: Scenario,
        settings: Settings,
        context: str,
        alerts: list[Alert],
        run_id: str,
        common: dict[str, Any],
    ) -> RunRecord:
        assert self.naive is not None
        t0 = time.monotonic()
        try:
            result: InvestigationResult = asyncio.run(
                self.naive.investigate(settings, context, f"{run_id}-naive", self.llm_factory)
            )
            answered = round(time.monotonic() - t0, 1)
            outcome = self.naive.executor.apply(result.rca, alerts, run_id)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            return RunRecord(**common, manifested=True, status="error", error=repr(exc))
        rca = result.rca
        return RunRecord(
            **common,
            manifested=True,
            status=result.status,
            score=score(answer_key(scenario), result),
            root_cause_text=rca.root_cause if rca else result.escalation_reason,
            recommended_action=rca.recommended_action.model_dump() if rca else None,
            totals=result.totals,
            seconds_to_answer=answered,
            **outcome.model_dump(),
        )

    def _wait_for_alert(self, scenario: Scenario) -> list[str]:
        """Wait until any expected alert fires. Returns the expected alerts that are firing."""
        expected = {(a.name, a.service) for a in scenario.ground_truth.expected_alerts}
        deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            _, alerts = firing_alerts(self.settings.prometheus_url)
            fired = sorted(
                f"{a.name}({a.service})" for a in alerts if (a.name, a.service) in expected
            )
            if fired:
                return fired
            self.sleep(5)
        return []


def append(results_path: Path, record: RunRecord) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a") as f:
        f.write(record.model_dump_json() + "\n")


def run_v1_from_recordings(
    recordings_dir: Path,
    results_path: Path,
    variants: tuple[str, ...],
    v1: Callable[[str, str], tuple[dict, dict]],
    log: Callable[[str], None] = print,
) -> list[RunRecord]:
    """Run v1 on every recorded SentinelOps run's alert text, per variant, resuming per system.

    v1 never reads live state (its metrics client import fails), so the alert text it would
    have been given is all it needs, and it can run without the stack.
    """
    sentinel = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            r = json.loads(line)
            if r["system"] == "sentinelops" and r["manifested"]:
                sentinel[r["run_id"]] = r
    records = []
    for variant in variants:
        system = f"v1-{variant}"
        done = completed_runs(results_path, system)
        for run_id, base in sorted(sentinel.items()):
            if run_id in done:
                continue
            first = (recordings_dir / run_id / "audit.jsonl").read_text().splitlines()[0]
            context = json.loads(first)["alert_context"]
            log(f"v1 ({variant}) {run_id}")
            common = {
                k: base[k]
                for k in ("run_id", "scenario_id", "repeat", "started_at", "alerts_fired")
            }
            t0 = time.monotonic()
            try:
                plan, versions = v1(variant, context)
                record = RunRecord(
                    **common,
                    system=system,
                    manifested=True,
                    status="answered",
                    root_cause_text=str(plan.get("likely_root_cause", "")),
                    recommended_action={"remediation_steps": plan.get("remediation_steps") or []},
                    seconds_to_answer=round(time.monotonic() - t0, 1),
                    system_version=versions,
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never dropped
                record = RunRecord(
                    **common, system=system, manifested=True, status="error", error=repr(exc)
                )
            append(results_path, record)
            records.append(record)
    return records
