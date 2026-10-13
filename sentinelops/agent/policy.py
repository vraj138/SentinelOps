"""The policy gate: decides whether the agent may call a tool, before the call happens.

During an investigation only read-only diagnostic tools are allowed. A tool must be both on
the allowlist and declared read-only by its server, so adding a new tool to a server does not
silently give the agent new powers.
"""

from sentinelops.agent.models import ToolSpec

DIAGNOSTIC_TOOLS = frozenset(
    {
        "query_metrics",
        "query_metrics_range",
        "list_alerts",
        "list_containers",
        "container_events",
        "read_logs",
        "list_deploys",
        "show_deploy",
        "get_feature_flags",
        "search_runbooks",
        "read_runbook",
    }
)


class PolicyGate:
    def __init__(self, allowed: frozenset[str] = DIAGNOSTIC_TOOLS) -> None:
        self.allowed = allowed

    def check(self, name: str, spec: ToolSpec | None) -> str | None:
        """Return None if the call is allowed, otherwise the reason it is denied."""
        if spec is None:
            return f"unknown tool {name!r}"
        if name not in self.allowed:
            return f"tool {name!r} is not allowed during an investigation"
        if not spec.read_only:
            return f"tool {name!r} is not declared read-only by its server"
        return None
