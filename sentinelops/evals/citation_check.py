"""Would each recorded answer pass SentinelOps' evidence check? Free: re-checks recordings.

SentinelOps refuses an answer whose citations are not ids of successful tool calls from the same
investigation, and asks the model to fix it. The naive agent has no such check, so this measures
how often its answers could not be verified automatically. Rejected citations are split into
malformed ones (text that starts with a real tool id, like "T6: 5xx ratio is 0.93") and
unsupported ones (no tool result behind them, such as a missing id or "Alerts: ...").
"""

import json
import re
from pathlib import Path
from typing import Any

from sentinelops.agent.audit import AuditLog
from sentinelops.agent.investigator import validate_answer

RECORDING_SUFFIX = {"sentinelops": "", "naive-agent": "-naive"}
LEADING_ID = re.compile(r"^\[?(T\d+)\b")


def check(recording: Path) -> dict[str, Any] | None:
    """Citation counts and the check's verdict for one recorded answer (None if no answer)."""
    rca = json.loads((recording / "result.json").read_text()).get("rca")
    if rca is None:
        return None
    audit = AuditLog.load(recording / "audit.jsonl")
    calls = audit.tool_calls()

    def ok(ref: str) -> bool:
        return ref in calls and not calls[ref]["is_error"]

    refs = [ref for claim in rca["claims"] for ref in claim["evidence"]]
    rejected = [ref for ref in refs if not ok(ref)]
    # "T6: 5xx ratio is about 0.93" names a real result, so a person can trace it; the check can't.
    malformed = [ref for ref in rejected if (m := LEADING_ID.match(ref)) and ok(m.group(1))]
    _, problems = validate_answer(json.dumps(rca), audit)
    return {
        "passed": not problems,
        "citations": len(refs),
        "malformed_citations": len(malformed),
        "unsupported_citations": len(rejected) - len(malformed),
    }


def score(results_path: Path, recordings_dir: Path) -> None:
    """Add an `evidence_check` field to every agent run that has a recording."""
    rows = [json.loads(line) for line in results_path.read_text().splitlines() if line]
    for r in rows:
        suffix = RECORDING_SUFFIX.get(r["system"])
        if suffix is None or not r.get("manifested") or r.get("status") == "error":
            continue
        recording = recordings_dir / f"{r['run_id']}{suffix}"
        if (recording / "result.json").exists():
            r["evidence_check"] = check(recording)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
