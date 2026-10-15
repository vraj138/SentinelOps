"""The audit log: one JSONL record per LLM call, tool call, policy decision and run event.

The same file is the citation target (tool call ids like "T3"), the audit trail, and the
recording that replay mode re-runs (`python -m sentinelops.evals replay`). Each record is
written and flushed as it happens, so a crash mid-run still leaves everything up to that point
on disk.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[dict[str, Any]] = []
        self._counters = {"T": 0, "L": 0}

    @classmethod
    def load(cls, path: Path) -> "AuditLog":
        """An existing audit log, read back from disk (for scoring recorded runs)."""
        log = cls(path)
        log.records = [json.loads(line) for line in path.read_text().splitlines() if line]
        return log

    def next_id(self, prefix: str) -> str:
        """Next sequential id: T1, T2, ... for tool calls; L1, L2, ... for LLM calls."""
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]}"

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "kind": kind, **fields}
        self.records.append(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def tool_calls(self) -> dict[str, dict[str, Any]]:
        """Tool call records by id."""
        return {r["id"]: r for r in self.records if r["kind"] == "tool_call"}

    def totals(self) -> dict[str, Any]:
        """Run totals. cost_usd sums the priced calls only, so with unpriced calls it is a
        lower bound; unpriced_llm_calls says how many were not priced."""
        llm = [r for r in self.records if r["kind"] == "llm_call"]
        return {
            "llm_calls": len(llm),
            "tool_calls": len(self.tool_calls()),
            "tokens": sum(r["usage"]["total"] for r in llm),
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in llm), 4),
            "unpriced_llm_calls": sum(1 for r in llm if r["cost_usd"] is None),
        }
