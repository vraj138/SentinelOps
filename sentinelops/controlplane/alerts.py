"""Reads the firing alerts that open an incident."""

from mcp_servers.common import ToolError
from mcp_servers.metrics import PrometheusBackend
from sentinelops.controlplane.models import Alert


def firing_alerts(prometheus_url: str) -> tuple[str, list[Alert]]:
    """A description of the firing alerts for the agent, and the alerts themselves.

    If Prometheus can't be reached, the description says so plainly and the list is empty.
    """
    try:
        raw = PrometheusBackend(prometheus_url).alerts()
    except ToolError as exc:
        return f"Could not fetch alerts: {exc}. Investigate with the tools that work.", []
    firing = [a for a in raw if a.get("state") == "firing"]
    if not firing:
        return "No alerts are firing right now. Check whether anything is wrong.", []
    alerts = [
        Alert(name=a["labels"].get("alertname", ""), service=a["labels"].get("service"))
        for a in firing
    ]
    lines = [
        f"- {a['labels'].get('alertname')} on {a['labels'].get('service')} since "
        f"{a.get('activeAt')}: {a.get('annotations', {}).get('summary', '')}"
        for a in firing
    ]
    return "Firing alerts:\n" + "\n".join(lines), alerts
