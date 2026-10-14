"""Tests for the Claude and OpenAI adapters, with fake SDK clients (no network)."""

import asyncio
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic.types.beta import BetaMessage
from openai.types.chat import ChatCompletion

from sentinelops.agent import anthropic_llm, openai_llm
from sentinelops.agent.anthropic_llm import AnthropicLLM
from sentinelops.agent.llm import LLMUnavailable, cost_usd
from sentinelops.agent.models import (
    RCA_JSON_SCHEMA,
    AssistantTurn,
    ToolCall,
    ToolResult,
    ToolResults,
    ToolSpec,
    Usage,
    UserMessage,
)

CLAUDE_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig123"},
        {"type": "text", "text": "Checking alerts."},
        {"type": "tool_use", "id": "toolu_1", "name": "list_alerts", "input": {}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 100,
    },
}
SPEC = ToolSpec(
    name="list_alerts",
    description="alerts",
    input_schema={"type": "object"},
    server="metrics",
    read_only=True,
)


def test_cost_uses_configured_prices() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4 + 20 + 0.2)
    assert cost_usd("unpriced-model", usage) is None


def test_claude_response_keeps_thinking_for_replay() -> None:
    response = anthropic_llm.to_response(BetaMessage.model_validate(CLAUDE_MESSAGE))

    assert response.stop_reason == "tool_use"
    assert response.turn.tool_calls == [ToolCall(id="toolu_1", name="list_alerts", arguments={})]
    assert response.turn.raw[0]["signature"] == "sig123"
    assert response.usage.cache_read_tokens == 5000
    assert response.cost_usd == pytest.approx((1000 * 4 + 200 * 20 + 5000 * 0.2 + 100 * 5) / 1e6)


def test_claude_messages_replay_own_turns_and_rebuild_others() -> None:
    own = anthropic_llm.to_response(BetaMessage.model_validate(CLAUDE_MESSAGE)).turn
    other = AssistantTurn(
        text="hi",
        tool_calls=[ToolCall(id="call_9", name="read_logs", arguments={"service": "x"})],
        provider="openai",
    )
    transcript = [
        UserMessage(text="go"),
        own,
        ToolResults(results=[ToolResult(call_id="toolu_1", content="[T1] none", is_error=True)]),
        other,
    ]

    messages = anthropic_llm.to_messages(transcript)

    assert messages[1]["content"] == own.raw
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "[T1] none",
        "is_error": True,
    }
    assert messages[3]["content"][1] == {
        "type": "tool_use",
        "id": "call_9",
        "name": "read_logs",
        "input": {"service": "x"},
    }


class FakeAnthropic:
    def __init__(self, error: Exception | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self.error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> BetaMessage:
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return BetaMessage.model_validate(CLAUDE_MESSAGE)


def test_claude_request_shape() -> None:
    client = FakeAnthropic()
    llm = AnthropicLLM("claude-opus-5-5", "high", client=client)  # type: ignore[arg-type]

    asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA))

    kwargs = client.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["output_config"]["format"]["schema"] == RCA_JSON_SCHEMA
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in kwargs
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert kwargs["tools"][0]["name"] == "list_alerts"


def test_claude_final_turn_sends_tools_with_choice_none() -> None:
    client = FakeAnthropic()
    llm = AnthropicLLM("claude-opus-5-5", "high", client=client)  # type: ignore[arg-type]

    asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA, False))

    assert client.kwargs["tool_choice"] == {"type": "none"}
    assert client.kwargs["tools"]


def test_claude_fallback_turn_drops_blocks_before_the_fallback_marker() -> None:
    message = dict(CLAUDE_MESSAGE)
    message["content"] = [
        {"type": "thinking", "thinking": "", "signature": "declined"},
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-4-8"},
            "trigger": {"type": "refusal", "category": None},
        },
        {"type": "thinking", "thinking": "", "signature": "kept"},
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "toolu_2", "name": "list_alerts", "input": {}},
    ]
    message["model"] = "claude-opus-4-8"

    response = anthropic_llm.to_response(BetaMessage.model_validate(message))

    signatures = [b.get("signature") for b in response.turn.raw if b["type"] == "thinking"]
    assert signatures == ["kept"]
    assert [c.id for c in response.turn.tool_calls] == ["toolu_2"]
    assert response.cost_usd is not None


def test_claude_overloaded_means_unavailable() -> None:
    request = httpx2.Request("POST", "https://api.example")
    error = anthropic.OverloadedError(
        "overloaded", response=httpx2.Response(529, request=request), body=None
    )
    llm = AnthropicLLM("claude-opus-5-5", "high", client=FakeAnthropic(error))  # type: ignore[arg-type]

    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA))


def test_claude_connection_error_means_unavailable() -> None:
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.example"))
    llm = AnthropicLLM("claude-opus-5-5", "high", client=FakeAnthropic(error))  # type: ignore[arg-type]

    with pytest.raises(LLMUnavailable, match="anthropic"):
        asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA))


def test_claude_auth_error_is_not_hidden_as_unavailable() -> None:
    request = httpx2.Request("POST", "https://api.example")
    error = anthropic.AuthenticationError(
        "bad key", response=httpx2.Response(401, request=request), body=None
    )
    llm = AnthropicLLM("claude-opus-5-5", "high", client=FakeAnthropic(error))  # type: ignore[arg-type]

    with pytest.raises(anthropic.AuthenticationError):
        asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA))


def test_openai_conversion_round_trip() -> None:
    completion = ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "some-openai-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_logs",
                                    "arguments": '{"service":"orders"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 40,
                "total_tokens": 340,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
        }
    )

    response = openai_llm.to_response(completion)
    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 200 and response.usage.cache_read_tokens == 100
    assert response.cost_usd is None

    results = ToolResults(
        results=[ToolResult(call_id="call_1", content="[T1] boom", is_error=True)]
    )
    messages = openai_llm.to_messages("sys", [UserMessage(text="go"), response.turn, results])
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == '{"service": "orders"}'
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ERROR: [T1] boom"}


def test_openai_connection_error_means_unavailable() -> None:
    async def create(**kwargs: Any) -> None:
        raise openai.APIConnectionError(request=httpx2.Request("POST", "https://api.example"))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm = openai_llm.OpenAILLM("m", client=client)  # type: ignore[arg-type]

    with pytest.raises(LLMUnavailable, match="openai"):
        asyncio.run(llm.complete("sys", [UserMessage(text="go")], [SPEC], RCA_JSON_SCHEMA))


def test_fallback_llm_closes_both_clients(tmp_path) -> None:
    from sentinelops.agent.audit import AuditLog
    from sentinelops.agent.llm import FallbackLLM

    closed: list[str] = []

    class Closable:
        def __init__(self, name: str) -> None:
            self.name = name
            self.provider = name

        async def aclose(self) -> None:
            closed.append(self.name)

    asyncio.run(FallbackLLM(Closable("a"), Closable("b"), AuditLog(tmp_path / "x.jsonl")).aclose())
    assert closed == ["a", "b"]
