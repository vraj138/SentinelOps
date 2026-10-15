"""Plain data for the templates: titles, the state stepper, run statistics, chart windows.

Nothing here renders HTML, so the templates' autoescaping covers every value.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sentinelops.controlplane.models import TERMINAL, Incident, State

HAPPY_PATH = [
    State.OPEN,
    State.INVESTIGATING,
    State.PROPOSED,
    State.APPROVED,
    State.EXECUTING,
    State.VERIFYING,
    State.RESOLVED,
]
BUSY = {State.INVESTIGATING, State.APPROVED, State.EXECUTING, State.VERIFYING}
STATE_TONE = {
    State.SELF_RESOLVED: "ok",
    State.OPEN: "neutral",
    State.INVESTIGATING: "info",
    State.PROPOSED: "warn",
    State.APPROVED: "info",
    State.EXECUTING: "info",
    State.VERIFYING: "info",
    State.RESOLVED: "ok",
    State.ESCALATED: "bad",
}
STATUS_TONE = {"healthy": "ok", "degraded": "warn", "down": "bad"}
ALERT_PANEL = {
    "ServiceDown": "requests",
    "HighErrorRate": "errors",
    "HighLatencyP95": "latency",
    "HighMemoryUsage": "memory",
    "FrequentRestarts": "restarts",
}
# Grafana's classic palette: distinguishable on dark and light backgrounds.
PALETTE = ["#73bf69", "#f2cc0c", "#8ab8ff", "#ff780a", "#f2495c", "#5794f2", "#b877d9"]


def audit_records(runs_dir: Path, incident_id: str) -> list[dict[str, Any]]:
    path = runs_dir / incident_id / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def latest_run(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records of the most recent investigation run (an incident can be re-investigated)."""
    starts = [i for i, r in enumerate(records) if r.get("kind") == "investigation_started"]
    if not starts:
        starts = [i for i, r in enumerate(records) if r.get("kind") == "run_start"]
    return records[starts[-1] :] if starts else records


