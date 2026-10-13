"""OpenAI adapter, used only as a fallback when Claude is unavailable (Chat Completions API)."""

import json
from collections.abc import Sequence
from typing import Any

import openai

from sentinelops.agent.llm import LLMUnavailable
from sentinelops.agent.models import (
    AssistantTurn,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolResults,
    ToolSpec,
    TranscriptItem,
    Usage,
    UserMessage,
)

STOP_REASONS: dict[str, StopReason] = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_messages(system: str, transcript: Sequence[TranscriptItem]) -> list[dict[str, Any]]:
    """Convert the neutral transcript to Chat Completions messages."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for item in transcript:
        if isinstance(item, UserMessage):
            messages.append({"role": "user", "content": item.text})
        elif isinstance(item, AssistantTurn):
            message: dict[str, Any] = {"role": "assistant", "content": item.text or None}
            if item.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in item.tool_calls
                ]
            messages.append(message)
        elif isinstance(item, ToolResults):
            for r in item.results:
                content = f"ERROR: {r.content}" if r.is_error else r.content
                messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
    return messages


def to_response(completion: Any) -> LLMResponse:
    choice = completion.choices[0]
    calls = [
        ToolCall(id=c.id, name=c.function.name, arguments=json.loads(c.function.arguments or "{}"))
        for c in (choice.message.tool_calls or [])
    ]
    u = completion.usage
    cached = (u.prompt_tokens_details.cached_tokens or 0) if u.prompt_tokens_details else 0
    usage = Usage(
        input_tokens=u.prompt_tokens - cached,
        output_tokens=u.completion_tokens,
        cache_read_tokens=cached,
    )
    return LLMResponse(
        turn=AssistantTurn(text=choice.message.content or "", tool_calls=calls, provider="openai"),
        stop_reason=STOP_REASONS.get(choice.finish_reason, "other"),
        usage=usage,
        model=completion.model,
        cost_usd=None,  # OpenAI prices are not configured; reported as unknown, never guessed
    )


class OpenAILLM:
    provider = "openai"

    def __init__(self, model: str, client: openai.AsyncOpenAI | None = None) -> None:
        self.model = model
        self.client = client or openai.AsyncOpenAI()

    async def complete(
        self,
        system: str,
        transcript: Sequence[TranscriptItem],
        tools: Sequence[ToolSpec],
        output_schema: dict[str, Any],
        tools_enabled: bool = True,
    ) -> LLMResponse:
        tool_params = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        try:
            completion = await self.client.chat.completions.create(
                model=self.model,
                messages=to_messages(system, transcript),
                tools=tool_params or openai.NOT_GIVEN,
                tool_choice=("auto" if tools_enabled else "none")
                if tool_params
                else openai.NOT_GIVEN,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "root_cause_analysis", "schema": output_schema},
                },
                max_completion_tokens=16000,
            )
        except (
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        ) as exc:
            raise LLMUnavailable(f"openai: {exc!r}") from exc
        return to_response(completion)
