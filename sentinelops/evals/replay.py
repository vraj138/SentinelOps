"""Replay a recorded run: the agent sees exactly the tool results it saw live, without the
stack running. Used to re-test prompt or model changes against fixed evidence.

A call is answered from the recording when the same tool was called with the same arguments.
Anything else gets an explicit error, so the agent knows the data is not in the recording.
"""

import json
from pathlib import Path
from typing import Any

from sentinelops.agent.models import ToolSpec


def _key(tool: str, arguments: dict[str, Any]) -> str:
    return tool + json.dumps(arguments, sort_keys=True)


class ReplayHub:
    """Drop-in for ToolHub, serving results from a recording directory."""

    def __init__(self, recording_dir: Path) -> None:
        self.specs = {
            s["name"]: ToolSpec.model_validate(s)
            for s in json.loads((recording_dir / "tools.json").read_text())
        }
        self.results: dict[str, list[tuple[str, bool]]] = {}
        for line in (recording_dir / "audit.jsonl").read_text().splitlines():
            record = json.loads(line)
            if record.get("kind") == "tool_call" and record.get("policy") == "allowed":
                key = _key(record["tool"], record.get("arguments", {}))
                self.results.setdefault(key, []).append((record["result"], record["is_error"]))

    async def __aenter__(self) -> "ReplayHub":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        recorded = self.results.get(_key(name, arguments))
        if not recorded:
            return "not in recording: this call was not made during the live run", True
        return recorded.pop(0) if len(recorded) > 1 else recorded[0]
