"""Contract tests for the opt-in client-side MCP peer surface."""

from __future__ import annotations

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from zenture._contract import (
    ArtifactUploadResponse,
    ListRunEventsResponse,
    ListRunsResponse,
    PublicRunResponse,
)
from zenture._mcp import (
    PRODUCT_TOOL_NAMES,
    AsyncMcpClient,
    McpClient,
    McpEndpoint,
    McpRunRead,
)
from zenture.errors import (
    ZentureMCPDependencyError,
    ZentureMCPError,
    ZentureMCPProtocolError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from pydantic import BaseModel

    from zenture._mcp.transport import AsyncMcpTransport

RUN_ID = "run_33333333333343338333333333333333"
ARTIFACT_REF = "art_33333333333343338333333333333333"


def _run(*, status: str = "completed", run_id: str = RUN_ID) -> dict[str, object]:
    return {
        "run_id": run_id,
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": status,
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:01Z",
        "completed_at": "2026-09-01T12:00:01Z",
        "artifact_refs": [ARTIFACT_REF],
        "event_cursor": "cursor_1",
    }


def _list_runs() -> dict[str, object]:
    return {
        "runs": [
            {
                "run_id": RUN_ID,
                "status": "completed",
                "decision": "ready",
                "profile": "standard",
                "created_at": "2026-09-01T12:00:00Z",
                "updated_at": "2026-09-01T12:00:01Z",
            }
        ],
        "has_more": False,
    }


def _events() -> dict[str, object]:
    return {
        "events": [
            {
                "type": "run.event",
                "event_id": "event_1",
                "run_id": RUN_ID,
                "sequence": 1,
                "phase": "completed",
                "status": "completed",
                "message_key": "run.status.completed",
                "event_cursor": "cursor_1",
            }
        ],
        "has_more": False,
    }


def _tool_result(payload: object, *, is_error: bool = False) -> ToolResult:
    return ToolResult(structured_content=payload, is_error=is_error)


@dataclass
class ToolResult:
    structured_content: object
    is_error: bool = False


@dataclass
class TextContent:
    type: str
    text: str


@dataclass
class TextOnlyToolResult:
    content: list[TextContent]
    is_error: bool = False


class RecordingTransport:
    def __init__(
        self,
        responses: dict[str, object],
        *,
        tool_names: tuple[str, ...] = PRODUCT_TOOL_NAMES,
    ) -> None:
        self.responses = responses
        self.tool_names = tool_names
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def list_tools(self) -> object:
        return {"tools": [{"name": name} for name in self.tool_names]}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, arguments))
        payload = self.responses[name]
        return _tool_result(payload)


class AsyncRecordingTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    async def list_tools(self) -> object:
        return {
            "tools": [
                {"name": name}
                for name in (
                    "run",
                    "attach_artifact",
                    "list_runs",
                    "get_run",
                    "cancel_run",
                    "record_run_outcome",
                )
            ]
        }

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.calls.append((name, arguments))
        return _tool_result(self.responses[name])


class RawTransport:
    def __init__(self, result: object) -> None:
        self.result = result

    def list_tools(self) -> object:
        return {"tools": list(PRODUCT_TOOL_NAMES)}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        return self.result


class RaisingTransport:
    def list_tools(self) -> object:
        raise RuntimeError("transport failed")

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        raise RuntimeError("transport failed")


class AsyncRaisingTransport:
    async def list_tools(self) -> object:
        raise RuntimeError("transport failed")

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        raise RuntimeError("transport failed")


class CatalogTransport:
    def __init__(self, catalog: object) -> None:
        self.catalog = catalog

    def list_tools(self) -> object:
        return self.catalog

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        return _tool_result(_run())


class MCPErrorTransport:
    def list_tools(self) -> object:
        return {"tools": list(PRODUCT_TOOL_NAMES)}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        raise ZentureMCPError("known_transport_error")


class AsyncMCPErrorTransport:
    async def list_tools(self) -> object:
        raise ZentureMCPError("known_transport_error")

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        del name, arguments
        raise ZentureMCPError("known_transport_error")


def _responses() -> dict[str, object]:
    return {
        "run": _run(status="queued"),
        "attach_artifact": {
            "artifact_ref": ARTIFACT_REF,
            "content_hash": "a" * 64,
            "byte_size": 4,
            "content_type": "text/plain",
        },
        "list_runs": _list_runs(),
        "get_run": {**_run(), "event_replay": _events()},
        "cancel_run": _run(status="cancelled"),
        "record_run_outcome": _run(),
    }


