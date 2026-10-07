"""SDK catalog parity across the actual Run registration and official MCP session."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
from anyio.from_thread import start_blocking_portal
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from zenture._mcp.client import _PortalTransport  # pyright: ignore[reportPrivateUsage]
from zenture._mcp.transport import _OfficialAsyncMcpTransport  # pyright: ignore[reportPrivateUsage]
from zenture.mcp import AsyncMcpClient, McpClient, McpToolDefinition


@pytest.fixture
def registration(monkeypatch: pytest.MonkeyPatch) -> Any:
    source = Path(__file__).resolve().parents[4] / "zenture-mcp/services/mcp-server/src"
    if not source.is_dir():
        pytest.skip("actual MCP registration source requires an adjacent workspace checkout")
    monkeypatch.syspath_prepend(str(source))  # pyright: ignore[reportUnknownMemberType]
    return importlib.import_module("zenture_mcp_server.run_tools").register_run_tools


class NoProductCalls:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("catalog discovery must not invoke a product client")


@asynccontextmanager
async def initialized(
    registration: Any,
    attach: bool,
) -> AsyncGenerator[tuple[_OfficialAsyncMcpTransport, dict[str, dict[str, Any]]]]:
    server = MCPServer("registration-catalog-test")

    async def resolver(*_args: object) -> None:
        raise AssertionError("catalog discovery must not resolve an artifact")

    registration(server, NoProductCalls(), artifact_source_resolver=resolver if attach else None)
    expected = {
        tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in await server.list_tools()
    }
    async with InMemoryTransport(server) as streams, ClientSession(*streams) as session:
        await session.initialize()
        yield _OfficialAsyncMcpTransport(session), expected


def assert_registration(
    catalog: tuple[McpToolDefinition, ...],
    expected: dict[str, dict[str, Any]],
    attach: bool,
) -> None:
    assert {tool.name for tool in catalog} == set(expected)
    assert ("attach_artifact" in expected) is attach
    for tool in catalog:
        actual = expected[tool.name]
        assert tool.title == actual.get("title")
        assert tool.description == actual.get("description")
        assert tool.input_schema == actual["inputSchema"]
        assert tool.output_schema == actual.get("outputSchema")
        assert tool.annotations == actual.get("annotations")


@pytest.mark.asyncio
@pytest.mark.parametrize("attach", [False, True])
async def test_async_catalog_matches_actual_run_registration(
    registration: Any, attach: bool
) -> None:
    async with initialized(registration, attach) as (transport, expected):
        client = AsyncMcpClient(transport)
        catalog = await client.get_tool_catalog()
        assert_registration(catalog, expected, attach)
        assert await client.list_tools() == tuple(tool.name for tool in catalog)


@pytest.mark.parametrize("attach", [False, True])
def test_sync_portal_catalog_matches_actual_run_registration(
    registration: Any, attach: bool
) -> None:
    with (
        start_blocking_portal() as portal,
        portal.wrap_async_context_manager(initialized(registration, attach)) as (
            transport,
            expected,
        ),
    ):
        client = McpClient(_PortalTransport(portal, transport))
        catalog = client.get_tool_catalog()
        assert_registration(catalog, expected, attach)
        assert client.list_tools() == tuple(tool.name for tool in catalog)
