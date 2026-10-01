"""Client-level cross-channel composition with mocked protocol boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from zenture._mcp import AsyncMcpClient
from zenture._resources.async_runs import AsyncRunsResource

if TYPE_CHECKING:
    from collections.abc import Mapping

RUN_ID = "run_33333333333343338333333333333333"
RUN_KEY = "cross_channel_run_1"


def _run(*, status: str = "completed") -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": status,
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:01Z",
        "completed_at": "2026-09-01T12:00:01Z",
        "event_cursor": "cursor_1",
    }


@dataclass
class ToolResult:
    structured_content: object
    is_error: bool = False


class McpTransport:
    async def list_tools(self) -> object:
        return {"tools": [{"name": name} for name in ("run", "get_run", "cancel_run")]}

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        if name == "run":
            # Only the MCP Run receipt extends the canonical REST projection.
            key = arguments["idempotency_key"]
            assert key == RUN_KEY
            return ToolResult({**_run(status="completed"), "idempotency_key": key})
        if name == "get_run":
            return ToolResult(_run())
        if name == "cancel_run":
            return ToolResult(_run(status="cancelled"))
        raise AssertionError(name)


class ApiTransport:
    async def request_json(
        self,
        _method: str,
        path: str,
        *,
        json: object | None = None,
        **_kwargs: object,
    ) -> object:
        if path == f"/runs/{RUN_ID}":
            return _run()
        if path == f"/runs/{RUN_ID}/outcome":
            return _run()
        raise AssertionError((path, json))


@pytest.mark.asyncio
async def test_mcp_and_api_clients_compose_over_one_canonical_run_projection() -> None:
    mcp = AsyncMcpClient(McpTransport())
    api = AsyncRunsResource(ApiTransport())  # type: ignore[arg-type]

    created_via_mcp = await mcp.run(
        task="Review the selected answer",
        artifact={"type": "text", "value": "selected answer"},
        idempotency_key=RUN_KEY,
    )
    read_via_api = await api.get(created_via_mcp.run_id, view="full")
    outcome_via_api = await api.record_outcome(
        created_via_mcp.run_id,
        outcome="used",
        idempotency_key="cross-channel-outcome-1",
    )
    cancelled_via_mcp = await mcp.cancel_run(created_via_mcp.run_id)

    assert created_via_mcp.run_id == read_via_api.run_id == outcome_via_api.run_id
    assert created_via_mcp.status.value == read_via_api.status.value == "completed"
    assert cancelled_via_mcp.status.value == "cancelled"
    assert created_via_mcp.idempotency_key == RUN_KEY
    assert "idempotency_key" not in created_via_mcp.model_dump()
    assert not hasattr(read_via_api, "idempotency_key")
    assert not hasattr(outcome_via_api, "idempotency_key")
    assert not hasattr(cancelled_via_mcp, "idempotency_key")