def test_sync_mcp_peer_maps_all_product_tools_to_canonical_models() -> None:
    transport = RecordingTransport(_responses())
    client = McpClient(transport)

    assert client.list_tools() == (
        "run",
        "attach_artifact",
        "list_runs",
        "get_run",
        "cancel_run",
        "record_run_outcome",
    )
    client.require_product_tools()

    run = client.run(task="Review this answer", artifact={"type": "text", "value": "text"})
    artifact = client.attach_artifact(
        file_name="answer.txt",
        mime_type="text/plain",
        byte_size=4,
        content_hash="a" * 64,
    )
    runs = client.list_runs(limit=1)
    read = client.get_run(RUN_ID, replay_cursor="cursor_0")
    cancelled = client.cancel_run(RUN_ID)
    outcome = client.record_run_outcome(RUN_ID, outcome="used")

    assert isinstance(run, PublicRunResponse)
    assert isinstance(artifact, ArtifactUploadResponse)
    assert isinstance(runs, ListRunsResponse)
    assert isinstance(read, McpRunRead)
    assert isinstance(read.run, PublicRunResponse)
    assert isinstance(read.event_replay, ListRunEventsResponse)
    assert isinstance(cancelled, PublicRunResponse)
    assert isinstance(outcome, PublicRunResponse)
    assert [name for name, _arguments in transport.calls] == [
        "run",
        "attach_artifact",
        "list_runs",
        "get_run",
        "cancel_run",
        "record_run_outcome",
    ]
    assert transport.calls[0][1]["profile"] == "standard"
    assert transport.calls[3][1] == {
        "run_id": RUN_ID,
        "view": "summary",
        "replay_cursor": "cursor_0",
        "replay_limit": 50,
    }
    assert transport.calls[-1][1] == {
        "run_id": RUN_ID,
        "outcome": "used",
        "finding_adjudications": [],
    }


def test_mcp_run_peer_preserves_deadline_and_ignores_additive_response_fields() -> None:
    responses = _responses()
    responses["get_run"] = {
        **_run(),
        "deadline_at": "2026-09-01T12:05:00Z",
        "future_run_field": {"opaque": True},
    }
    client = McpClient(RecordingTransport(responses))

    read = client.get_run(RUN_ID)

    assert read.run.deadline_at is not None
    assert read.run.deadline_at.isoformat() == "2026-09-01T12:05:00+00:00"
    assert not hasattr(read.run, "future_run_field")


@pytest.mark.parametrize("final_credits", [None, "omitted"])
def test_mcp_start_accepts_pending_billing_with_absent_or_null_amount(
    final_credits: object,
) -> None:
    projection: dict[str, object] = {
        "schema_version": "run.terminal_billing_projection.v1",
        "status": "pending",
    }
    if final_credits != "omitted":
        projection["final_credits"] = final_credits
    payload = {**_run(status="queued"), "billing_projection": projection}

    run = McpClient(RecordingTransport({"run": payload})).run(
        task="Review this answer", artifact={"type": "text", "value": "text"}
    )

    assert run.billing_projection is not None
    assert run.billing_projection.final_credits is None


@pytest.mark.parametrize("status", ["settled", "released", "unavailable"])
@pytest.mark.parametrize("final_credits", [None, "omitted"])
def test_mcp_start_rejects_terminal_billing_without_typed_amount(
    status: str, final_credits: object
) -> None:
    projection: dict[str, object] = {
        "schema_version": "run.terminal_billing_projection.v1",
        "status": status,
    }
    if final_credits != "omitted":
        projection["final_credits"] = final_credits
    payload = {**_run(status="queued"), "billing_projection": projection}

    with pytest.raises(ZentureMCPProtocolError, match="invalid_result_contract"):
        McpClient(RecordingTransport({"run": payload})).run(
            task="Review this answer", artifact={"type": "text", "value": "text"}
        )


def test_mcp_start_rejects_pending_billing_with_final_amount() -> None:
    payload = {
        **_run(status="queued"),
        "billing_projection": {
            "schema_version": "run.terminal_billing_projection.v1",
            "status": "pending",
            "final_credits": {"status": "available", "amount": "1.00", "unit": "credits"},
        },
    }

    with pytest.raises(ZentureMCPProtocolError, match="invalid_result_contract"):
        McpClient(RecordingTransport({"run": payload})).run(
            task="Review this answer", artifact={"type": "text", "value": "text"}
        )


def test_mcp_start_preserves_terminal_billing_amount_validation() -> None:
    payload = {
        **_run(status="queued"),
        "billing_projection": {
            "schema_version": "run.terminal_billing_projection.v1",
            "status": "settled",
            "final_credits": {"status": "available", "amount": "1.00", "unit": "credits"},
        },
    }

    run = McpClient(RecordingTransport({"run": payload})).run(
        task="Review this answer", artifact={"type": "text", "value": "text"}
    )

    assert run.billing_projection is not None
    assert run.billing_projection.status == "settled"
    assert run.billing_projection.final_credits is not None
    assert run.billing_projection.final_credits.amount == "1.00"


