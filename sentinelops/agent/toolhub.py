"""Connects to the MCP tool servers and routes tool calls to the right one.

In production each server runs as a subprocess over stdio. Tests pass `MCPServer` objects,
which the MCP client connects to in-process.
"""

import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

from sentinelops.agent.models import ToolSpec
from sentinelops.config import ROOT

SERVER_MODULES = ("metrics", "runtime", "logs", "changes", "runbooks")
ServerSource = MCPServer | StdioServerParameters


def stdio_servers() -> dict[str, ServerSource]:
    """Launch each tool server as `python -m mcp_servers.<name>` from the repo root."""
    return {
        name: StdioServerParameters(
            command=sys.executable,
            args=["-m", f"mcp_servers.{name}"],
            env=dict(os.environ),
            cwd=ROOT,
        )
        for name in SERVER_MODULES
    }


class ToolHub:
    """Holds one MCP client per server for the length of an investigation."""

    def __init__(self, sources: dict[str, ServerSource]) -> None:
        self.sources = sources
        self.specs: dict[str, ToolSpec] = {}
        self._clients: dict[str, Client] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "ToolHub":
        for server_name, source in self.sources.items():
            client = await self._stack.enter_async_context(Client(source))
            self._clients[server_name] = client
            listing = await client.list_tools()
            for tool in listing.tools:
                if tool.name in self.specs:
                    raise ValueError(f"tool {tool.name!r} is defined by two servers")
                annotations = tool.annotations
                self.specs[tool.name] = ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    server=server_name,
                    read_only=bool(annotations and annotations.read_only_hint),
                )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Call a tool. Returns (text, is_error). Transport failures become error results."""
        spec = self.specs[name]
        try:
            result = await self._clients[spec.server].call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the model and the audit log
            return f"tool server {spec.server!r} failed: {exc!r}", True
        text = "".join(getattr(block, "text", "") for block in result.content)
        return text, bool(result.is_error)
