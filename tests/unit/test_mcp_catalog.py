"""Observable discovery contracts for SDK-owned complete MCP definitions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.types import PaginatedRequestParams

from zenture.errors import ZentureMCPProtocolError
from zenture.mcp import AsyncMcpClient, McpClient, McpToolDefinition


class CatalogTransport:
    def __init__(self, pages: Sequence[object]) -> None:
        self.pages = pages
        self.cursors: list[str | None] = []

    def list_tools(self, *, cursor: str | None = None) -> object:
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]

    def call_tool(self, name: str, arguments: object) -> object:
        raise AssertionError("discovery must not call product tools")


class AsyncCatalogTransport(CatalogTransport):
    async def list_tools(self, *, cursor: str | None = None) -> object:
        return super().list_tools(cursor=cursor)

    async def call_tool(self, name: str, arguments: object) -> object:
        raise AssertionError("discovery must not call product tools")


def discover(pages: Sequence[object], asynchronous: bool) -> tuple[McpToolDefinition, ...]:
    if asynchronous:
        return asyncio.run(AsyncMcpClient(AsyncCatalogTransport(pages)).get_tool_catalog())
    return McpClient(CatalogTransport(pages)).get_tool_catalog()


def tool(name: str = "run") -> dict[str, Any]:
    return {
        "name": name,
        "title": "Run",
        "description": "Registered usage",
        "inputSchema": {
            "type": "object",
            "$defs": {"Task": {"maxLength": 20}},
            "properties": {"task": {"$ref": "#/$defs/Task"}},
        },
        "outputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "extension": {"values": [1, None]}},
    }


def assert_detached_metadata(
    definition: McpToolDefinition,
    record: Any,
    raw: dict[str, Any],
    model: bool,
) -> None:
    snapshot = deepcopy(definition.input_schema)
    if model:
        record.input_schema["type"] = "array"
    else:
        raw["inputSchema"]["type"] = "array"
    assert definition.input_schema == snapshot
    output_snapshot = deepcopy(definition.output_schema)
    annotations_snapshot = deepcopy(definition.annotations)
    if model:
        record.output_schema["type"] = "array"
        record.annotations.read_only_hint = True
    else:
        raw["outputSchema"]["type"] = "array"
        raw["annotations"]["extension"]["values"].append(2)
    assert definition.output_schema == output_snapshot
    assert definition.annotations == annotations_snapshot
    definition.input_schema["type"] = "string"
    assert (record.input_schema if model else raw["inputSchema"])["type"] == "array"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("model", [False, True])
def test_complete_catalog_preserves_registered_json_and_public_type(
    asynchronous: bool, model: bool
) -> None:
    import zenture.mcp

    assert callable(getattr(McpClient, "get_tool_catalog", None)), (
        "public full catalog discovery must be available"
    )
    from mcp.types import ListToolsResult, Tool, ToolAnnotations

    raw = tool()
    record: Any = Tool(**raw) if model else raw
    page = ListToolsResult(tools=[record]) if model else {"tools": [record]}
    result = discover([page], asynchronous)
    assert len(result) == 1
    definition = result[0]
    assert isinstance(definition, zenture.mcp.McpToolDefinition)
    assert (definition.name, definition.title, definition.description) == (
        "run",
        "Run",
        "Registered usage",
    )
    assert definition.input_schema == raw["inputSchema"]
    assert definition.output_schema == raw["outputSchema"]
    # Official annotations supply standard defaults in their JSON representation.
    expected = (
        ToolAnnotations(**raw["annotations"]).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        if model
        else raw["annotations"]
    )
    assert definition.annotations == expected
    assert_detached_metadata(definition, record, raw, model)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_absent_optional_metadata_and_attach_remain_absent(asynchronous: bool) -> None:
    result = discover([{"tools": [{"name": "run", "inputSchema": dict[str, object]()}]}], asynchronous)
    assert [item.name for item in result] == ["run"]
    assert (
        result[0].title,
        result[0].description,
        result[0].output_schema,
        result[0].annotations,
    ) == (None, None, None, None)
    assert discover([{"tools": list[object]()}], asynchronous) == ()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("page", "reason"),
    [
        ({"tools": [tool(), tool()]}, "duplicate_tool_name"),
        ({"tools": [{"name": "run"}]}, "invalid_tool_catalog"),
        ({"tools": [{**tool(), "inputSchema": list[object]()}]}, "invalid_tool_catalog"),
        ({"tools": [{**tool(), "outputSchema": "raw private data"}]}, "invalid_tool_catalog"),
        ({"tools": [{**tool(), "annotations": {"bad": object()}}]}, "invalid_tool_catalog"),
        ({"tools": [{**tool(), "inputSchema": {"bad": float("nan")}}]}, "invalid_tool_catalog"),
        ({"tools": [{**tool(), "title": 1}]}, "invalid_tool_catalog"),
        ({"tools": [tool("bad name")]}, "invalid_tool_catalog"),
        ({"tools": [tool(f"tool_{n}") for n in range(129)]}, "tool_catalog_too_large"),
    ],
)
def test_malformed_catalogs_fail_with_safe_protocol_errors(
    asynchronous: bool, page: object, reason: str
) -> None:
    with pytest.raises(ZentureMCPProtocolError, match=reason) as caught:
        discover([page], asynchronous)
    assert "raw private data" not in str(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_catalog_follows_complete_pagination(asynchronous: bool) -> None:
    pages = [{"tools": [tool()], "nextCursor": "page2"}, {"tools": [tool("get_run")]}]
    assert [item.name for item in discover(pages, asynchronous)] == ["run", "get_run"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        (
            [
                {"tools": [tool()], "nextCursor": "again"},
                {"tools": list[object](), "nextCursor": "again"},
            ],
            "tool_catalog_cursor_loop",
        ),
        ([{"tools": [tool()], "nextCursor": "next"}, {"tools": [tool()]}], "duplicate_tool_name"),
        (
            [{"tools": list[object](), "nextCursor": f"page{n}"} for n in range(128)],
            "tool_catalog_too_many_pages",
        ),
        ([{"tools": list[object](), "nextCursor": 2}], "invalid_tool_catalog"),
        (
            [
                {"tools": [tool(f"tool_{n}") for n in range(65)], "nextCursor": "next"},
                {"tools": [tool(f"tool_{n}") for n in range(65, 129)]},
            ],
            "tool_catalog_too_large",
        ),
    ],
)
def test_pagination_is_bounded_and_never_returns_partial_catalog(
    asynchronous: bool, pages: Sequence[object], reason: str
) -> None:
    with pytest.raises(ZentureMCPProtocolError, match=reason):
        discover(pages, asynchronous)


def test_legacy_name_only_transport_stays_usable_and_rejects_unsupported_continuation() -> None:
    class Legacy:
        def __init__(self, pages: Sequence[object]) -> None:
            self.pages = pages

        def call_tool(self, name: str, arguments: object) -> object:
            raise AssertionError("discovery must not call product tools")

        def list_tools(self) -> object:
            return self.pages[0]

    transport = Legacy([{"tools": [{"name": "run"}]}])
    assert McpClient(transport).list_tools() == ("run",)
    transport.pages = [{"tools": [tool()], "nextCursor": "next"}]
    method = getattr(McpClient(transport), "get_tool_catalog", None)
    assert callable(method), "public full catalog discovery must be available"
    with pytest.raises(ZentureMCPProtocolError, match="tool_catalog_pagination_unsupported"):
        method()


@pytest.mark.asyncio
async def test_official_transport_passes_pagination_params_without_retrying() -> None:
    from zenture._mcp.transport import (
        _OfficialAsyncMcpTransport,  # pyright: ignore[reportPrivateUsage]
    )

    class Session:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        async def list_tools(self, *, params: PaginatedRequestParams | None = None) -> object:
            self.cursors.append(None if params is None else params.cursor)
            if params is None:
                return {"tools": [tool()], "nextCursor": "next"}
            assert params.cursor == "next"
            return {"tools": [tool("get_run")]}

    session = Session()
    definitions = await AsyncMcpClient(_OfficialAsyncMcpTransport(session)).get_tool_catalog()
    assert [item.name for item in definitions] == ["run", "get_run"]
    assert session.cursors == [None, "next"]


@pytest.mark.asyncio
async def test_async_legacy_name_only_transport_preserves_names_and_fails_continuation() -> None:
    class Legacy:
        async def list_tools(self) -> object:
            return {"tools": [{"name": "run"}]}

        async def call_tool(self, name: str, arguments: object) -> object:
            raise AssertionError("discovery must not call product tools")

    assert await AsyncMcpClient(Legacy()).list_tools() == ("run",)

    class PaginatedLegacy(Legacy):
        async def list_tools(self) -> object:
            return {"tools": [tool()], "nextCursor": "next"}

    with pytest.raises(ZentureMCPProtocolError, match="tool_catalog_pagination_unsupported"):
        await AsyncMcpClient(PaginatedLegacy()).get_tool_catalog()


def test_sync_portal_follows_catalog_continuation() -> None:
    from anyio.from_thread import start_blocking_portal

    from zenture._mcp.client import _PortalTransport  # pyright: ignore[reportPrivateUsage]

    transport = AsyncCatalogTransport(
        [
            {"tools": [tool()], "nextCursor": "next"},
            {"tools": [tool("get_run")]},
        ]
    )
    with start_blocking_portal() as portal:
        catalog = McpClient(_PortalTransport(portal, transport)).get_tool_catalog()
    assert [item.name for item in catalog] == ["run", "get_run"]
    assert transport.cursors == [None, "next"]
