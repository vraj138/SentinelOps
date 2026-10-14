"""The investigation loop: model -> policy gate -> tool -> audit log, until a cited RCA.

Ends in one of two ways:
- "rca": the model produced a root-cause analysis whose every claim cites successful tool
  calls from this run.
- "escalated": a budget ran out, the model refused or was cut off, or its answer could not be
  validated after one repair attempt. The evidence gathered so far is kept in the audit log.
"""

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.llm import LLM
from sentinelops.agent.models import (
    RCA_JSON_SCHEMA,
    LLMResponse,
    RootCauseAnalysis,
    ToolResult,
    ToolResults,
    ToolSpec,
    TranscriptItem,
    UserMessage,
)
from sentinelops.agent.policy import PolicyGate
from sentinelops.agent.prompts import (
    FINAL_ANSWER_NOW,
    SYSTEM_PROMPT,
    initial_message,
    repair_message,
)
from sentinelops.agent.toolhub import ToolHub


class Budget(BaseModel):
    max_tool_calls: int = 30
    max_tokens: int = 1_000_000
    max_cost_usd: float = 3.0
    max_seconds: float = 600


class InvestigationResult(BaseModel):
    status: Literal["rca", "escalated"]
    rca: RootCauseAnalysis | None = None
    escalation_reason: str | None = None
    totals: dict[str, Any]


def parse_answer(text: str) -> tuple[RootCauseAnalysis | None, list[str]]:
    """Parse the final answer only: no citation or action checks."""
    try:
        return RootCauseAnalysis.model_validate(json.loads(text)), []
    except (json.JSONDecodeError, ValidationError) as exc:
        return None, [f"the answer is not valid JSON in the required format: {exc}"]


def validate_answer(text: str, audit: AuditLog) -> tuple[RootCauseAnalysis | None, list[str]]:
    """Parse the final answer and check every citation. Returns (rca, problems)."""
    try:
        rca = RootCauseAnalysis.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        return None, [f"the answer is not valid JSON in the required format: {exc}"]
    calls = audit.tool_calls()
    problems = []
    if not rca.claims:
        problems.append("the answer has no claims")
    for claim in rca.claims:
        if not claim.evidence:
            problems.append(f"claim has no evidence: {claim.statement!r}")
        for ref in claim.evidence:
            if ref not in calls:
                problems.append(f"{ref} is not a tool call in this investigation")
            elif calls[ref]["is_error"]:
                problems.append(f"{ref} failed, so it cannot be cited as evidence")
    action = rca.recommended_action
    if action.kind in ("rollback", "restart") and action.service is None:
        problems.append(f"recommended_action {action.kind} needs a service")
    if action.kind == "set_flag" and (action.flag is None or action.flag_value is None):
        problems.append("recommended_action set_flag needs flag and flag_value")
    return (rca if not problems else None), problems


