"""Tests for the investigation loop with a scripted fake LLM and in-process tool servers."""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import Budget, InvestigationResult, Investigator
from sentinelops.agent.llm import FallbackLLM, LLMUnavailable
from sentinelops.agent.models import (
    AssistantTurn,
    LLMResponse,
    ToolCall,
    ToolResults,
    ToolSpec,
    TranscriptItem,
    Usage,
    UserMessage,
)
from sentinelops.agent.policy import PolicyGate
from sentinelops.agent.toolhub import ToolHub

READ_ONLY = ToolAnnotations(read_only_hint=True)


def demo_server(prometheus_up: bool = True) -> MCPServer:
    server = MCPServer("demo")

    @server.tool(description="alerts", annotations=READ_ONLY)
    def list_alerts() -> str:
        if not prometheus_up:
            raise ToolError("Prometheus is unreachable")
        return '{"alerts":[{"name":"HighErrorRate","service":"payments"}]}'

    @server.tool(description="logs", annotations=READ_ONLY)
    def read_logs(service: str) -> str:
        return f"{service}: KeyError: 'risk_score'"

    @server.tool(description="restart a service")
    def restart_service(service: str) -> str:
        return "restarted"

    @server.tool(description="read-only but not on the allowlist", annotations=READ_ONLY)
    def secret_tool() -> str:
        return "should never be offered"

    return server


def turn(text: str = "", calls: Sequence[tuple[str, str, dict[str, Any]]] = ()) -> AssistantTurn:
    tool_calls = [ToolCall(id=i, name=n, arguments=a) for i, n, a in calls]
    return AssistantTurn(text=text, tool_calls=tool_calls, provider="fake")


def response(text: str = "", calls=(), stop: str | None = None) -> LLMResponse:
    stop_reason = stop or ("tool_use" if calls else "end_turn")
    return LLMResponse(
        turn=turn(text, calls),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=Usage(input_tokens=100, output_tokens=20),
        model="fake-model",
        cost_usd=0.001,
    )


def rca_json(evidence: list[str], action: str = "rollback", gaps: Sequence[str] = ()) -> str:
    return json.dumps(
        {
            "summary": "payments release broke charges",
            "root_cause_service": "payments",
            "root_cause": "fraud check reads a missing field",
            "claims": [{"statement": "payments raises KeyError", "evidence": evidence}],
            "confidence": "high",
            "recommended_action": {
                "kind": action,
                "service": "payments",
                "flag": None,
                "flag_value": None,
                "reason": "roll back the bad release",
            },
            "data_gaps": list(gaps),
        }
    )


Step = LLMResponse | Callable[[Sequence[TranscriptItem], Sequence[ToolSpec]], LLMResponse]


class FakeLLM:
    provider = "fake"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = steps
        self.seen: list[tuple[list[TranscriptItem], list[str]]] = []
        self.tools_enabled: list[bool] = []

    async def complete(
        self, system, transcript, tools, output_schema, tools_enabled=True
    ) -> LLMResponse:
        self.seen.append((list(transcript), [t.name for t in tools]))
        self.tools_enabled.append(tools_enabled)
        step = self.steps.pop(0)
        return step(transcript, tools) if callable(step) else step


def investigate(
    tmp_path: Path, llm: Any, budget: Budget | None = None, prometheus_up: bool = True
) -> tuple[InvestigationResult, AuditLog]:
    audit = AuditLog(tmp_path / "audit.jsonl")

    async def run() -> InvestigationResult:
        async with ToolHub({"demo": demo_server(prometheus_up)}) as hub:
            investigator = Investigator(llm, hub, audit, PolicyGate(), budget or Budget())
            return await investigator.run("HighErrorRate on payments")

    return asyncio.run(run()), audit


def last_tool_results(transcript: Sequence[TranscriptItem]) -> ToolResults:
    return next(item for item in reversed(transcript) if isinstance(item, ToolResults))


