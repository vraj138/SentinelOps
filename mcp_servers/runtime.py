"""Runtime tool server: read-only container state and lifecycle events (crashes, OOM kills)."""

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_servers.common import SERVICES, ToolError, to_result
from mcp_servers.docker_backend import DockerBackend
from sentinelops.config import Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_server(backend: DockerBackend) -> MCPServer:
    """The runtime MCP server, backed by `backend`."""
    server = MCPServer(
        "runtime",
        instructions=f"Read-only container state for the services {list(SERVICES)}.",
    )

    @server.tool(
        description="Status of every service container: running, health, restarts, image, start.",
        annotations=READ_ONLY,
    )
    def list_containers() -> str:
        statuses, errors = [], {}
        for service in SERVICES:
            try:
                statuses.append(backend.status(service))
            except ToolError as exc:
                errors[service] = str(exc)
        return to_result({"containers": statuses, "errors": errors})

    @server.tool(
        description=(
            "Lifecycle events for one service container in the last `since_minutes` (max 120): "
            "create, start, restart, die (with exitCode; 137 = killed), oom, kill, stop."
        ),
        annotations=READ_ONLY,
    )
    def container_events(service: str, since_minutes: int = 30) -> str:
        if not 1 <= since_minutes <= 120:
            raise ToolError("since_minutes must be between 1 and 120")
        events = backend.events(service, since_minutes)
        return to_result({"service": service, "since_minutes": since_minutes, "events": events})

    return server


if __name__ == "__main__":
    settings = Settings()
    backend = DockerBackend(
        settings.docker_bin, settings.container_name_template, settings.container_events_path
    )
    build_server(backend).run()
