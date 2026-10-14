"""The LLM interface used by the investigator, plus pricing and provider fallback."""

from collections.abc import Sequence
from typing import Any, Protocol

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.models import LLMResponse, ToolSpec, TranscriptItem, Usage

# USD per million tokens: (input, output, cache read, cache write). The primary model plus the
# models the server-side fallback may route to. Where only input/output prices are published
# here, cache rates use the standard ratios (read 0.1x input, 5-minute write 1.25x input).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}


class LLMUnavailable(Exception):
    """The provider could not answer (network, timeout, rate limit, server error)."""


class LLM(Protocol):
    provider: str

    async def complete(
        self,
        system: str,
        transcript: Sequence[TranscriptItem],
        tools: Sequence[ToolSpec],
        output_schema: dict[str, Any],
        tools_enabled: bool = True,
    ) -> LLMResponse:
        """One model turn. With tools_enabled=False the model must answer without tools,
        but the tool definitions are still sent (the history contains tool calls)."""
        ...


def cost_usd(model: str, usage: Usage) -> float | None:
    """Price of one call, or None if the model has no configured price."""
    if model not in PRICES:
        return None
    price_in, price_out, price_read, price_write = PRICES[model]
    return (
        usage.input_tokens * price_in
        + usage.output_tokens * price_out
        + usage.cache_read_tokens * price_read
        + usage.cache_write_tokens * price_write
    ) / 1_000_000


class FallbackLLM:
    """Uses the primary provider until it is unavailable, then the secondary for the rest of
    the run. Configuration errors (bad key, bad request) are raised, never hidden."""

    def __init__(self, primary: LLM, secondary: LLM | None, audit: AuditLog) -> None:
        self.primary = primary
        self.secondary = secondary
        self.audit = audit
        self.active = primary

    @property
    def provider(self) -> str:
        return self.active.provider

    async def aclose(self) -> None:
        """Close both providers' HTTP clients."""
        for llm in (self.primary, self.secondary):
            close = getattr(llm, "aclose", None)
            if close is not None:
                await close()

    async def complete(
        self,
        system: str,
        transcript: Sequence[TranscriptItem],
        tools: Sequence[ToolSpec],
        output_schema: dict[str, Any],
        tools_enabled: bool = True,
    ) -> LLMResponse:
        try:
            return await self.active.complete(
                system, transcript, tools, output_schema, tools_enabled
            )
        except LLMUnavailable as exc:
            if self.secondary is None or self.active is self.secondary:
                raise
            self.audit.record(
                "provider_fallback",
                from_provider=self.active.provider,
                to_provider=self.secondary.provider,
                reason=str(exc),
            )
            self.active = self.secondary
            return await self.active.complete(
                system, transcript, tools, output_schema, tools_enabled
            )
