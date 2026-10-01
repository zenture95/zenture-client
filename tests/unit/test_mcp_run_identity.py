"""Run identity receipts across native peer dispatch and decoding boundaries."""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

import pytest

from zenture._contract import PrepareKnowledgeRunRequest, PublicRunResponse
from zenture.errors import ZentureMCPError, ZentureMCPProtocolError
from zenture.mcp import AsyncMcpClient, McpClient

ARTIFACT = {"type": "text", "value": "selected answer"}
KEY = "Review_A-123"


def receipt(key: object) -> dict[str, object]:
    return {
        "run_id": "run_abc123",
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": "queued",
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-01T12:00:00Z",
        "artifact_refs": [],
        "idempotency_key": key,
    }


class PeerTransport:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[Mapping[str, object]] = []

    def list_tools(self) -> object:
        return {"tools": []}

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        assert name == "run"
        self.calls.append(arguments)
        key = arguments.get("idempotency_key")
        assert isinstance(key, str), "Run must allocate its identity before dispatch"
        if self.mode == "transport":
            raise RuntimeError("private transport payload")
        if self.mode == "typed":
            raise ZentureMCPError("capacity_unavailable", status_code=503)
        if self.mode == "cancel":
            raise asyncio.CancelledError
        payload = receipt(key)
        if self.mode == "foreign":
            payload["idempotency_key"] = "foreign_key"
        elif self.mode == "missing":
            payload.pop("idempotency_key")
        elif self.mode == "decode":
            payload["generation"] = "malformed"
        elif self.mode.startswith("tool"):
            payload = {
                "idempotency_key": key,
                "error": {
                    "code": "idempotency_conflict",
                    "http_status": 409,
                    "retryable": False,
                    "next_action": "check_request",
                    "reason": "private tool payload",
                },
            }
            if self.mode == "tool_foreign":
                payload["idempotency_key"] = "foreign_key"
            if self.mode == "tool_missing":
                payload.pop("idempotency_key")
        if self.mode == "json":
            return {"content": [{"type": "text", "text": "malformed JSON"}]}
        if self.mode == "text":
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}
        return {"structuredContent": payload}


class AsyncPeerTransport(PeerTransport):
    async def list_tools(self) -> object:
        return super().list_tools()

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        await asyncio.sleep(0)
        return super().call_tool(name, arguments)


async def invoke(peer: McpClient | AsyncMcpClient, **kwargs: Any) -> object:
    # Capture absent API behavior as an assertion-level failure during RED.
    try:
        if isinstance(peer, AsyncMcpClient):
            return await peer.run(task="Review", artifact=ARTIFACT, **kwargs)
        return peer.run(task="Review", artifact=ARTIFACT, **kwargs)
    except Exception as exc:
        return exc


def peers(
    async_peer: bool, mode: str = "success"
) -> tuple[McpClient | AsyncMcpClient, PeerTransport]:
    if async_peer:
        transport = AsyncPeerTransport(mode)
        return AsyncMcpClient(transport), transport
    sync_transport = PeerTransport(mode)
    return McpClient(sync_transport), sync_transport


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
@pytest.mark.parametrize("mode", ["success", "text"])
@pytest.mark.parametrize("key", [KEY, "A", "Z" * 128])
async def test_run_explicit_identity_receipt_preserves_rest_shape(
    async_peer: bool, mode: str, key: str
) -> None:
    peer, transport = peers(async_peer, mode)
    result = await invoke(peer, idempotency_key=key)
    assert isinstance(result, PublicRunResponse)
    assert getattr(result, "idempotency_key", None) == key
    assert result.run_id == "run_abc123"
    assert result.profile.value == "standard"
    assert "idempotency_key" not in result.model_dump()
    assert "idempotency_key" not in result.model_dump(mode="json")
    wire = dict(transport.calls[0])
    assert wire.pop("idempotency_key") == key
    prepared = PrepareKnowledgeRunRequest.model_validate(wire)
    assert "idempotency_key" not in prepared.model_dump()
    public = PublicRunResponse.model_validate(
        {k: v for k, v in receipt(key).items() if k != "idempotency_key"}
    )
    assert result.model_dump() == public.model_dump()
    assert not hasattr(public, "idempotency_key")
    again = await invoke(peer, idempotency_key=key)
    assert isinstance(again, PublicRunResponse)
    assert transport.calls[0] == transport.calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
