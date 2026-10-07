"""Filepath: tests/integration/contract/test_mcp_validation_issues.py
Purpose: Carry real registered MCP validation details into typed SDK errors.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from zenture._mcp.client import (
    _error_from_payload,  # pyright: ignore[reportPrivateUsage] -- consumer boundary
)
from zenture.errors import error_from_response


@pytest.mark.parametrize(
    ("tool", "arguments", "path", "category"),
    [
        ("get_run", {"run_id": "run_abc", "view": "PRIVATE_SENTINEL"}, "/view", "invalid_enum"),
        ("get_run", {"run_id": "run_abc", "replay_limit": 51}, "/replay_limit", "out_of_range"),
        ("get_run", {"run_id": "run_abc", "replay_cursor": ""}, "/replay_cursor", "too_short"),
        ("list_runs", {"created_after": ""}, "/created_after", "too_short"),
        ("list_runs", {"created_before": ""}, "/created_before", "too_short"),
        (
            "run",
            {"task": "task", "artifact": {"type": "PRIVATE_SENTINEL", "value": "selected"}},
            "/artifact/type",
            "invalid_enum",
        ),
        (
            "run",
            {"task": "task", "artifact": {"value": "PRIVATE_SENTINEL"}},
            "/artifact/type",
            "required",
        ),
    ],
)
def test_registered_get_run_issues_survive_sdk_sanitation(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, object],
    path: str,
    category: str,
) -> None:
    source = Path(__file__).resolve().parents[4] / "zenture-mcp/services/mcp-server/src"
    monkeypatch.setattr(sys, "path", [str(source), *sys.path])
    server = importlib.import_module("zenture_mcp_server.mcp_adapter").ZentureMCPServer(
        "sdk-contract"
    )
    register = importlib.import_module("zenture_mcp_server.run_tools").register_run_tools
    register(server, cast("Any", object()))
    result = asyncio.run(server.call_tool(tool, arguments))
    payload = result.structured_content
    assert payload is not None
    error = _error_from_payload(payload)
    assert [(issue.path, issue.category) for issue in error.issues] == [(path, category)]
    assert "PRIVATE" not in str(error) + repr(error)
    # REST sanitation must not accidentally admit MCP-only fields.
    rest = error_from_response(
        status_code=422,
        payload={
            "error": {
                "code": "validation_failed",
                "message": "Validation failed",
                "issues": payload["error"]["issues"],
            }
        },
        headers={},
    )
    if path in {"/view", "/replay_limit", "/replay_cursor"}:
        assert rest.issues == ()


def test_registered_provenance_does_not_tighten_local_timestamp_admission() -> None:
    from zenture._mcp.contracts import McpListRunsRequest

    assert McpListRunsRequest(created_after="", created_before="").created_after == ""
