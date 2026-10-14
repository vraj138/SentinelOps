"""Data models for the agent: a provider-neutral transcript, LLM responses, and the RCA.

The transcript is append-only. An assistant turn keeps the provider's raw content so it can be
sent back to that provider unchanged (Claude's thinking blocks must be replayed exactly); if a
different provider continues the run, it rebuilds the turn from the neutral fields instead.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

SERVICES = ("gateway", "orders", "payments")
Provider = Literal["anthropic", "openai", "fake"]
StopReason = Literal["tool_use", "end_turn", "max_tokens", "refusal", "other"]


class ToolCall(BaseModel):
    id: str  # the provider's id for this call, echoed back with the result
    name: str
    arguments: dict[str, Any]


class UserMessage(BaseModel):
    kind: Literal["user"] = "user"
    text: str


class AssistantTurn(BaseModel):
    kind: Literal["assistant"] = "assistant"
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider: Provider
    raw: Any = None  # provider-native content, replayed verbatim to the same provider


class ToolResult(BaseModel):
    call_id: str
    content: str
    is_error: bool = False


class ToolResults(BaseModel):
    kind: Literal["tool_results"] = "tool_results"
    results: list[ToolResult]


TranscriptItem = UserMessage | AssistantTurn | ToolResults


class ToolSpec(BaseModel):
    """A tool as offered to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    read_only: bool


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class LLMResponse(BaseModel):
    turn: AssistantTurn
    stop_reason: StopReason
    usage: Usage
    model: str
    cost_usd: float | None  # None when the model's price is not configured


class Claim(BaseModel):
    statement: str
    evidence: list[str]  # tool call ids such as "T3"


class RecommendedAction(BaseModel):
    kind: Literal["rollback", "restart", "set_flag", "escalate"]
    service: Literal["gateway", "orders", "payments"] | None = None
    flag: str | None = None
    flag_value: bool | None = None
    reason: str


class RootCauseAnalysis(BaseModel):
    """The agent's final answer."""

    summary: str
    root_cause_service: Literal["gateway", "orders", "payments", "postgres", "external", "unknown"]
    root_cause: str
    claims: list[Claim]
    confidence: Literal["low", "medium", "high"]
    recommended_action: RecommendedAction
    data_gaps: list[str]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


RCA_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "root_cause_service",
        "root_cause",
        "claims",
        "confidence",
        "recommended_action",
        "data_gaps",
    ],
    "properties": {
        "summary": {"type": "string"},
        "root_cause_service": {
            "type": "string",
            "enum": ["gateway", "orders", "payments", "postgres", "external", "unknown"],
        },
        "root_cause": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement", "evidence"],
                "properties": {
                    "statement": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "recommended_action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "service", "flag", "flag_value", "reason"],
            "properties": {
                "kind": {"type": "string", "enum": ["rollback", "restart", "set_flag", "escalate"]},
                "service": _nullable({"type": "string", "enum": list(SERVICES)}),
                "flag": _nullable({"type": "string"}),
                "flag_value": _nullable({"type": "boolean"}),
                "reason": {"type": "string"},
            },
        },
        "data_gaps": {"type": "array", "items": {"type": "string"}},
    },
}
