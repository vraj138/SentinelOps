"""Logs tool server: recent, redacted log lines for one service."""

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_servers.common import SERVICES, ToolError, cap, redact
from mcp_servers.docker_backend import DockerBackend
from sentinelops.config import Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
MAX_LINES = 200
HEALTH_NOISE = ('"GET /health ', '"GET /metrics ')


def build_server(backend: DockerBackend) -> MCPServer:
    """The logs MCP server, backed by `backend`."""
    server = MCPServer(
        "logs",
        instructions=f"Read-only logs for the services {list(SERVICES)}. Secrets are redacted.",
    )

    @server.tool(
        description=(
            "Recent log lines for one service. `since_minutes` max 120, `max_lines` max 200. "
            "`contains` keeps only lines containing that text (case-insensitive). Health-check "
            "and metrics-scrape access lines are dropped. Logs of a container that was "
            "recreated (deploy, rollback) start at its new start time."
        ),
        annotations=READ_ONLY,
    )
    def read_logs(
        service: str, since_minutes: int = 15, max_lines: int = 100, contains: str = ""
    ) -> str:
        if not 1 <= since_minutes <= 120:
            raise ToolError("since_minutes must be between 1 and 120")
        if not 1 <= max_lines <= MAX_LINES:
            raise ToolError(f"max_lines must be between 1 and {MAX_LINES}")
        raw = backend.logs(service, since_minutes, tail=2000)
        lines = [line for line in raw.splitlines() if not any(n in line for n in HEALTH_NOISE)]
        if contains:
            lines = [line for line in lines if contains.lower() in line.lower()]
        shown = lines[-max_lines:]
        header = f"{service}: {len(shown)} of {len(lines)} matching lines (last {since_minutes}m)"
        return cap(redact("\n".join([header, *shown])))

    return server


if __name__ == "__main__":
    settings = Settings()
    backend = DockerBackend(
        settings.docker_bin, settings.container_name_template, settings.container_events_path
    )
    build_server(backend).run()