@pytest.mark.asyncio
async def test_async_mcp_peer_preserves_the_same_typed_boundary() -> None:
    transport = AsyncRecordingTransport(_responses())
    client = AsyncMcpClient(transport)

    assert await client.list_tools() == (
        "run",
        "attach_artifact",
        "list_runs",
        "get_run",
        "cancel_run",
        "record_run_outcome",
    )
    await client.require_product_tools()
    run = await client.run(task="Review this answer", artifact={"type": "text", "value": "text"})
    read = await client.get_run(RUN_ID, replay_cursor="cursor_0")
    await client.cancel_run(RUN_ID)

    assert isinstance(run, PublicRunResponse)
    assert isinstance(read, McpRunRead)
    assert isinstance(read.event_replay, ListRunEventsResponse)
    assert [name for name, _arguments in transport.calls] == ["run", "get_run", "cancel_run"]


@pytest.mark.asyncio
async def test_async_mcp_peer_maps_artifact_list_replay_and_outcome_operations() -> None:
    transport = AsyncRecordingTransport(_responses())
    client = AsyncMcpClient(transport)

    artifact = await client.attach_artifact(
        file_name="answer.txt",
        mime_type="text/plain",
        byte_size=4,
        content_hash="a" * 64,
    )
    runs = await client.list_runs(limit=1)
    events = await client.replay_events(RUN_ID, cursor="cursor_0", limit=1)
    outcome = await client.record_run_outcome(
        RUN_ID,
        outcome="edited",
        edited_artifact_ref=ARTIFACT_REF,
        finding_adjudications=[{"finding_ref": "finding_1", "outcome": "confirmed"}],
    )

    assert isinstance(artifact, ArtifactUploadResponse)
    assert isinstance(runs, ListRunsResponse)
    assert isinstance(events, ListRunEventsResponse)
    assert isinstance(outcome, PublicRunResponse)
    assert [name for name, _arguments in transport.calls] == [
        "attach_artifact",
        "list_runs",
        "get_run",
        "record_run_outcome",
    ]


@pytest.mark.asyncio
async def test_async_mcp_client_maps_transport_failures_and_missing_catalog() -> None:
    failing = AsyncMcpClient(AsyncRaisingTransport())
    with pytest.raises(ZentureMCPError, match="mcp_transport_unavailable"):
        await failing.list_tools()
    with pytest.raises(ZentureMCPError, match="mcp_transport_unavailable"):
        await failing.get_run(RUN_ID)

    incomplete = AsyncRecordingTransport(_responses())

    async def incomplete_tools() -> object:
        return {"tools": [{"name": "run"}]}

    incomplete.list_tools = incomplete_tools  # type: ignore[method-assign]
    with pytest.raises(ZentureMCPProtocolError, match="tool_catalog_incomplete"):
        await AsyncMcpClient(incomplete).require_product_tools()


@pytest.mark.asyncio
async def test_async_mcp_client_connects_through_the_transport_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.client as client_module

    transport = AsyncRecordingTransport(_responses())

    @asynccontextmanager
    async def open_transport(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[AsyncMcpTransport, None]:
        yield transport

    monkeypatch.setattr(client_module, "open_streamable_http_transport", open_transport)
    async with AsyncMcpClient.connect("https://mcp.zenture.app", bearer_token="opaque") as client:
        result = await client.run(
            task="Review this answer",
            artifact={"type": "text", "value": "text"},
        )

    assert result.run_id == RUN_ID


def test_mcp_error_is_typed_and_does_not_expose_remote_body() -> None:
    transport = RecordingTransport(
        {
            "run": {
                "error": {
                    "code": "forbidden",
                    "http_status": 403,
                    "retryable": False,
                    "next_action": "reauthorize",
                    "reason": "private-body-must-not-escape",
                }
            }
        }
    )

    with pytest.raises(ZentureMCPError) as error:
        McpClient(transport).run(
            task="Review this answer", artifact={"type": "text", "value": "text"}
        )

    assert error.value.code == "forbidden"
    assert error.value.status_code == 403
    assert error.value.next_action == "reauthorize"
    assert "private-body" not in str(error.value)


def test_mcp_payloads_are_bounded_and_reject_sensitive_fields() -> None:
    transport = RecordingTransport(
        {
            "get_run": {**_run(), "user_id": "must-not-cross-boundary"},
        }
    )

    with pytest.raises(ZentureMCPProtocolError):
        McpClient(transport).get_run(RUN_ID)

    malformed_payload: object = {1: "invalid-key"}
    malformed = RecordingTransport({"get_run": malformed_payload})
    with pytest.raises(ZentureMCPProtocolError):
        McpClient(malformed).get_run(RUN_ID)


@pytest.mark.parametrize(
    ("result", "error_code"),
    [
        (None, "invalid_result"),
        ({"value": object()}, "invalid_json"),
        ({"value": "x" * (256 * 1024)}, "result_too_large"),
        ({"error": "invalid"}, "invalid_error"),
    ],
)
def test_mcp_result_boundary_rejects_malformed_or_unbounded_payloads(
    result: object, error_code: str
) -> None:
    with pytest.raises(ZentureMCPProtocolError) as error:
        McpClient(RawTransport(result)).get_run(RUN_ID)

    assert error.value.code == error_code


def test_mcp_result_boundary_accepts_protocol_structured_content_alias() -> None:
    read = McpClient(RawTransport({"structuredContent": _run()})).get_run(RUN_ID)

    assert read.run.run_id == RUN_ID


def test_mcp_result_boundary_accepts_bounded_json_text_content() -> None:
    result = TextOnlyToolResult(
        content=[TextContent(type="text", text=json.dumps(_run(), separators=(",", ":")))]
    )

    read = McpClient(RawTransport(result)).get_run(RUN_ID)

    assert read.run.run_id == RUN_ID


def test_mcp_result_boundary_decodes_bounded_json_text_errors() -> None:
    result = TextOnlyToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {"error": {"code": "forbidden", "http_status": 403}},
                    separators=(",", ":"),
                ),
            )
        ],
        is_error=True,
    )

    with pytest.raises(ZentureMCPError) as error:
        McpClient(RawTransport(result)).get_run(RUN_ID)

    assert error.value.code == "forbidden"