async def test_run_allocates_fresh_identity_before_each_deliberate_dispatch(
    async_peer: bool,
) -> None:
    peer, transport = peers(async_peer)
    first, second = await invoke(peer), await invoke(peer)
    assert isinstance(first, PublicRunResponse)
    assert isinstance(second, PublicRunResponse)
    keys = [call["idempotency_key"] for call in transport.calls]
    assert all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) for key in keys)
    assert len(set(keys)) == 2
    assert getattr(first, "idempotency_key", None) == keys[0]
    assert getattr(second, "idempotency_key", None) == keys[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
@pytest.mark.parametrize(
    "key", [None, 123, True, b"key", "", " spaced", "spaced ", "a.b", "a:b", "ä", "x\n", "x" * 129]
)
async def test_run_rejects_invalid_explicit_identity_before_send(
    async_peer: bool, key: object
) -> None:
    peer, transport = peers(async_peer)
    result = await invoke(peer, idempotency_key=key)
    assert isinstance(result, ValueError)
    assert transport.calls == []
    assert str(key) not in str(result) or key == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("missing", "idempotency_key_mismatch"),
        ("foreign", "idempotency_key_mismatch"),
        ("tool_missing", "idempotency_key_mismatch"),
        ("tool_foreign", "idempotency_key_mismatch"),
        ("decode", "invalid_result_contract"),
        ("json", "invalid_result"),
        ("transport", "mcp_transport_unavailable"),
        ("typed", "capacity_unavailable"),
        ("tool", "idempotency_conflict"),
    ],
)
async def test_run_errors_retain_local_identity_without_retry_or_payload(
    async_peer: bool, mode: str, code: str
) -> None:
    peer, transport = peers(async_peer, mode)
    result = await invoke(peer, idempotency_key=KEY)
    assert isinstance(result, ZentureMCPError)
    assert result.code == code
    assert isinstance(result, ZentureMCPProtocolError) == (
        mode in {"missing", "foreign", "tool_missing", "tool_foreign", "decode", "json"}
    )
    assert getattr(result, "idempotency_key", None) == KEY
    assert len(transport.calls) == 1
    assert KEY not in str(result) + repr(result)
    assert "private" not in str(result) + repr(result)
    if mode == "tool":
        assert result.status_code == 409
        assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
async def test_run_cancellation_propagates(async_peer: bool) -> None:
    peer, transport = peers(async_peer, "cancel")
    with pytest.raises(asyncio.CancelledError):
        await invoke(peer, idempotency_key=KEY)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_async_concurrent_runs_keep_success_and_error_identity_isolated() -> None:
    class ContendingTransport(AsyncPeerTransport):
        async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            self.calls.append(arguments)
            if len(self.calls) < 2:
                await ready.wait()
            else:
                ready.set()
            key = arguments["idempotency_key"]
            if key == "attempt_A":
                raise RuntimeError("private payload")
            return {"structuredContent": receipt(key)}

    ready = asyncio.Event()
    transport = ContendingTransport()
    peer = AsyncMcpClient(transport)
    failed, succeeded = await asyncio.gather(
        invoke(peer, idempotency_key="attempt_A"), invoke(peer, idempotency_key="attempt_B")
    )
    assert isinstance(failed, ZentureMCPError)
    assert isinstance(succeeded, PublicRunResponse)
    assert getattr(failed, "idempotency_key", None) == "attempt_A"
    assert getattr(succeeded, "idempotency_key", None) == "attempt_B"
    assert len(transport.calls) == 2


def test_sync_concurrent_runs_keep_identity_isolated() -> None:
    barrier = Barrier(2)

    class ContendingTransport(PeerTransport):
        def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
            barrier.wait(timeout=5)
            return super().call_tool(name, arguments)

    peer = McpClient(ContendingTransport())

    def run(key: str) -> PublicRunResponse:
        return peer.run(task="Review", artifact=ARTIFACT, idempotency_key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                run,
                ["attempt_A", "attempt_B"],
            )
        )
    assert [getattr(result, "idempotency_key", None) for result in results] == [
        "attempt_A",
        "attempt_B",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_peer", [False, True])
async def test_run_generated_failure_keeps_once_allocated_key(
    async_peer: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._mcp.client as client_module

    allocations: list[str] = []
    original = uuid4

    def allocate() -> UUID:
        key = original()
        allocations.append(key.hex)
        return key

    monkeypatch.setattr(client_module, "uuid4", allocate)
    peer, transport = peers(async_peer, "transport")
    result = await invoke(peer)
    assert isinstance(result, ZentureMCPError)
    assert len(allocations) == 1
    assert transport.calls[0]["idempotency_key"] == allocations[0]
    assert result.idempotency_key == allocations[0]
    result = await invoke(peer, idempotency_key=KEY)
    assert isinstance(result, ZentureMCPError)
    assert result.idempotency_key == KEY
    assert len(allocations) == 1
