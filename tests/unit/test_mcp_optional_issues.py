"""Filepath: tests/unit/test_mcp_optional_issues.py
Purpose: Keep optional malformed details from replacing mandatory MCP error semantics.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_mcp_client import RUN_ID, AsyncRecordingTransport, RecordingTransport

from zenture.errors import ZentureMCPError, ZentureMCPProtocolError
from zenture.mcp import AsyncMcpClient, McpClient


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("method", ["get_run", "run"])
def test_optional_forbidden_data_keeps_coarse_error_but_other_fields_stay_guarded(
    asynchronous: bool, outside: bool, method: str
) -> None:
    error: dict[str, object] = {
        "code": "validation_failed",
        "http_status": 422,
        "retryable": True,
        "next_action": "retry_later",
        "retry_after_seconds": 5,
        "issues": [{"path": "/task", "category": "required", "prompt": "PRIVATE_SENTINEL"}],
    }
    if outside:
        error["prompt"] = "PRIVATE_SENTINEL"
    responses: dict[str, object] = {}
    responses[method] = {"error": error}
    peer = (
        AsyncMcpClient(AsyncRecordingTransport(responses))
        if asynchronous
        else McpClient(RecordingTransport(responses))
    )

    async def invoke() -> None:
        args: tuple[object, ...] = (RUN_ID,) if method == "get_run" else ()
        kwargs: dict[str, Any] = (
            {}
            if method == "get_run"
            else {
                "task": "task",
                "artifact": {"type": "text", "value": "selected"},
                "idempotency_key": "saved_key",
            }
        )
        if isinstance(peer, AsyncMcpClient):
            await getattr(peer, method)(*args, **kwargs)
        else:
            getattr(peer, method)(*args, **kwargs)

    with pytest.raises(ZentureMCPError) as caught:
        asyncio.run(invoke())
    if outside:
        assert isinstance(caught.value, ZentureMCPProtocolError)
        assert caught.value.code == "forbidden_result_field"
    else:
        assert not isinstance(caught.value, ZentureMCPProtocolError)
        assert caught.value.code == "validation_failed"
        assert caught.value.status_code == 422
        assert caught.value.retryable is True
        assert caught.value.retry_after_seconds == 5
        assert caught.value.issues == ()
    assert "PRIVATE" not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("guard", ["size", "idempotency"])
def test_optional_issue_sanitation_preserves_original_payload_guards(
    asynchronous: bool, guard: str
) -> None:
    class SyncPeer(RecordingTransport):
        def call_tool(self, name: str, arguments: Any) -> object:
            return {
                "structured_content": {
                    "idempotency_key": "wrong_key"
                    if guard == "idempotency"
                    else arguments["idempotency_key"],
                    "error": {
                        "code": "validation_failed",
                        "issues": [
                            {
                                "prompt": "x" * (512 * 1024 + 1)
                                if guard == "size"
                                else "PRIVATE_SENTINEL"
                            }
                        ],
                    },
                }
            }

    class AsyncPeer(AsyncRecordingTransport):
        async def call_tool(self, name: str, arguments: Any) -> object:
            return SyncPeer({}).call_tool(name, arguments)

    peer = (
        AsyncMcpClient(AsyncPeer({}))
        if asynchronous
        else McpClient(SyncPeer({}))
    )

    async def invoke() -> None:
        kwargs: dict[str, Any] = {
            "task": "task",
            "artifact": {"type": "text", "value": "selected"},
            "idempotency_key": "saved_key",
        }
        if isinstance(peer, AsyncMcpClient):
            await peer.run(**kwargs)
        else:
            peer.run(**kwargs)

    with pytest.raises(ZentureMCPProtocolError) as caught:
        asyncio.run(invoke())
    assert caught.value.code == (
        "result_too_large" if guard == "size" else "idempotency_key_mismatch"
    )
