"""Same-Run waits cross the installed official MCP session boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from zenture._mcp.client import _PortalTransport  # pyright: ignore[reportPrivateUsage]
from zenture._mcp.transport import _OfficialAsyncMcpTransport  # pyright: ignore[reportPrivateUsage]
from zenture.errors import ZenturePollingTimeoutError
from zenture.mcp import AsyncMcpClient, McpClient

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@asynccontextmanager
async def connected(
    delay: float = 0,
) -> AsyncGenerator[tuple[_OfficialAsyncMcpTransport, list[str]], None]:
    server = MCPServer("wait-transport-test")
    reads: list[str] = []

    @server.tool()
    async def get_run(run_id: str, view: str, replay_limit: int) -> dict[str, object]:
        assert view == "summary"
        assert replay_limit == 50
        reads.append(run_id)
        await asyncio.sleep(delay)
        return {
            "run_id": run_id,
            "generation": 1,
            "family": "knowledge",
            "work_type": "answer",
            "profile": "standard",
            "status": "expired",
            "created_at": "2026-09-01T12:00:00Z",
            "updated_at": "2026-09-01T12:00:01Z",
            "artifact_refs": [],
        }

    async with InMemoryTransport(server) as streams, ClientSession(*streams) as session:
        await session.initialize()
        yield _OfficialAsyncMcpTransport(session), reads


@pytest.mark.asyncio
async def test_official_async_wait_returns_same_expired_run() -> None:
    async with connected() as (transport, reads):
        read = await AsyncMcpClient(transport).wait_run("run_observed", 2)
        assert read.run.run_id == "run_observed"
        assert read.run.status == "expired"
        assert reads == ["run_observed"]


def test_official_sync_portal_wait_returns_same_expired_run() -> None:
    with start_blocking_portal() as portal, portal.wrap_async_context_manager(connected()) as pair:
        transport, reads = pair
        read = McpClient(_PortalTransport(portal, transport)).wait_run("run_observed", 2)
        assert read.run.run_id == "run_observed"
        assert read.run.status == "expired"
        assert reads == ["run_observed"]


@pytest.mark.asyncio
async def test_official_session_read_deadline_expires_without_second_dispatch() -> None:
    async with connected(delay=0.1) as (transport, reads):
        with pytest.raises(ZenturePollingTimeoutError) as raised:
            await AsyncMcpClient(transport).wait_run("run_observed", 0.01)
        assert raised.value.operation_id == "run_observed"
        assert reads == ["run_observed"]