def test_happy_path_produces_cited_rca(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            response(
                calls=[("c1", "list_alerts", {}), ("c2", "read_logs", {"service": "payments"})]
            ),
            response(rca_json(["T1", "T2"])),
        ]
    )

    result, audit = investigate(tmp_path, llm)

    assert result.status == "rca"
    assert result.rca is not None and result.rca.claims[0].evidence == ["T1", "T2"]
    results = last_tool_results(llm.seen[1][0]).results
    assert results[0].content.startswith("[T1] ")
    assert results[1].content == "[T2] payments: KeyError: 'risk_score'"
    kinds = [
        json.loads(line)["kind"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert kinds == ["run_start", "llm_call", "tool_call", "tool_call", "llm_call", "run_end"]
    assert result.totals["llm_calls"] == 2 and result.totals["tool_calls"] == 2


def test_only_allowlisted_read_only_tools_are_offered(tmp_path: Path) -> None:
    llm = FakeLLM(
        [response(calls=[("c1", "read_logs", {"service": "payments"})]), response(rca_json(["T1"]))]
    )

    investigate(tmp_path, llm)

    assert sorted(llm.seen[0][1]) == ["list_alerts", "read_logs"]


def test_policy_denies_write_tools_even_if_requested(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            response(calls=[("c1", "restart_service", {"service": "payments"})]),
            response(calls=[("c2", "read_logs", {"service": "payments"})]),
            response(rca_json(["T2"])),
        ]
    )

    result, audit = investigate(tmp_path, llm)

    denied = audit.tool_calls()["T1"]
    assert denied["policy"] == "denied" and denied["is_error"]
    model_saw = last_tool_results(llm.seen[1][0]).results[0]
    assert model_saw.is_error and "denied by policy" in model_saw.content
    assert result.status == "rca"


def test_tool_failure_reaches_model_and_cannot_be_cited(tmp_path: Path) -> None:
    """The v1 bug: a broken tool must look broken, and the answer may not rely on it."""
    llm = FakeLLM(
        [
            response(
                calls=[("c1", "list_alerts", {}), ("c2", "read_logs", {"service": "payments"})]
            ),
            response(rca_json(["T1", "T2"])),
            response(rca_json(["T2"], gaps=["metrics unavailable: Prometheus unreachable"])),
        ]
    )

    result, audit = investigate(tmp_path, llm, prometheus_up=False)

    first = last_tool_results(llm.seen[1][0]).results[0]
    assert first.is_error and "Prometheus is unreachable" in first.content
    rejected = [r for r in audit.records if r["kind"] == "answer_rejected"]
    assert "T1 failed, so it cannot be cited as evidence" in rejected[0]["problems"]
    assert result.status == "rca"
    assert result.rca is not None and result.rca.data_gaps


def test_invalid_answer_twice_escalates(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            response(calls=[("c1", "read_logs", {"service": "payments"})]),
            response(rca_json(["T9"])),
            response("not json"),
        ]
    )

    result, _ = investigate(tmp_path, llm)

    assert result.status == "escalated"
    assert "failed validation twice" in (result.escalation_reason or "")
    repair = llm.seen[2][0][-1]
    assert isinstance(repair, UserMessage) and "T9 is not a tool call" in repair.text


def test_tool_budget_forces_final_answer_without_tools(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            response(calls=[("c1", "read_logs", {"service": "payments"})]),
            response(calls=[("c2", "read_logs", {"service": "orders"})]),
            response(rca_json(["T1", "T2"], action="escalate")),
        ]
    )

    result, _ = investigate(tmp_path, llm, budget=Budget(max_tool_calls=2))

    assert llm.tools_enabled == [True, True, False]
    final_prompt = llm.seen[2][0][-1]
    assert isinstance(final_prompt, UserMessage) and "budget" in final_prompt.text
    assert result.status == "rca"