def test_mcp_result_boundary_rejects_mcp_error_flag_without_safe_error_envelope() -> None:
    result = {
        "isError": True,
        "structured_content": {"status": "not-an-error-envelope"},
    }

    with pytest.raises(ZentureMCPProtocolError, match="invalid_error"):
        McpClient(RawTransport(result)).get_run(RUN_ID)


def test_mcp_error_envelope_rejects_invalid_error_code() -> None:
    error_payload = {
        "error": {"code": "bad code", "http_status": 400, "retryable": False, "next_action": "x"}
    }

    with pytest.raises(ZentureMCPProtocolError, match="invalid_error_code"):
        McpClient(RawTransport(error_payload)).get_run(RUN_ID)


@pytest.mark.parametrize(
    ("error_payload", "expected_retryable"),
    [
        (
            {"error": {"code": "bad", "http_status": "400", "retryable": "no", "next_action": 1}},
            False,
        ),
        (
            {
                "error": {
                    "code": "bad",
                    "http_status": 700,
                    "retryable": True,
                    "next_action": "x" * 129,
                    "request_id": 1,
                    "retry_after_seconds": -1,
                }
            },
            True,
        ),
    ],
)
def test_mcp_error_envelope_normalizes_invalid_metadata(
    error_payload: dict[str, object],
    expected_retryable: bool,
) -> None:
    with pytest.raises(ZentureMCPError) as error:
        McpClient(RawTransport(error_payload)).get_run(RUN_ID)

    assert error.value.status_code == 502
    assert error.value.retryable is expected_retryable
    assert error.value.next_action == "check_request"


def test_mcp_error_envelope_preserves_bounded_optional_metadata() -> None:
    error_payload = {
        "error": {
            "code": "capacity_unavailable",
            "http_status": 503,
            "retryable": True,
            "next_action": "retry_later",
            "request_id": "req_1",
            "retry_after_seconds": 2,
        }
    }

    with pytest.raises(ZentureMCPError) as error:
        McpClient(RawTransport(error_payload)).get_run(RUN_ID)

    assert error.value.status_code == 503
    assert error.value.retryable is True
    assert error.value.next_action == "retry_later"
    assert error.value.request_id == "req_1"
    assert error.value.retry_after_seconds == 2


def test_mcp_client_maps_invalid_result_contract_to_protocol_error() -> None:
    with pytest.raises(ZentureMCPProtocolError, match="invalid_result_contract"):
        McpClient(RawTransport({})).get_run(RUN_ID)


def test_mcp_client_maps_transport_failures_to_safe_typed_error() -> None:
    client = McpClient(RaisingTransport())

    with pytest.raises(ZentureMCPError, match="mcp_transport_unavailable"):
        client.list_tools()
    with pytest.raises(ZentureMCPError, match="mcp_transport_unavailable"):
        client.get_run(RUN_ID)

    with pytest.raises(ZentureMCPError, match="known_transport_error"):
        McpClient(MCPErrorTransport()).get_run(RUN_ID)


@pytest.mark.asyncio
async def test_async_mcp_client_preserves_typed_transport_errors() -> None:
    client = AsyncMcpClient(AsyncMCPErrorTransport())
    with pytest.raises(ZentureMCPError, match="known_transport_error"):
        await client.list_tools()
    with pytest.raises(ZentureMCPError, match="known_transport_error"):
        await client.get_run(RUN_ID)

    with pytest.raises(ZentureMCPProtocolError, match="event_replay_missing"):
        await AsyncMcpClient(AsyncRecordingTransport({"get_run": _run()})).replay_events(RUN_ID)


def test_mcp_client_requires_the_complete_product_catalog() -> None:
    with pytest.raises(ZentureMCPProtocolError, match="tool_catalog_incomplete"):
        McpClient(RecordingTransport(_responses(), tool_names=("run",))).require_product_tools()


