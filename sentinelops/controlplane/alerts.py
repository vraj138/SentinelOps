"""Reads the firing alerts that open an incident, and checks whether they are still firing."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp_servers.common import ToolError
from mcp_servers.metrics import PrometheusBackend
from sentinelops.controlplane.models import Alert, Incident


def parse_alert(raw: dict[str, Any]) -> Alert:
    since = raw.get("activeAt")
    return Alert(
        name=raw["labels"].get("alertname", ""),
        service=raw["labels"].get("service"),
        since=datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None,
        summary=raw.get("annotations", {}).get("summary", ""),
    )


def describe(alerts: list[Alert]) -> str:
    """The alert text an investigation starts from."""
    if not alerts:
        return "No alerts are firing right now. Check whether anything is wrong."
    lines = [
        f"- {a.name} on {a.service} since {a.since.isoformat() if a.since else 'unknown'}"
        f"{': ' + a.summary if a.summary else ''}"
        for a in alerts
    ]
    return "Firing alerts:\n" + "\n".join(lines)


def firing_alerts(prometheus_url: str) -> tuple[str, list[Alert]]:
    """A description of the firing alerts for the agent, and the alerts themselves.

    If Prometheus can't be reached, the description says so plainly and the list is empty.
    """
    try:
        raw = PrometheusBackend(prometheus_url).alerts()
    except ToolError as exc:
        return f"Could not fetch alerts: {exc}. Investigate with the tools that work.", []
    alerts = [parse_alert(a) for a in raw if a.get("state") == "firing"]
    return describe(alerts), alerts


def still_firing_checker(
    prometheus_url: str,
) -> Callable[[Incident], bool | None]:
    """A check for whether any of an incident's alerts still fires: True, False, or None when
    Prometheus can't be reached (unknown is never treated as resolved)."""

    def check(incident: Incident) -> bool | None:
        keys = {a.key for a in incident.alerts}
        if not keys:
            return None
        try:
            raw = PrometheusBackend(prometheus_url).alerts()
        except ToolError:
            return None
        return any(parse_alert(a).key in keys for a in raw if a.get("state") == "firing")

    return check
