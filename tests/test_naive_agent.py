"""Tests for the naive baseline agent: its prompt, its non-strict answers, its auto-executor."""

import asyncio
from pathlib import Path

import pytest

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import Budget, Investigator
from sentinelops.agent.policy import PolicyGate
from sentinelops.agent.prompts import SYSTEM_PROMPT
from sentinelops.agent.toolhub import ToolHub
from sentinelops.controlplane.actions import ActionBackend
from sentinelops.controlplane.models import Alert
from sentinelops.controlplane.verify import Verifier
from sentinelops.evals.naive_agent import NAIVE_SYSTEM_PROMPT, NaiveExecutor, naive_prompt
from sentinelops.stack.deploy_repo import BASE_RELEASES, DeployRepo
from sentinelops.stack.flags import FlagFile
from tests.test_agent import FakeLLM, demo_server, rca_json, response
from tests.test_controlplane import FakeMetrics, FakeRuntime, rca


def test_naive_prompt_removes_exactly_the_safety_guidance() -> None:
    assert "[T3]" not in NAIVE_SYSTEM_PROMPT
    assert "outside our control" not in NAIVE_SYSTEM_PROMPT
    assert "after a human approves" not in NAIVE_SYSTEM_PROMPT
    assert "applied automatically" in NAIVE_SYSTEM_PROMPT
    # Neutral investigation know-how and the answer format are shared with SentinelOps.
    assert "Errors and latency propagate upstream" in NAIVE_SYSTEM_PROMPT
    assert "recommended_action is one of" in NAIVE_SYSTEM_PROMPT
    assert len(NAIVE_SYSTEM_PROMPT) < len(SYSTEM_PROMPT)


def test_naive_prompt_fails_loudly_if_the_agent_prompt_changes() -> None:
    with pytest.raises(ValueError, match="no longer matches"):
        naive_prompt(SYSTEM_PROMPT.replace("Evidence rules:", "Evidence:"))


def run_investigator(tmp_path: Path, llm, strict: bool):
    audit = AuditLog(tmp_path / "audit.jsonl")

    async def go():
        async with ToolHub({"demo": demo_server()}) as hub:
            return await Investigator(llm, hub, audit, PolicyGate(), Budget(), strict=strict).run(
                "x"
            )

    return asyncio.run(go())


def test_non_strict_accepts_uncited_answers_and_never_repairs(tmp_path: Path) -> None:
    llm = FakeLLM(
        [response(calls=[("c1", "read_logs", {"service": "payments"})]), response(rca_json(["T9"]))]
    )
    assert run_investigator(tmp_path, llm, strict=False).status == "rca"

    garbage = FakeLLM([response("not json")])
    result = run_investigator(tmp_path / "b", garbage, strict=False)
    assert result.status == "escalated" and "invalid answer" in (result.escalation_reason or "")
    assert len(garbage.seen) == 1  # no repair turn


@pytest.fixture
def stack(tmp_path: Path):
    repo, flags = DeployRepo(tmp_path / "repo"), FlagFile(tmp_path / "flags.json")
    repo.init()
    flags.init()
    return repo, flags, FakeRuntime()


def executor(stack, metrics) -> NaiveExecutor:
    verifier = Verifier(metrics, settle_seconds=0, deadline_seconds=0, sleep=lambda s: None)
    return NaiveExecutor(ActionBackend(*stack), verifier)


ALERTS = [Alert(name="HighErrorRate", service="payments")]


def test_escalation_executes_nothing(stack) -> None:
    outcome = executor(stack, FakeMetrics()).apply(rca("escalate", None), ALERTS, "r")
    assert not outcome.executed and outcome.fixed_after_action is None


def test_proposal_is_executed_without_approval_and_health_measured(stack) -> None:
    repo, _, runtime = stack
    repo.deploy("payments", {"PAYMENTS_FRAUD_CHECK_RATE": "1.0"}, "bad")

    outcome = executor(stack, FakeMetrics()).apply(rca("rollback", "payments"), ALERTS, "r")

    assert outcome.executed and outcome.fixed_after_action is True
    assert repo.read("payments") == BASE_RELEASES["payments"]
    assert runtime.calls == [("recreate", "payments")]


def test_wrong_change_is_left_in_place(stack) -> None:
    repo, _, runtime = stack
    repo.deploy("gateway", {}, "decoy")
    before = repo.read("gateway")

    outcome = executor(stack, FakeMetrics(failing={"payments"})).apply(
        rca("rollback", "gateway"), ALERTS, "r"
    )

    assert outcome.executed and outcome.fixed_after_action is False
    assert any("payments" in c for c in outcome.failed_checks)
    assert repo.read("gateway") != before  # no auto-rollback: the wrong change stays


def test_unexecutable_proposal_is_recorded(stack) -> None:
    outcome = executor(stack, FakeMetrics()).apply(rca("rollback", "orders"), ALERTS, "r")
    assert not outcome.executed and "no deploy to roll back" in (outcome.execution_error or "")
