"""Filepath: tests/unit/test_validation_issues.py
Purpose: Prove additive safe error details and local Run validation privacy.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from zenture._contract.models import PrepareKnowledgeRunRequest
from zenture._mcp.client import (
    _error_from_payload,  # pyright: ignore[reportPrivateUsage] -- parser boundary unit tests
)
from zenture.errors import error_from_response


def test_rest_and_mcp_preserve_typed_issues_and_coarse_error() -> None:
    issues = [
        {
            "path": "/profile",
            "category": "invalid_enum",
            "constraints": {"allowed_values": ["fast", "standard", "detailed"]},
        }
    ]
    rest = error_from_response(
        status_code=422,
        payload={
            "error": {"code": "validation_failed", "message": "Invalid request", "issues": issues}
        },
        headers={"Retry-After": "3"},
    )
    mcp = _error_from_payload(
        {
            "error": {
                "code": "validation_failed",
                "http_status": 422,
                "retryable": False,
                "next_action": "check_request",
                "issues": issues,
            }
        }
    )
    assert rest.issues == mcp.issues
    assert rest.issues[0].path == "/profile"
    assert rest.issues[0].constraints is not None
    assert rest.issues[0].constraints.allowed_values == ("fast", "standard", "detailed")
    assert rest.retry_after == 3
    assert mcp.retryable is False
    assert "allowed_values" not in repr(rest) + str(rest) + repr(mcp) + str(mcp)


@pytest.mark.parametrize(
    "issues",
    [
        None,
        [],
        "bad",
        [{"path": "/task", "category": "BAD"}],
        [{"path": "/task", "category": "required", "input": "PRIVATE_SENTINEL"}],
    ],
)
def test_malformed_optional_issues_do_not_erase_coarse_failure(issues: object) -> None:
    error = error_from_response(
        status_code=422,
        payload={
            "error": {"code": "validation_failed", "message": "Invalid request", "issues": issues}
        },
        headers={},
    )
    assert error.error_code == "validation_failed"
    assert error.issues == ()


def test_direct_run_validation_preserves_family_without_input_or_unknown_key() -> None:
    with pytest.raises(ValidationError) as caught:
        PrepareKnowledgeRunRequest.model_validate(
            {
                "task": "PRIVATE_SENTINEL",
                "profile": "PRIVATE_SENTINEL",
                "artifact": {"type": "text", "value": "ok"},
                "PRIVATE_KEY": "PRIVATE_SENTINEL",
            }
        )
    assert "PRIVATE_SENTINEL" not in str(caught.value) + repr(caught.value)
    assert "PRIVATE_KEY" not in str(caught.value) + repr(caught.value)
    assert all(error.get("input") is None for error in caught.value.errors())


@pytest.mark.parametrize(
    "issue",
    [
        {"path": "/PRIVATE_KEY", "category": "required"},
        {
            "path": "/profile",
            "category": "invalid_enum",
            "constraints": {"allowed_values": ["PRIVATE_SENTINEL"]},
        },
        {"path": "/task", "category": "too_long", "constraints": {"max_length": 123}},
        {"path": "/finding_adjudications/20/outcome", "category": "invalid_enum"},
    ],
)
def test_external_issue_details_must_match_public_schema(issue: object) -> None:
    error = _error_from_payload(
        {
            "error": {
                "code": "validation_failed",
                "http_status": 422,
                "retryable": False,
                "next_action": "check_request",
                "issues": [issue],
            }
        }
    )
    assert error.code == "validation_failed"
    assert error.issues == ()
    assert "PRIVATE" not in str(error) + repr(error)


def test_actual_mcp_producer_output_is_consumed_without_fixture_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import sys
    from pathlib import Path

    monkeypatch.setattr(
        sys,
        "path",
        [
            str(Path(__file__).resolve().parents[3] / "zenture-mcp/services/mcp-server/src"),
            *sys.path,
        ],
    )
    import importlib

    ZentureMCPServer = importlib.import_module("zenture_mcp_server.mcp_adapter").ZentureMCPServer
    register_run_tools = importlib.import_module("zenture_mcp_server.run_tools").register_run_tools

    server = ZentureMCPServer("sdk-consumer-contract")
    register_run_tools(server, cast("Any", object()))
    result = asyncio.run(server.call_tool("run", {"artifact": {"type": "text", "value": "ok"}}))
    assert result.structured_content is not None
    error = _error_from_payload(result.structured_content)
    assert error.code == "validation_failed"
    assert [(item.path, item.category) for item in error.issues] == [("/task", "required")]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sync_async_rest_transport_exposes_same_typed_failure(asynchronous: bool) -> None:
    import asyncio

    import httpx

    from zenture import AsyncZentureClient, ZentureClient
    from zenture.errors import ZentureValidationError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "validation_failed",
                    "message": "Invalid request",
                    "issues": [{"path": "/task", "category": "required"}],
                }
            },
            headers={"Retry-After": "4"},
        )

    async def call_async() -> None:
        async with AsyncZentureClient(
            api_key="zt_live_" + "0" * 32,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            await client.runs.list()

    def invoke() -> None:
        if asynchronous:
            asyncio.run(call_async())
        else:
            with ZentureClient(
                api_key="zt_live_" + "0" * 32,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            ) as client:
                client.runs.list()

    with pytest.raises(ZentureValidationError) as caught:
        invoke()
    assert caught.value.issues[0].path == "/task"
    assert caught.value.retry_after == 4


def test_declared_byte_size_issue_survives_rest_parsing() -> None:
    error = error_from_response(
        status_code=422,
        payload={
            "error": {
                "code": "validation_failed",
                "message": "Invalid request",
                "issues": [
                    {
                        "path": "/byte_size",
                        "category": "out_of_range",
                        "constraints": {"maximum": 10485760, "unit": "bytes"},
                    }
                ],
            }
        },
        headers={},
    )
    assert len(error.issues) == 1
    assert error.issues[0].constraints is not None
    assert error.issues[0].constraints.unit == "bytes"