def test_mcp_client_replay_requires_an_explicit_replay_page() -> None:
    with pytest.raises(ZentureMCPProtocolError, match="event_replay_missing"):
        McpClient(RecordingTransport({"get_run": _run()})).replay_events(RUN_ID)

    replay = McpClient(RecordingTransport(_responses())).replay_events(
        RUN_ID, cursor="cursor_0", limit=1
    )
    assert replay.events[0].event_id == "event_1"

    with pytest.raises(ZentureMCPProtocolError, match="invalid_event_replay"):
        McpClient(RecordingTransport({"get_run": {**_run(), "event_replay": "invalid"}})).get_run(
            RUN_ID, replay_cursor="cursor_0"
        )


def test_mcp_client_replay_requests_the_first_page_explicitly() -> None:
    transport = RecordingTransport(_responses())

    replay = McpClient(transport).replay_events(RUN_ID)

    assert isinstance(replay, ListRunEventsResponse)
    assert transport.calls[0][1] == {
        "run_id": RUN_ID,
        "view": "summary",
        "replay_limit": 50,
        "include_event_replay": True,
    }


def test_sync_mcp_replay_rejects_a_foreign_run_projection_and_event() -> None:
    foreign_run = "run_aaaaaaaaaaaaaaaa"
    foreign_projection = {**_run(), "run_id": foreign_run, "event_replay": _events()}
    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        McpClient(RecordingTransport({"get_run": foreign_projection})).get_run(RUN_ID)

    foreign_events = _events()
    foreign_events["events"][0]["run_id"] = foreign_run  # type: ignore[index]
    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        McpClient(
            RecordingTransport({"get_run": {**_run(), "event_replay": foreign_events}})
        ).replay_events(RUN_ID)


@pytest.mark.asyncio
async def test_async_mcp_replay_rejects_a_foreign_run_projection_and_event() -> None:
    foreign_run = "run_aaaaaaaaaaaaaaaa"
    foreign_projection = {**_run(), "run_id": foreign_run, "event_replay": _events()}
    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        await AsyncMcpClient(AsyncRecordingTransport({"get_run": foreign_projection})).get_run(
            RUN_ID
        )

    foreign_events = _events()
    foreign_events["events"][0]["run_id"] = foreign_run  # type: ignore[index]
    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        await AsyncMcpClient(
            AsyncRecordingTransport({"get_run": {**_run(), "event_replay": foreign_events}})
        ).replay_events(RUN_ID)


@pytest.mark.parametrize("operation", ["cancel_run", "record_run_outcome"])
def test_sync_mcp_run_mutation_rejects_a_foreign_run_projection(operation: str) -> None:
    response = _run(run_id="run_aaaaaaaaaaaaaaaa")
    client = McpClient(RecordingTransport({operation: response}))

    operation_call = (
        partial(client.cancel_run, RUN_ID)
        if operation == "cancel_run"
        else partial(client.record_run_outcome, RUN_ID, outcome="used")
    )

    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        operation_call()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel_run", "record_run_outcome"])
async def test_async_mcp_run_mutation_rejects_a_foreign_run_projection(
    operation: str,
) -> None:
    response = _run(run_id="run_aaaaaaaaaaaaaaaa")
    client = AsyncMcpClient(AsyncRecordingTransport({operation: response}))

    operation_call = (
        client.cancel_run(RUN_ID)
        if operation == "cancel_run"
        else client.record_run_outcome(RUN_ID, outcome="used")
    )

    with pytest.raises(ZentureMCPProtocolError, match="run_id_mismatch"):
        await operation_call


def test_mcp_tool_catalog_rejects_invalid_and_unbounded_descriptors() -> None:
    with pytest.raises(ZentureMCPProtocolError, match="invalid_tool_catalog"):
        McpClient(RecordingTransport(_responses(), tool_names=("Run",))).list_tools()

    too_many_names = tuple(f"tool_{index}" for index in range(129))
    with pytest.raises(ZentureMCPProtocolError, match="tool_catalog_too_large"):
        McpClient(RecordingTransport(_responses(), tool_names=too_many_names)).list_tools()

    catalogs = cast("tuple[object, ...]", ({"tools": "invalid"}, {"missing": []}, object()))
    for catalog in catalogs:
        with pytest.raises(ZentureMCPProtocolError, match="invalid_tool_catalog"):
            McpClient(CatalogTransport(catalog)).list_tools()


