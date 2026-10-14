"""A scripted stand-in for the model, for dry runs of the benchmark harness (no API calls).

It reads the alerts, the deploy history and orders' container events, then always
recommends escalation. Its scores are
meaningless; it exists to prove the harness end to end (clean slate, alert wait, tools,
recording, scoring, report) and to time everything except the model.
"""

import json
from collections.abc import Sequence
from typing import Any

from sentinelops.agent.models import (
    AssistantTurn,
    LLMResponse,
    ToolCall,
    ToolSpec,
    TranscriptItem,
    Usage,
)


class ScriptedLLM:
    provider = "fake"

    def __init__(self) -> None:
        self.turn = 0

    async def complete(
        self,
        system: str,
        transcript: Sequence[TranscriptItem],
        tools: Sequence[ToolSpec],
        output_schema: dict[str, Any],
        tools_enabled: bool = True,
    ) -> LLMResponse:
        self.turn += 1
        if self.turn == 1:
            calls = [
                ToolCall(id="s1", name="list_alerts", arguments={}),
                ToolCall(id="s2", name="list_deploys", arguments={"since_minutes": 30}),
                ToolCall(id="s3", name="container_events", arguments={"service": "orders"}),
            ]
            turn = AssistantTurn(tool_calls=calls, provider="fake")
            stop = "tool_use"
        else:
            answer = {
                "summary": "dry run",
                "root_cause_service": "unknown",
                "root_cause": "dry run: no reasoning performed",
                "claims": [{"statement": "alerts and deploys were read", "evidence": ["T1", "T2"]}],
                "confidence": "low",
                "recommended_action": {
                    "kind": "escalate",
                    "service": None,
                    "flag": None,
                    "flag_value": None,
                    "reason": "dry run",
                },
                "data_gaps": ["scripted model"],
            }
            turn = AssistantTurn(text=json.dumps(answer), provider="fake")
            stop = "end_turn"
        return LLMResponse(
            turn=turn,
            stop_reason=stop,  # type: ignore[arg-type]
            usage=Usage(),
            model="scripted",
            cost_usd=0.0,
        )
