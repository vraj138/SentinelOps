"""Claude adapter for the investigator, using the official Anthropic SDK.

- Claude's own assistant turns are replayed verbatim (thinking blocks included); turns from
  another provider are rebuilt as text + tool_use blocks.
- The final answer is constrained with `output_config.format` (the RCA JSON schema).
- Server-side refusal fallback is enabled (`fallbacks: "default"`), and the whole prefix is
  prompt-cached because every turn resends the growing history.
"""

from collections.abc import Sequence
from typing import Any

import anthropic

from sentinelops.agent.llm import LLMUnavailable, cost_usd
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

FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP_REASONS: dict[str, StopReason] = {
    "tool_use": "tool_use",
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def to_messages(transcript: Sequence[TranscriptItem]) -> list[dict[str, Any]]:
    """Convert the neutral transcript to Messages API messages."""
    messages: list[dict[str, Any]] = []
    for item in transcript:
        if isinstance(item, UserMessage):
            messages.append({"role": "user", "content": item.text})
        elif isinstance(item, AssistantTurn):
            if item.provider == "anthropic" and item.raw is not None:
                content = item.raw
            else:
                content = ([{"type": "text", "text": item.text}] if item.text else []) + [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in item.tool_calls
                ]
            messages.append({"role": "assistant", "content": content})
        elif isinstance(item, ToolResults):
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in item.results
            ]
            messages.append({"role": "user", "content": blocks})
    return messages


NOT_ECHOED_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}


def replayable_blocks(content: list[Any]) -> list[Any]:
    """Blocks to keep for replay. After a server-side fallback, thinking and tool_use blocks
    that appear before the last `fallback` block must not be echoed back."""
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    return [
        b for i, b in enumerate(content) if i > boundary or b.type not in NOT_ECHOED_BEFORE_FALLBACK
    ]


def to_response(message: Any) -> LLMResponse:
    """Convert a Messages API response to the neutral LLMResponse."""
    content = replayable_blocks(list(message.content))
    text = "".join(b.text for b in content if b.type == "text")
    calls = [
        ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
        for b in content
        if b.type == "tool_use"
    ]
    raw = [b.model_dump(exclude_none=True) for b in content]
    u = message.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=u.cache_read_input_tokens or 0,
        cache_write_tokens=u.cache_creation_input_tokens or 0,
    )
    return LLMResponse(
        turn=AssistantTurn(text=text, tool_calls=calls, provider="anthropic", raw=raw),
        stop_reason=STOP_REASONS.get(message.stop_reason or "", "other"),
        usage=usage,
        model=message.model,
        cost_usd=cost_usd(message.model, usage),
    )


class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        effort: str,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 16000,
    ) -> None:
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic()
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        """Close the HTTP client while its event loop is still running."""
        await self.client.close()

    async def complete(
        self,
        system: str,
        transcript: Sequence[TranscriptItem],
        tools: Sequence[ToolSpec],
        output_schema: dict[str, Any],
        tools_enabled: bool = True,
    ) -> LLMResponse:
        tool_params = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        tool_choice = {"type": "auto"} if tools_enabled else {"type": "none"}
        try:
            message = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=to_messages(transcript),
                tools=tool_params,
                tool_choice=tool_choice,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": output_schema},
                },
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except (
            anthropic.APIConnectionError,  # includes timeouts
            anthropic.RateLimitError,
            anthropic.OverloadedError,
            anthropic.ServiceUnavailableError,
            anthropic.DeadlineExceededError,
            anthropic.InternalServerError,
        ) as exc:
            raise LLMUnavailable(f"anthropic: {exc!r}") from exc
        return to_response(message)