def test_mcp_endpoint_accepts_canonical_hosts_and_rejects_arbitrary_origins() -> None:
    assert McpEndpoint.from_value("https://mcp.zenture.app").url == "https://mcp.zenture.app/"
    assert McpEndpoint.from_value("https://mcp-int.zenture.app/").url.endswith("/")
    assert McpEndpoint.from_value("http://127.0.0.1:8100").url.endswith(":8100/")

    with pytest.raises(ValueError, match="approved zenture origin"):
        McpEndpoint.from_value("https://untrusted.example/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        McpEndpoint.from_value("http://mcp.zenture.app")
    with pytest.raises(ValueError, match="approved zenture origin"):
        McpEndpoint("https://untrusted.example/")


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://user:pass@mcp.zenture.app/", "user information"),
        ("https://mcp.zenture.app/?token=hidden", "query"),
        ("https://mcp.zenture.app/mcp", "hosted root"),
        ("https://mcp.zenture.app:444/", "canonical HTTPS port"),
        ("https://mcp.zenture.app:bad/", "port is invalid"),
        ("", "non-empty"),
    ],
)
def test_mcp_endpoint_rejects_unsafe_url_shapes(endpoint: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        McpEndpoint.from_value(endpoint)


@pytest.mark.parametrize(
    ("factory", "value", "message"),
    [
        (
            "artifact",
            {"file_name": "a", "mime_type": "text/plain", "byte_size": 1, "content_hash": "bad"},
            "lowercase SHA",
        ),
        (
            "artifact",
            {"file_name": "a", "mime_type": "text/plain", "byte_size": 1, "content_hash": None},
            "lowercase SHA",
        ),
        ("list", {"status": tuple("x" for _ in range(9))}, "eight short values"),
        ("list", {"cursor": "cursor+plus"}, "cursor is invalid"),
        ("get", {"run_id": RUN_ID, "replay_cursor": "cursor\nheader"}, "replay_cursor is invalid"),
        ("outcome", {"run_id": "bad", "outcome": "used"}, "run_id is invalid"),
        (
            "outcome",
            {"run_id": RUN_ID, "outcome": "used", "edited_artifact_ref": ARTIFACT_REF},
            "edited outcome requires",
        ),
        (
            "outcome",
            {"run_id": RUN_ID, "outcome": "edited", "edited_artifact_ref": "bad"},
            "edited_artifact_ref",
        ),
    ],
)
def test_mcp_request_models_reject_invalid_or_ambiguous_inputs(
    factory: str, value: dict[str, object], message: str
) -> None:
    from zenture._mcp.contracts import (
        McpArtifactRequest,
        McpGetRunRequest,
        McpListRunsRequest,
        McpOutcomeRequest,
    )

    factories: dict[str, type[BaseModel]] = {
        "artifact": McpArtifactRequest,
        "list": McpListRunsRequest,
        "get": McpGetRunRequest,
        "outcome": McpOutcomeRequest,
    }
    with pytest.raises(ValueError, match=message):
        factories[factory].model_validate(value)


@pytest.mark.parametrize(
    "cursor",
    [
        1,
        b"cursor_bytes",
        ["cursor_list"],
        {"cursor": "mapping"},
        "cursor with spaces",
        "cursor\theader",
        "cursor\nheader",
        "x" * 513,
    ],
)
def test_sync_mcp_cursor_inputs_fail_with_stable_contract_errors(cursor: object) -> None:
    transport = RecordingTransport(_responses())
    client = McpClient(transport)

    with pytest.raises(ValueError, match="cursor is invalid"):
        client.list_runs(cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match="replay_cursor is invalid"):
        client.get_run(RUN_ID, replay_cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match="replay_cursor is invalid"):
        client.replay_events(RUN_ID, cursor=cast("Any", cursor))

    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor",
    [
        1,
        b"cursor_bytes",
        ["cursor_list"],
        {"cursor": "mapping"},
        "cursor with spaces",
        "cursor\theader",
        "cursor\nheader",
        "x" * 513,
    ],
)
async def test_async_mcp_cursor_inputs_fail_with_stable_contract_errors(cursor: object) -> None:
    transport = AsyncRecordingTransport(_responses())
    client = AsyncMcpClient(transport)

    with pytest.raises(ValueError, match="cursor is invalid"):
        await client.list_runs(cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match="replay_cursor is invalid"):
        await client.get_run(RUN_ID, replay_cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match="replay_cursor is invalid"):
        await client.replay_events(RUN_ID, cursor=cast("Any", cursor))

    assert transport.calls == []


def test_mcp_tool_catalog_rejects_duplicate_names() -> None:
    transport = RecordingTransport(
        _responses(),
        tool_names=(*PRODUCT_TOOL_NAMES, "run"),
    )

    with pytest.raises(ZentureMCPProtocolError, match="duplicate"):
        McpClient(transport).require_product_tools()


def test_sync_mcp_arguments_reject_invalid_run_and_unbounded_inputs() -> None:
    transport = RecordingTransport({"get_run": _run()})
    client = McpClient(transport)

    with pytest.raises(ValueError, match="run_id is invalid"):
        client.get_run("not-a-run")
    with pytest.raises(ValueError, match="greater than or equal"):
        client.list_runs(limit=0)
    with pytest.raises(ValueError, match="blank"):
        client.run(task=" ", artifact={"type": "text", "value": "text"})