def test_parallel_calls_past_the_budget_are_skipped_not_dropped(tmp_path: Path) -> None:
    calls = [("c1", "list_alerts", {}), ("c2", "read_logs", {"service": "payments"})]
    llm = FakeLLM(
        [
            response(calls=[*calls, ("c3", "read_logs", {"service": "orders"})]),
            response(rca_json(["T1", "T2"])),
        ]
    )

    result, audit = investigate(tmp_path, llm, budget=Budget(max_tool_calls=2))

    results = last_tool_results(llm.seen[1][0]).results
    assert [r.call_id for r in results] == ["c1", "c2", "c3"]
    assert results[2].is_error and "budget" in results[2].content
    assert [r["kind"] for r in audit.records].count("tool_skipped") == 1
    assert llm.tools_enabled == [True, False]
    assert result.status == "rca"


def test_tool_call_after_budget_escalates(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            response(calls=[("c1", "read_logs", {"service": "payments"})]),
            response(calls=[("c2", "read_logs", {"service": "orders"})]),
        ]
    )

    result, _ = investigate(tmp_path, llm, budget=Budget(max_tool_calls=1))

    assert result.status == "escalated"
    assert "after the tool budget" in (result.escalation_reason or "")


def test_llm_failure_is_written_to_the_audit_log(tmp_path: Path) -> None:
    class Broken:
        provider = "fake"

        async def complete(self, *args: Any) -> LLMResponse:
            raise RuntimeError("400 invalid schema")

    with pytest.raises(RuntimeError):
        investigate(tmp_path, Broken())

    kinds = [
        json.loads(line)["kind"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    assert kinds == ["run_start", "llm_error", "run_end"]


def test_action_must_name_its_target(tmp_path: Path) -> None:
    bad = json.loads(rca_json(["T1"]))
    bad["recommended_action"]["service"] = None
    llm = FakeLLM(
        [
            response(calls=[("c1", "read_logs", {"service": "payments"})]),
            response(json.dumps(bad)),
            response(rca_json(["T1"])),
        ]
    )

    result, audit = investigate(tmp_path, llm)

    rejected = next(r for r in audit.records if r["kind"] == "answer_rejected")
    assert "recommended_action rollback needs a service" in rejected["problems"]
    assert result.status == "rca"


def test_time_budget_escalates_without_calling_the_model(tmp_path: Path) -> None:
    llm = FakeLLM([])

    result, _ = investigate(tmp_path, llm, budget=Budget(max_seconds=-1))

    assert result.status == "escalated"
    assert "time limit" in (result.escalation_reason or "")
    assert llm.seen == []


def test_refusal_escalates(tmp_path: Path) -> None:
    result, _ = investigate(tmp_path, FakeLLM([response(stop="refusal")]))

    assert result.status == "escalated"
    assert "refusal" in (result.escalation_reason or "")


class Unavailable:
    provider = "down"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *args: Any) -> LLMResponse:
        self.calls += 1
        raise LLMUnavailable("connection refused")


def test_fallback_switches_provider_and_stays_switched(tmp_path: Path) -> None:
    primary = Unavailable()
    secondary = FakeLLM(
        [response(calls=[("c1", "read_logs", {"service": "payments"})]), response(rca_json(["T1"]))]
    )
    audit = AuditLog(tmp_path / "fallback.jsonl")
    llm = FallbackLLM(primary, secondary, audit)

    result, _ = investigate(tmp_path, llm)

    assert result.status == "rca"
    assert primary.calls == 1
    assert [r["kind"] for r in audit.records] == ["provider_fallback"]


def test_no_fallback_configured_raises(tmp_path: Path) -> None:
    llm = FallbackLLM(Unavailable(), None, AuditLog(tmp_path / "x.jsonl"))

    with pytest.raises(LLMUnavailable):
        investigate(tmp_path, llm)


def test_credentials_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pydantic import SecretStr

    from sentinelops import investigate as cli
    from sentinelops.config import Settings

    for name in cli.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    settings = Settings(_env_file=None)
    assert cli.has_anthropic_credentials(settings) is False

    settings.anthropic_api_key = SecretStr("sk-test")
    assert cli.has_anthropic_credentials(settings) is True
