"""Tests for MCP stdio concurrency lock (issue #1462)."""

import asyncio
from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.types import MCPServerConfig


async def test_lock_created_on_init() -> None:
    """_send_request must be serialized via asyncio.Lock."""
    config = MCPServerConfig(
        name="test", transport="stdio", command="echo",
        args=["{}"],
    )
    client = MCPStdioClient(config)
    assert hasattr(client, "_lock")
    assert isinstance(client._lock, asyncio.Lock)