def test_mcp_run_input_uses_gateway_text_and_reference_bounds() -> None:
    from zenture._contract import PrepareKnowledgeRunRequest
    from zenture._mcp.contracts import McpOutcomeRequest

    PrepareKnowledgeRunRequest.model_validate(
        {"task": "x" * 20_000, "artifact": {"type": "text", "value": "x" * 50_000}}
    )
    with pytest.raises(ValueError, match="validation error"):
        PrepareKnowledgeRunRequest.model_validate(
            {"task": "x", "artifact": {"type": "text", "value": "x" * 50_001}}
        )
    with pytest.raises(ValueError, match="selected text"):
        PrepareKnowledgeRunRequest.model_validate(
            {"task": "x", "artifact": {"type": "text", "value": "data:text/plain,x"}}
        )

    McpOutcomeRequest(
        run_id="run_abc",
        outcome="edited",
        edited_artifact_ref="art_" + "a" * 128,
    )
    with pytest.raises(ValueError, match="edited_artifact_ref"):
        McpOutcomeRequest(
            run_id="run_abc",
            outcome="edited",
            edited_artifact_ref="art_" + "a" * 129,
        )


def test_mcp_adapter_modules_do_not_import_server_or_private_runtime_clients() -> None:
    from pathlib import Path

    root = Path(__file__).parents[2] / "src" / "zenture" / "_mcp"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "zenture_mcp_server" not in source
    assert "redis" not in source.lower()
    assert "supabase" not in source.lower()


@pytest.mark.asyncio
async def test_official_streamable_transport_is_lazy_and_credential_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    requested: list[str] = []
    observed: dict[str, object] = {}

    class StreamContext:
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_exc_info: object) -> None:
            observed["stream_closed"] = True

    class SessionContext:
        def __init__(self, *_streams: object) -> None:
            pass

        async def __aenter__(self) -> SessionContext:
            observed["session_opened"] = True
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            observed["session_closed"] = True

        async def initialize(self) -> None:
            observed["initialized"] = True

        async def list_tools(self) -> object:
            return {"tools": [{"name": "run"}]}

        async def call_tool(self, name: str, *, arguments: dict[str, object]) -> object:
            return _tool_result({"name": name, "arguments": arguments})

    class Timeout:
        def __init__(self, value: float, *, read: float) -> None:
            self.value = value
            self.read = read

        def __eq__(self, other: object) -> bool:
            return (
                isinstance(other, Timeout) and self.value == other.value and self.read == other.read
            )

    class HttpClientContext:
        def __init__(self, **kwargs: object) -> None:
            observed["http_kwargs"] = kwargs

        async def __aenter__(self) -> HttpClientContext:
            observed["http_opened"] = True
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            observed["http_closed"] = True

    def import_module(name: str) -> object:
        requested.append(name)
        if name == "mcp":
            return SimpleNamespace(ClientSession=SessionContext)
        if name == "mcp.client.streamable_http":

            def streamablehttp_client(*_args: object, **kwargs: object) -> StreamContext:
                observed["stream_kwargs"] = kwargs
                return StreamContext()

            return SimpleNamespace(streamable_http_client=streamablehttp_client)
        if name == "httpx2":
            return SimpleNamespace(AsyncClient=HttpClientContext, Timeout=Timeout)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    async with transport_module.open_streamable_http_transport(
        "https://mcp.zenture.app",
        bearer_token=lambda: "opaque",
    ) as transport:
        await transport.list_tools()
        await transport.call_tool("run", {"task": "selected"})

    assert requested == ["mcp", "mcp.client.streamable_http", "httpx2"]
    assert observed["initialized"] is True
    assert observed["session_closed"] is True
    assert observed["stream_closed"] is True
    assert observed["http_kwargs"] == {
        "headers": {"Authorization": "Bearer opaque"},
        "timeout": Timeout(30.0, read=30.0),
        "follow_redirects": False,
    }
    stream_kwargs = observed["stream_kwargs"]
    assert isinstance(stream_kwargs, dict)
    assert "http_client" in stream_kwargs


@pytest.mark.asyncio
async def test_official_transport_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    class Timeout:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class CancelledClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> CancelledClient:
            raise asyncio.CancelledError

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    def import_module(name: str) -> object:
        if name == "mcp":
            return SimpleNamespace(ClientSession=object)
        if name == "mcp.client.streamable_http":
            return SimpleNamespace(streamable_http_client=object)
        if name == "httpx2":
            return SimpleNamespace(AsyncClient=CancelledClient, Timeout=Timeout)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(asyncio.CancelledError):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
        ):
            raise AssertionError("transport should not open")


@pytest.mark.asyncio
async def test_official_transport_preserves_typed_mcp_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    class Timeout:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class HttpClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> HttpClient:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    class FailingStream:
        async def __aenter__(self) -> tuple[object, object]:
            raise ZentureMCPError("known_transport_error")

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    def import_module(name: str) -> object:
        def failing_stream(*args: object, **kwargs: object) -> FailingStream:
            del args, kwargs
            return FailingStream()

        if name == "mcp":
            return SimpleNamespace(ClientSession=object)
        if name == "mcp.client.streamable_http":
            return SimpleNamespace(streamable_http_client=failing_stream)
        if name == "httpx2":
            return SimpleNamespace(AsyncClient=HttpClient, Timeout=Timeout)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(ZentureMCPError, match="known_transport_error"):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
        ):
            raise AssertionError("transport should not open")


