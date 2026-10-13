"""Shared test helpers."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult

CallTool = Callable[[MCPServer, str, dict[str, Any]], CallToolResult]


@pytest.fixture
def call_tool() -> CallTool:
    """Call one tool on an MCP server in-process, through a real MCP client."""

    def _call(server: MCPServer, name: str, args: dict[str, Any]) -> CallToolResult:
        async def run() -> CallToolResult:
            async with Client(server) as client:
                return await client.call_tool(name, args)

        return asyncio.run(run())

    return _call


def text_of(result: CallToolResult) -> str:
    """The text content of a tool result."""
    return "".join(getattr(block, "text", "") for block in result.content)
