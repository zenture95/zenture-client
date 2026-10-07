"""Filepath: tests/unit/test_mcp_validation_privacy.py
Purpose: Prove public MCP local validation excludes content before any transport I/O.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError

from zenture.mcp import AsyncMcpClient, McpClient

if TYPE_CHECKING:
    from collections.abc import Mapping

SENTINEL = "PRIVATE_VALIDATION_SENTINEL"


class NoDispatch:
    def list_tools(self) -> object:
        return {"tools": []}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        pytest.fail("Local validation must reject before transport dispatch")


class AsyncNoDispatch(NoDispatch):
    async def list_tools(self) -> object:
        return super().list_tools()

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        return super().call_tool(name, arguments)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("get_run", (SENTINEL,), {}),
        ("get_run", ("run_abc",), {"view": SENTINEL}),
        ("list_runs", (), {"status": [SENTINEL * 4]}),
        ("list_runs", (), {"limit": SENTINEL}),
        (
            "attach_artifact",
            (),
            {
                "file_name": "selected.txt",
                "mime_type": "text/plain",
                "byte_size": 5,
                "content_hash": SENTINEL,
            },
        ),
        ("record_run_outcome", ("run_abc",), {"outcome": SENTINEL}),
        (
            "record_run_outcome",
            ("run_abc",),
            {
                "outcome": "used",
                "finding_adjudications": [{"finding_ref": "finding:known", "outcome": SENTINEL}],
            },
        ),
        ("wait_run", (SENTINEL, 1), {}),
    ],
)
def test_public_mcp_validation_preserves_family_without_content(
    asynchronous: bool,
    method: str,
    args: tuple[object, ...],
    kwargs: dict[str, Any],
) -> None:
    peer = AsyncMcpClient(AsyncNoDispatch()) if asynchronous else McpClient(NoDispatch())

    async def invoke() -> None:
        if isinstance(peer, AsyncMcpClient):
            await getattr(peer, method)(*args, **kwargs)
        else:
            getattr(peer, method)(*args, **kwargs)

    with pytest.raises(ValidationError) as caught:
        asyncio.run(invoke())
    assert SENTINEL not in str(caught.value) + repr(caught.value)
    assert all(item.get("input") is None and "ctx" not in item for item in caught.value.errors())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(("outcome", "reference"), [("used", "art_" + SENTINEL), ("edited", None)])
def test_public_mcp_outcome_combination_errors_are_content_free(
    asynchronous: bool, outcome: str, reference: str | None
) -> None:
    peer = AsyncMcpClient(AsyncNoDispatch()) if asynchronous else McpClient(NoDispatch())

    async def invoke() -> None:
        if isinstance(peer, AsyncMcpClient):
            await peer.record_run_outcome(
                "run_abc", outcome=cast("Any", outcome), edited_artifact_ref=reference
            )
        else:
            peer.record_run_outcome(
                "run_abc", outcome=cast("Any", outcome), edited_artifact_ref=reference
            )

    with pytest.raises(ValidationError) as caught:
        asyncio.run(invoke())
    assert SENTINEL not in str(caught.value) + repr(caught.value)
    assert all(item.get("input") is None and "ctx" not in item for item in caught.value.errors())


@pytest.mark.parametrize(("outcome", "reference"), [("used", "art_" + SENTINEL), ("edited", None)])
def test_rest_outcome_combination_errors_are_content_free(
    outcome: str, reference: str | None
) -> None:
    from zenture._contract.models import RecordRunOutcomeRequest

    with pytest.raises(ValidationError) as caught:
        RecordRunOutcomeRequest(outcome=cast("Any", outcome), edited_artifact_ref=reference)
    assert SENTINEL not in str(caught.value) + repr(caught.value)
    assert all(item.get("input") is None and "ctx" not in item for item in caught.value.errors())