class Investigator:
    def __init__(
        self,
        llm: LLM,
        hub: ToolHub,
        audit: AuditLog,
        gate: PolicyGate,
        budget: Budget,
        system_prompt: str = SYSTEM_PROMPT,
        strict: bool = True,
    ) -> None:
        """`strict` is SentinelOps' safety layer for answers: every claim must cite successful
        tool calls, the action must name its target, and an invalid answer gets one repair
        attempt. The naive baseline agent runs with strict=False (accept the answer as given)."""
        self.llm = llm
        self.hub = hub
        self.audit = audit
        self.gate = gate
        self.budget = budget
        self.system_prompt = system_prompt
        self.strict = strict

    async def run(self, alert_context: str) -> InvestigationResult:
        started = time.monotonic()
        transcript: list[TranscriptItem] = [UserMessage(text=initial_message(alert_context))]
        tools = [
            spec for name, spec in self.hub.specs.items() if self.gate.check(name, spec) is None
        ]
        self.audit.record("run_start", alert_context=alert_context, tools=[t.name for t in tools])
        repairs_left = 1
        tools_enabled = True

        while True:
            exhausted = self._exhausted(started)
            if exhausted:
                return self._finish("escalated", reason=f"budget exhausted: {exhausted}")

            response = await self._complete(transcript, tools, tools_enabled)
            transcript.append(response.turn)

            if response.stop_reason == "tool_use" and response.turn.tool_calls:
                if not tools_enabled:
                    return self._finish(
                        "escalated", reason="model kept calling tools after the tool budget ran out"
                    )
                results = []
                for call in response.turn.tool_calls:
                    if self._tool_calls_used() >= self.budget.max_tool_calls:
                        results.append(self._skip_tool(call.id, call.name))
                    else:
                        results.append(await self._run_tool(call.id, call.name, call.arguments))
                transcript.append(ToolResults(results=results))
                if self._tool_calls_used() >= self.budget.max_tool_calls:
                    tools_enabled = False
                    transcript.append(UserMessage(text=FINAL_ANSWER_NOW))
                continue

            if response.stop_reason in ("refusal", "max_tokens", "other"):
                return self._finish(
                    "escalated", reason=f"model stopped without an answer: {response.stop_reason}"
                )

            if self.strict:
                rca, problems = validate_answer(response.turn.text, self.audit)
            else:
                rca, problems = parse_answer(response.turn.text)
            if rca is not None:
                return self._finish("rca", rca=rca)
            if not self.strict:
                return self._finish("escalated", reason="invalid answer: " + "; ".join(problems))
            self.audit.record("answer_rejected", problems=problems)
            if repairs_left == 0:
                return self._finish(
                    "escalated", reason="answer failed validation twice: " + "; ".join(problems)
                )
            repairs_left -= 1
            transcript.append(UserMessage(text=repair_message(problems)))

    async def _complete(
        self, transcript: list[TranscriptItem], tools: list[ToolSpec], tools_enabled: bool
    ) -> LLMResponse:
        """One model call, always written to the audit log, including when it fails."""
        llm_id = self.audit.next_id("L")
        t0 = time.monotonic()
        try:
            response = await self.llm.complete(
                self.system_prompt, transcript, tools, RCA_JSON_SCHEMA, tools_enabled
            )
        except Exception as exc:
            self.audit.record(
                "llm_error",
                id=llm_id,
                error=repr(exc),
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
            self._finish("escalated", reason=f"LLM call failed: {exc!r}")
            raise
        self.audit.record(
            "llm_call",
            id=llm_id,
            provider=response.turn.provider,
            model=response.model,
            stop_reason=response.stop_reason,
            usage={**response.usage.model_dump(), "total": response.usage.total},
            cost_usd=response.cost_usd,
            duration_ms=round((time.monotonic() - t0) * 1000),
            tools_enabled=tools_enabled,
            text=response.turn.text,
            tool_calls=[c.model_dump() for c in response.turn.tool_calls],
        )
        return response

    def _skip_tool(self, call_id: str, name: str) -> ToolResult:
        """Every tool call gets a result; calls past the budget get an explicit error."""
        message = "not run: the tool call budget for this investigation is used up"
        self.audit.record("tool_skipped", tool=name, call_id=call_id, reason=message)
        return ToolResult(call_id=call_id, content=message, is_error=True)

    async def _run_tool(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool_id = self.audit.next_id("T")
        denied = self.gate.check(name, self.hub.specs.get(name))
        if denied:
            self.audit.record(
                "tool_call",
                id=tool_id,
                tool=name,
                arguments=arguments,
                is_error=True,
                result=f"denied by policy: {denied}",
                policy="denied",
                duration_ms=0,
            )
            return ToolResult(
                call_id=call_id, content=f"[{tool_id}] denied by policy: {denied}", is_error=True
            )
        t0 = time.monotonic()
        text, is_error = await self.hub.call(name, arguments)
        self.audit.record(
            "tool_call",
            id=tool_id,
            tool=name,
            server=self.hub.specs[name].server,
            arguments=arguments,
            is_error=is_error,
            result=text,
            policy="allowed",
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        return ToolResult(call_id=call_id, content=f"[{tool_id}] {text}", is_error=is_error)

    def _tool_calls_used(self) -> int:
        return len(self.audit.tool_calls())

    def _exhausted(self, started: float) -> str | None:
        totals = self.audit.totals()
        if time.monotonic() - started > self.budget.max_seconds:
            return f"time limit of {self.budget.max_seconds:.0f}s"
        if totals["tokens"] > self.budget.max_tokens:
            return f"token limit of {self.budget.max_tokens}"
        if totals["cost_usd"] > self.budget.max_cost_usd:
            return f"cost limit of ${self.budget.max_cost_usd:.2f}"
        return None

    def _finish(
        self,
        status: Literal["rca", "escalated"],
        rca: RootCauseAnalysis | None = None,
        reason: str | None = None,
    ) -> InvestigationResult:
        result = InvestigationResult(
            status=status, rca=rca, escalation_reason=reason, totals=self.audit.totals()
        )
        self.audit.record("run_end", **result.model_dump(mode="json"))
        return result