def run_stats(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the latest investigation cost: model calls, tool calls, tokens, dollars, seconds.
    `total_cost_usd` and `runs` cover every run on the incident."""
    all_llm = [r for r in records if r.get("kind") == "llm_call"]
    run = latest_run(records)
    llm = [r for r in run if r.get("kind") == "llm_call"]
    if not llm:
        return None
    tools = [r for r in run if r.get("kind") == "tool_call"]
    starts = [r["ts"] for r in run if r.get("kind") == "run_start"]
    ends = [r["ts"] for r in run if r.get("kind") == "run_end"]
    seconds = None
    if starts and ends:
        seconds = (
            datetime.fromisoformat(ends[-1]) - datetime.fromisoformat(starts[0])
        ).total_seconds()
    return {
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "tool_errors": sum(1 for r in tools if r.get("is_error")),
        "tokens": sum(r["usage"]["total"] for r in llm),
        "cost_usd": sum(r["cost_usd"] or 0 for r in all_llm),
        "unpriced": sum(1 for r in all_llm if r["cost_usd"] is None),
        "seconds": seconds,
        "models": sorted({r["model"] for r in llm}),
        "runs": max(1, sum(1 for r in records if r.get("kind") == "run_start")),
    }


def tool_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls of the latest run: its evidence ids (T1, T2, ...) restart with every run."""
    return [r for r in latest_run(records) if r.get("kind") == "tool_call"]


def state_label(incident: Incident) -> tuple[str, str]:
    """(badge text, tone): an OPEN incident shows whether its investigation is queued or waiting."""
    info = incident.investigation
    if incident.state == State.OPEN and info.status in ("queued", "waiting"):
        return info.status.upper(), "info" if info.status == "queued" else "warn"
    return incident.state.value.replace("_", " "), STATE_TONE[incident.state]


def alert_groups(incident: Incident) -> list[tuple[str, list[str]]]:
    """Firing alerts grouped by name: [('HighErrorRate', ['gateway', 'orders'])]."""
    by_name: dict[str, list[str]] = {}
    for alert in incident.alerts:
        services = by_name.setdefault(alert.name, [])
        if (alert.service or "?") not in services:
            services.append(alert.service or "?")
    return [(name, sorted(services)) for name, services in by_name.items()]


def incident_title(incident: Incident) -> str:
    """'HighErrorRate on gateway, orders', or the first line of the context if no alerts."""
    if not incident.alerts:
        first = incident.alert_context.strip().splitlines()[0] if incident.alert_context else ""
        return first[:90] or "Incident"
    parts = [f"{name} on {', '.join(services)}" for name, services in alert_groups(incident)]
    return parts[0] if len(parts) == 1 else f"{parts[0]} +{len(parts) - 1} more"


def stepper(incident: Incident) -> list[dict[str, str]]:
    """Each lifecycle step as done, current, skipped or todo; an incident that ended off the
    happy path (escalated or self-resolved) ends with that step. Only the latest attempt counts:
    a re-investigation starts again from OPEN."""
    history = incident.history
    reopened = [i for i, h in enumerate(history) if State(h["to"]) == State.OPEN]
    if reopened:
        history = history[reopened[-1] + 1 :]
    visited = {State.OPEN} | {State(h["to"]) for h in history}
    ended = {State.ESCALATED: "failed", State.SELF_RESOLVED: "ended"}.get(incident.state)
    last_before = State(history[-1]["from"]) if ended and history else None
    path = HAPPY_PATH[: HAPPY_PATH.index(last_before) + 1] if last_before else HAPPY_PATH
    reached = max(HAPPY_PATH.index(s) for s in visited if s in HAPPY_PATH)
    steps = []
    for i, state in enumerate(path):
        if state == incident.state:
            status = "current"
        elif state in visited:
            status = "done"
        elif i < reached:
            status = "skipped"
        else:
            status = "todo"
        steps.append({"state": state.value, "label": state.value.title(), "status": status})
    if ended:
        label = incident.state.value.replace("_", "-").capitalize()
        steps.append({"state": incident.state.value, "label": label, "status": ended})
    return steps


def duration(start: datetime, end: datetime) -> str:
    return human_seconds((end - start).total_seconds())


def human_seconds(value: float) -> str:
    """42s, 4m 05s, 3h 07m, 2d 4h."""
    seconds = max(0, int(value))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def incident_duration(incident: Incident, now: datetime | None = None) -> str:
    end = incident.updated_at if incident.state in TERMINAL else (now or datetime.now(UTC))
    return duration(incident.created_at, end)


def incident_charts(
    incident: Incident, allowed: set[str], now: datetime | None = None
) -> dict[str, Any] | None:
    """Which panels to chart for an incident, for which services, over which window."""
    services = sorted({a.service for a in incident.alerts if a.service in allowed})
    if not services:
        return None
    panels = list(dict.fromkeys(ALERT_PANEL.get(a.name, "errors") for a in incident.alerts))
    for extra in ("errors", "latency", "requests"):
        if len(panels) >= 2:
            break
        if extra not in panels:
            panels.append(extra)
    end = incident.updated_at if incident.state in TERMINAL else (now or datetime.now(UTC))
    end = end + timedelta(minutes=5)
    start = max(incident.created_at - timedelta(minutes=15), end - timedelta(hours=24))
    return {
        "panels": panels,
        "services": services,
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "marker": int(incident.created_at.timestamp()),
    }


def diff_lines(dry_run: str) -> list[dict[str, str]]:
    """Dry-run text split into lines tagged add / del / ctx for diff colouring."""
    tone = {"+": "add", "-": "del"}
    return [
        {"tone": tone.get(line[:1], "ctx") if line[1:2] == " " else "ctx", "text": line}
        for line in dry_run.splitlines()
    ]


def service_colors(services: list[str]) -> dict[str, str]:
    return {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(services)}
