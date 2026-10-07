"""Filepath: tests/integration/contract/test_header_validation_issues.py
Purpose: Preserve declared public header details across actual producer, MCP and SDK seams.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from zenture.errors import ZentureMCPError, error_from_response
from zenture.mcp import AsyncMcpClient, McpClient
from zenture.validation_issues import safe_issues


@pytest.fixture
def producer(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    root = Path(__file__).resolve().parents[4] / "zenture-mcp/services/mcp-server"
    unit = Path(__file__).resolve().parents[2] / "unit"
    monkeypatch.setattr(sys, "path", [str(root / "src"), str(root / "tests"), str(unit), *sys.path])
    helpers = importlib.import_module("test_gateway_validation_producers")
    gateway = importlib.import_module("zenture_mcp_server.run_gateway_client")
    declarations = importlib.import_module("zenture_mcp_server.gateway_validation")
    return helpers._produce, gateway.McpRunGatewayClient, declarations.operation_schema


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("case", "operation", "path"),
    [("key", "run.start", "/Idempotency-Key"), ("length", "artifact.finalize", "/Content-Length")],
)
def test_actual_backend_mcp_sdk_header_details_survive_public_decode(
    producer: tuple[Any, Any, Any], asynchronous: bool, case: str, operation: str, path: str
) -> None:
    produce, gateway, _ = producer
    detail = produce(case)

    class Response:
        status_code = 422
        content = json.dumps({"error": {"code": "validation_failed", **detail}}).encode()

    remote = gateway._remote_error(
        Response(), operation=operation, request_id="synthetic_request", duration_ms=0
    )
    assert len(remote.issues) == 1
    payload = {
        "error": {
            "code": remote.code,
            "http_status": remote.status_code,
            "retryable": remote.retryable,
            "issues": [issue.model_dump(mode="json", exclude_none=True) for issue in remote.issues],
        }
    }
    fixtures = importlib.import_module("test_mcp_client")
    peer = (
        AsyncMcpClient(fixtures.AsyncRecordingTransport({"run": payload}))
        if asynchronous
        else McpClient(fixtures.RecordingTransport({"run": payload}))
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

    with pytest.raises(ZentureMCPError) as caught:
        asyncio.run(invoke())
    assert caught.value.code == "validation_failed"
    assert caught.value.status_code == 422
    assert [issue.path for issue in caught.value.issues] == [path]
    assert caught.value.issues[0].model_dump() == remote.issues[0].model_dump()
    rest = error_from_response(
        status_code=422,
        payload={"error": {"code": "validation_failed", "message": "Validation failed", **detail}},
        headers={},
    )
    assert [issue.path for issue in rest.issues] == [path]


def test_complete_supported_header_contract_matches_sdk(producer: tuple[Any, Any, Any]) -> None:
    _, _, schema = producer
    projection = importlib.import_module("zenture_mcp_server.validation_projection")
    properties: dict[str, Any] = {}
    for operation in ("run.start", "run.events_stream", "artifact.finalize"):
        properties.update(
            {
                name: node
                for name, node in schema(operation)["properties"].items()
                if name[0].isupper()
            }
        )
    assert set(properties) == {
        "Idempotency-Key",
        "Prefer",
        "Last-Event-ID",
        "Content-Length",
        "Content-Type",
        "X-Upload-ID",
    }
    for name, node in properties.items():
        raw: dict[str, object] = {"path": "/" + name, "category": "invalid_format"}
        branch = next(child for child in node.get("anyOf", [node]) if child.get("type") != "null")
        facts = projection.facts(branch)
        if facts:
            raw["constraints"] = facts
        for channel in ("mcp", "rest"):
            issues = safe_issues([raw], channel=channel)
            assert len(issues) == 1, name
            assert issues[0].path == "/" + name


@pytest.mark.parametrize(
    "raw",
    [
        {"path": "/Authorization", "category": "required"},
        {"path": "/PRIVATE_SENTINEL", "category": "required"},
        {
            "path": "/Idempotency-Key",
            "category": "too_long",
            "constraints": {"max_length": 128, "unit": "unicode_code_points"},
        },
        {
            "path": "/Content-Length",
            "category": "out_of_range",
            "constraints": {"maximum": 1, "unit": "bytes"},
        },
        {
            "path": "/Content-Length",
            "category": "out_of_range",
            "constraints": {"maximum": 10485760, "unit": "seconds"},
        },
        {"path": "/Content-Type", "category": "too_long", "constraints": {"max_length": 128}},
    ],
)
def test_unknown_or_unowned_header_facts_remain_optional(raw: dict[str, object]) -> None:
    assert safe_issues([raw], channel="mcp") == ()
    error = error_from_response(
        status_code=422,
        payload={
            "error": {"code": "validation_failed", "message": "Validation failed", "issues": [raw]}
        },
        headers={},
    )
    assert error.error_code == "validation_failed"
    assert error.issues == ()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("name", "operation", "method"),
    [
        ("Idempotency-Key", "run.start", "run"),
        ("Prefer", "run.start", "run"),
        ("Last-Event-ID", "run.events_stream", "get_run"),
        ("Content-Length", "artifact.finalize", "attach_artifact"),
        ("Content-Type", "artifact.finalize", "attach_artifact"),
        ("X-Upload-ID", "artifact.finalize", "attach_artifact"),
    ],
)
def test_every_declared_header_survives_public_sync_async_error_decode(
    producer: tuple[Any, Any, Any], asynchronous: bool, name: str, operation: str, method: str
) -> None:
    _, gateway, schema = producer
    projection = importlib.import_module("zenture_mcp_server.validation_projection")
    node = schema(operation)["properties"][name]
    branch = next(child for child in node.get("anyOf", [node]) if child.get("type") != "null")
    raw: dict[str, object] = {"path": "/" + name, "category": "invalid_format"}
    facts = projection.facts(branch)
    if facts:
        raw["constraints"] = facts

    class Response:
        status_code = 422
        content = json.dumps({"error": {"code": "validation_failed", "issues": [raw]}}).encode()

    remote = gateway._remote_error(
        Response(), operation=operation, request_id="synthetic_request", duration_ms=0
    )
    assert len(remote.issues) == 1
    payload = {
        "error": {
            "code": remote.code,
            "http_status": remote.status_code,
            "retryable": True,
            "retry_after_seconds": 5,
            "issues": [issue.model_dump(mode="json", exclude_none=True) for issue in remote.issues],
        }
    }
    fixtures = importlib.import_module("test_mcp_client")
    peer = (
        AsyncMcpClient(fixtures.AsyncRecordingTransport({method: payload}))
        if asynchronous
        else McpClient(fixtures.RecordingTransport({method: payload}))
    )

    async def invoke() -> None:
        args: tuple[object, ...] = ("run_abc",) if method == "get_run" else ()
        kwargs: dict[str, Any] = {}
        if method == "run":
            kwargs = {
                "task": "task",
                "artifact": {"type": "text", "value": "selected"},
                "idempotency_key": "saved_key",
            }
        elif method == "attach_artifact":
            kwargs = {
                "file_name": "selected.txt",
                "mime_type": "text/plain",
                "byte_size": 1,
                "content_hash": "a" * 64,
            }
        if isinstance(peer, AsyncMcpClient):
            await getattr(peer, method)(*args, **kwargs)
        else:
            getattr(peer, method)(*args, **kwargs)

    with pytest.raises(ZentureMCPError) as caught:
        asyncio.run(invoke())
    assert [issue.path for issue in caught.value.issues] == ["/" + name]
    assert caught.value.code == "validation_failed"
    assert caught.value.status_code == 422
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 5