@pytest.mark.asyncio
async def test_official_transport_maps_session_failures_to_safe_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    class FailingClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FailingClient:
            raise RuntimeError("remote body must not escape")

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    def import_module(name: str) -> object:
        def failing_stream(*args: object, **kwargs: object) -> object:
            del args, kwargs
            return object()

        if name == "mcp":
            return SimpleNamespace(ClientSession=object)
        if name == "mcp.client.streamable_http":
            return SimpleNamespace(streamable_http_client=failing_stream)
        if name == "httpx2":
            return SimpleNamespace(AsyncClient=FailingClient, Timeout=object)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(ZentureMCPError, match="mcp_transport_unavailable"):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
        ):
            raise AssertionError("transport should not open")


@pytest.mark.asyncio
async def test_official_transport_preserves_body_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    class Context:
        async def __aenter__(self) -> Context:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    class Session(Context):
        def __init__(self, *_args: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

    def import_module(name: str) -> object:
        if name == "mcp":
            return SimpleNamespace(ClientSession=Session)
        if name == "mcp.client.streamable_http":
            class StreamContext(Context):
                async def __aenter__(self) -> tuple[object, object]:
                    return object(), object()

            return SimpleNamespace(
                streamable_http_client=lambda *_args, **_kwargs: StreamContext()
            )
        if name == "httpx2":
            class Timeout:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

            class HttpClient(Context):
                def __init__(self, **_kwargs: object) -> None:
                    pass

            return SimpleNamespace(AsyncClient=HttpClient, Timeout=Timeout)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(RuntimeError, match="body failure"):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
        ):
            raise RuntimeError("body failure")


@pytest.mark.asyncio
async def test_official_transport_does_not_fail_after_successful_body_on_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    class Context:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Context:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            pass

    class FailingStream(Context):
        async def __aenter__(self) -> tuple[object, object]:
            return object(), object()

        async def __aexit__(self, *_exc_info: object) -> None:
            raise RuntimeError("close failure")

    class Session(Context):
        async def initialize(self) -> None:
            pass

    def import_module(name: str) -> object:
        if name == "mcp":
            return SimpleNamespace(ClientSession=Session)
        if name == "mcp.client.streamable_http":
            return SimpleNamespace(
                streamable_http_client=lambda *_args, **_kwargs: FailingStream()
            )
        if name == "httpx2":
            class Timeout:
                def __init__(self, *_args: object, **_kwargs: object) -> None:
                    pass

            return SimpleNamespace(AsyncClient=Context, Timeout=Timeout)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", import_module)
    async with transport_module.open_streamable_http_transport(
        "https://mcp.zenture.app",
        bearer_token="opaque",
    ):
        pass


@pytest.mark.asyncio
async def test_official_transport_rejects_invalid_timeout_before_optional_import() -> None:
    import zenture._mcp.transport as transport_module

    with pytest.raises(ValueError, match="between 0 and 300"):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
            timeout=0,
        ):
            raise AssertionError("transport should not open")


def test_bearer_resolution_is_caller_owned_and_bounded() -> None:
    from zenture._mcp.transport import resolve_bearer_token

    assert resolve_bearer_token(lambda: "opaque") == "opaque"
    with pytest.raises(ValueError, match="bounded"):
        resolve_bearer_token(" ")
    with pytest.raises(ValueError, match="bounded"):
        resolve_bearer_token("x" * 4097)

    async def deferred_value() -> str:
        return "opaque"

    pending = deferred_value()
    with pytest.raises(TypeError, match="asynchronous"):
        resolve_bearer_token(pending)  # type: ignore[arg-type]
    pending.close()


@pytest.mark.asyncio
async def test_async_bearer_resolution_accepts_a_deferred_caller_value() -> None:
    from zenture._mcp.transport import resolve_async_bearer_token

    async def provider() -> str:
        return "opaque"

    assert await resolve_async_bearer_token(provider) == "opaque"
    with pytest.raises(ValueError, match="bounded"):
        await resolve_async_bearer_token(" ")
    with pytest.raises(ValueError, match="bounded"):
        await resolve_async_bearer_token("x" * 4097)


@pytest.mark.asyncio
async def test_official_transport_reports_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.transport as transport_module

    def import_module(_name: str) -> object:
        raise ImportError("optional dependency not installed")

    monkeypatch.setattr(importlib, "import_module", import_module)
    with pytest.raises(ZentureMCPDependencyError):
        async with transport_module.open_streamable_http_transport(
            "https://mcp.zenture.app",
            bearer_token="opaque",
        ):
            raise AssertionError("transport should not open")
