"""Local REST and SSE contract tests for the public Run resources."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from zenture import AsyncZentureClient, ZentureClient
from zenture._contract import PublicRunEvent, PublicRunHeartbeat, PublicRunResponse
from zenture._contract.run_references import validate_run_cursor
from zenture._resources.runs import RunEventStreamState
from zenture.errors import (
    ZenturePollingStoppedError,
    ZenturePollingTimeoutError,
    ZentureResponseError,
    ZentureTransportError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

# Deterministic synthetic fixture for MockTransport-only tests; no real credentials/network.
API_KEY = f"zt_live_{UUID(int=0).hex}"
RUN_ID = "run_33333333333343338333333333333333"
PROPOSAL_ID = "33333333-3333-4333-8333-333333333333"
MAX_STREAM_EVENT_BYTES = 512 * 1024


def _proposal() -> dict[str, object]:
    return {
        "proposal_id": PROPOSAL_ID,
        "proposal_version": 1,
        "expires_at": "2026-08-30T12:30:00Z",
        "inferred_work_type": "answer",
        "task_contract_summary": {
            "work_type": "answer",
            "summary_ref": "snapshot:task-summary-v1",
            "requirement_count": 1,
        },
        "planned_checks": ["check:material-claims"],
        "unavailable_checks": [],
        "expected_duration_seconds": 30,
        "estimated_credits": "12",
        "maximum_credits": "24",
        "start_admissible": True,
        "proposal_hash": "a" * 64,
    }


def _run(
    *, status: str = "queued", run_id: str = RUN_ID, deadline_at: str | None = None
) -> dict[str, object]:
    response: dict[str, object] = {
        "run_id": run_id,
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": status,
        "created_at": "2026-08-30T12:00:00Z",
        "updated_at": "2026-08-30T12:00:00Z",
        "queue": {"queue_reason": "queue:admitted", "jobs_ahead": 0},
        "cancellation_requested": False,
    }
    if deadline_at is not None:
        response["deadline_at"] = deadline_at
    return response


def _max_jitter(_lower: float, upper: float) -> float:
    return upper


def test_public_run_response_coerces_wire_decision_to_strict_enum() -> None:
    response = PublicRunResponse.model_validate(
        {**_run(status="succeeded"), "acceptance_decision": "ready"}
    )

    assert response.acceptance_decision is not None
    assert response.acceptance_decision.value == "ready"


@pytest.mark.parametrize("status", ["expired", "cancel_requested", "budget_exhausted"])
def test_public_run_event_accepts_lifecycle_statuses(status: str) -> None:
    event = PublicRunEvent.model_validate(
        {
            "type": "run.event",
            "event_id": "event_lifecycle",
            "run_id": RUN_ID,
            "sequence": 1,
            "status": status,
            "message_key": f"run.status.{status}",
            "event_cursor": "cursor_lifecycle",
        }
    )

    assert event.status == status


def test_public_run_event_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError, match="status"):
        PublicRunEvent.model_validate(
            {
                "type": "run.event",
                "event_id": "event_unknown",
                "run_id": RUN_ID,
                "sequence": 1,
                "status": "future_status",
                "message_key": "run.status.future_status",
                "event_cursor": "cursor_unknown",
            }
        )


@pytest.mark.parametrize("status", ["completed", "succeeded"])
def test_sync_run_list_forwards_success_filter_and_reads_success_status(status: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "runs": [
                    {
                        "run_id": RUN_ID,
                        "status": status,
                        "profile": "standard",
                        "created_at": "2026-08-30T12:00:00Z",
                        "updated_at": "2026-08-30T12:00:00Z",
                    }
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.list(status=[status])

    assert requests[0].url.path == "/v1/runs"
    assert requests[0].url.params["status"] == status
    assert result.runs[0].status.value == status
    client.close()


def _sse_event(*, event_id: str, sequence: int, status: str, event_cursor: str) -> bytes:
    payload = {
        "type": "run.event",
        "event_id": event_id,
        "run_id": RUN_ID,
        "sequence": sequence,
        "status": status,
        "message_key": f"run.status.{status}",
        "event_cursor": event_cursor,
    }
    return f"event: run.event\ndata: {json.dumps(payload)}\n\n".encode()


def _sse_error(*, event_id: str, sequence: int, retryable: bool) -> bytes:
    payload = {
        "type": "run.error",
        "event_id": event_id,
        "run_id": RUN_ID,
        "sequence": sequence,
        "code": "dependency_unavailable",
        "message": "stream temporarily unavailable",
        "retryable": retryable,
        "terminal": not retryable,
    }
    return f"event: run.error\ndata: {json.dumps(payload)}\n\n".encode()


def _sse_heartbeat(*, event_id: str, sequence: int) -> bytes:
    payload = {
        "type": "run.heartbeat",
        "event_id": event_id,
        "run_id": RUN_ID,
        "sequence": sequence,
    }
    return f"event: run.heartbeat\ndata: {json.dumps(payload)}\n\n".encode()


def _sse_message_for_run(message_type: str, run_id: str) -> bytes:
    common: dict[str, object] = {
        "type": message_type,
        "event_id": "foreign_1",
        "run_id": run_id,
        "sequence": 1,
    }
    if message_type == "run.event":
        common.update(
            status="running",
            message_key="run.status.running",
            event_cursor="cursor_foreign",
        )
    elif message_type == "run.error":
        common.update(
            code="dependency_unavailable",
            message="temporary",
            retryable=False,
            terminal=True,
        )
    return f"event: {message_type}\ndata: {json.dumps(common)}\n\n".encode()


def _stream_response(*parts: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(parts),
    )


def _reconnect_responses() -> list[httpx.Response]:
    return [
        _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            )
        ),
        _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            ),
            _sse_event(
                event_id="event_bbb",
                sequence=1,
                status="running",
                event_cursor="cursor_bbb",
            ),
            _sse_event(
                event_id="event_aaa",
                sequence=2,
                status="running",
                event_cursor="cursor_ccc",
            ),
            _sse_error(event_id="error_aaa", sequence=4, retryable=True),
        ),
        _stream_response(
            _sse_event(
                event_id="event_ccc",
                sequence=3,
                status="completed",
                event_cursor="cursor_ddd",
            )
        ),
    ]


def test_sync_iter_events_reconnects_deduplicates_and_stops_at_terminal() -> None:
    responses = _reconnect_responses()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append(request)
        return responses[min(len(responses) - 1, len(requests) - 1)]

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))

    assert [message.event_id for message in messages] == [
        "event_aaa",
        "error_aaa",
        "event_ccc",
    ]
    assert len(requests) == 3
    assert requests[1].headers["last-event-id"] == "cursor_aaa"
    assert requests[2].headers["last-event-id"] == "cursor_aaa"
    client.close()


@pytest.mark.parametrize("status", ["expired", "budget_exhausted"])
def test_sync_iter_events_stops_immediately_at_lifecycle_terminal(status: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _stream_response(
            _sse_event(
                event_id="event_terminal",
                sequence=1,
                status=status,
                event_cursor="cursor_terminal",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))

    statuses: list[str] = []
    for message in messages:
        assert isinstance(message, PublicRunEvent)
        statuses.append(message.status)
    assert statuses == [status]
    assert len(requests) == 1
    client.close()


def test_sync_iter_events_keeps_cancel_requested_nonterminal() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = "cancel_requested" if len(requests) == 1 else "cancelled"
        return _stream_response(
            _sse_event(
                event_id=f"event_{len(requests)}",
                sequence=len(requests),
                status=status,
                event_cursor=f"cursor_{len(requests)}",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))

    statuses: list[str] = []
    for message in messages:
        assert isinstance(message, PublicRunEvent)
        statuses.append(message.status)
    assert statuses == ["cancel_requested", "cancelled"]
    assert len(requests) == 2
    client.close()


def test_sync_iter_events_rejects_oversized_record_before_json_decoding() -> None:
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _stream_response(b"data: " + b"x" * MAX_STREAM_EVENT_BYTES)
            )
        ),
    )

    with pytest.raises(ValueError, match="too large"):
        list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))
    client.close()


@pytest.mark.parametrize("message_type", ["run.event", "run.heartbeat", "run.error"])
def test_sync_iter_events_rejects_a_foreign_run_message(message_type: str) -> None:
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: _stream_response(
                    _sse_message_for_run(message_type, "run_aaaaaaaaaaaaaaaa")
                )
            )
        ),
    )

    with pytest.raises(ValueError, match="run_id"):
        list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))
    client.close()


def test_sync_replay_rejects_an_event_for_a_foreign_run() -> None:
    payload = {
        "events": [
            {
                "type": "run.event",
                "event_id": "event_foreign",
                "run_id": "run_aaaaaaaaaaaaaaaa",
                "sequence": 1,
                "status": "running",
                "message_key": "run.status.running",
                "event_cursor": "cursor_foreign",
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ),
    )

    with pytest.raises(ValueError, match="run_id"):
        client.runs.list_events(RUN_ID)
    client.close()


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
def test_sync_run_cursor_is_rejected_before_query_or_header_forwarding(cursor: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        client.runs.list(cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        client.runs.list_events(RUN_ID, cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        list(client.runs.iter_events(RUN_ID, last_event_id=cast("Any", cursor)))
    client.close()
    assert requests == []


@pytest.mark.parametrize("cursor", [None, 1, b"cursor_bytes", [], {}, ()])
def test_canonical_run_cursor_validator_rejects_every_non_string(cursor: object) -> None:
    with pytest.raises(ValueError, match=r"^replay_cursor is invalid$"):
        validate_run_cursor(cursor, field="replay_cursor")


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
async def test_async_run_cursor_is_rejected_before_query_or_header_forwarding(
    cursor: object,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        await client.runs.list(cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        await client.runs.list_events(RUN_ID, cursor=cast("Any", cursor))
    with pytest.raises(ValueError, match=r"^cursor is invalid$"):
        _ = [
            message
            async for message in client.runs.iter_events(
                RUN_ID,
                last_event_id=cast("Any", cursor),
            )
        ]
    await client.aclose()
    assert requests == []


@pytest.mark.asyncio
async def test_async_iter_events_reconnects_deduplicates_and_stops_at_terminal() -> None:
    responses = _reconnect_responses()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append(request)
        return responses[min(len(responses) - 1, len(requests) - 1)]

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    messages = [
        message
        async for message in client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0)
    ]

    assert [message.event_id for message in messages] == [
        "event_aaa",
        "error_aaa",
        "event_ccc",
    ]
    assert len(requests) == 3
    assert requests[1].headers["last-event-id"] == "cursor_aaa"
    assert requests[2].headers["last-event-id"] == "cursor_aaa"
    await client.aclose()


@pytest.mark.asyncio
async def test_async_iter_events_rejects_oversized_record_before_json_decoding() -> None:
    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: _stream_response(b"data: " + b"x" * MAX_STREAM_EVENT_BYTES)
            )
        ),
    )

    with pytest.raises(ValueError, match="too large"):
        _ = [
            message
            async for message in client.runs.iter_events(
                RUN_ID, initial_interval=0.0, max_interval=0.0
            )
        ]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["run.event", "run.heartbeat", "run.error"])
async def test_async_iter_events_rejects_a_foreign_run_message(message_type: str) -> None:
    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: _stream_response(
                    _sse_message_for_run(message_type, "run_aaaaaaaaaaaaaaaa")
                )
            )
        ),
    )

    with pytest.raises(ValueError, match="run_id"):
        _ = [
            message
            async for message in client.runs.iter_events(
                RUN_ID, initial_interval=0.0, max_interval=0.0
            )
        ]
    await client.aclose()


@pytest.mark.asyncio
async def test_async_replay_rejects_an_event_for_a_foreign_run() -> None:
    payload = {
        "events": [
            {
                "type": "run.event",
                "event_id": "event_foreign",
                "run_id": "run_aaaaaaaaaaaaaaaa",
                "sequence": 1,
                "status": "running",
                "message_key": "run.status.running",
                "event_cursor": "cursor_foreign",
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }
    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        ),
    )

    with pytest.raises(ValueError, match="run_id"):
        await client.runs.list_events(RUN_ID)
    await client.aclose()


def test_sync_iter_events_reconnects_after_retryable_transport_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadError("stream reset")
        return _stream_response(
            _sse_event(
                event_id="event_bbb",
                sequence=2,
                status="completed",
                event_cursor="cursor_bbb",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))

    assert [message.event_id for message in messages] == ["event_bbb"]
    assert len(requests) == 2
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_reconnects_after_retryable_transport_error() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadError("stream reset")
        return _stream_response(
            _sse_event(
                event_id="event_bbb",
                sequence=2,
                status="completed",
                event_cursor="cursor_bbb",
            )
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    messages = [
        message
        async for message in client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0)
    ]

    assert [message.event_id for message in messages] == ["event_bbb"]
    assert len(requests) == 2
    await client.aclose()


@pytest.mark.parametrize(
    ("timeout", "initial_interval", "max_interval"),
    [
        (float("nan"), 1.0, 8.0),
        (None, float("inf"), 8.0),
        (None, 2.0, 1.0),
        (None, 1.0, float("nan")),
    ],
)
def test_sync_iter_events_rejects_invalid_wait_parameters(
    timeout: float | None, initial_interval: float, max_interval: float
) -> None:
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: _stream_response(b""))
        ),
    )
    with pytest.raises(ValueError, match=r"finite|greater"):
        list(
            client.runs.iter_events(
                RUN_ID,
                timeout=timeout,
                initial_interval=initial_interval,
                max_interval=max_interval,
            )
        )
    client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout", "initial_interval", "max_interval"),
    [
        (float("nan"), 1.0, 8.0),
        (None, float("inf"), 8.0),
        (None, 2.0, 1.0),
        (None, 1.0, float("nan")),
    ],
)
async def test_async_iter_events_rejects_invalid_wait_parameters(
    timeout: float | None, initial_interval: float, max_interval: float
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _stream_response(b"")

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match=r"finite|greater"):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID,
                timeout=timeout,
                initial_interval=initial_interval,
                max_interval=max_interval,
            )
        ]
    await client.aclose()


def test_sync_iter_events_applies_bounded_reconnect_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 0.0
    sleeps: list[float] = []
    requests = 0

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(runs_module, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    monkeypatch.setattr(
        runs_module,
        "random",
        SimpleNamespace(uniform=_max_jitter),
        raising=False,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ReadError("stream reset")
        return _stream_response(
            _sse_event(
                event_id="event_jitter_sync",
                sequence=1,
                status="completed",
                event_cursor="cursor_jitter_sync",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID, initial_interval=1.0, max_interval=8.0))

    assert [message.event_id for message in messages] == ["event_jitter_sync"]
    assert requests == 2
    assert sleeps == [2.5]
    assert 0.0 < sleeps[0] <= 8.0
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_applies_bounded_reconnect_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 0.0
    sleeps: list[float] = []
    requests = 0

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(
        runs_module,
        "random",
        SimpleNamespace(uniform=_max_jitter),
        raising=False,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ReadError("stream reset")
        return _stream_response(
            _sse_event(
                event_id="event_jitter_async",
                sequence=1,
                status="completed",
                event_cursor="cursor_jitter_async",
            )
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    messages = [
        message
        async for message in client.runs.iter_events(RUN_ID, initial_interval=1.0, max_interval=8.0)
    ]

    assert [message.event_id for message in messages] == ["event_jitter_async"]
    assert requests == 2
    assert sleeps == [2.5]
    assert 0.0 < sleeps[0] <= 8.0
    await client.aclose()


def test_sync_iter_events_stops_on_caller_stop() -> None:
    requests: list[httpx.Request] = []
    stop_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            )
        )

    def stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        list(client.runs.iter_events(RUN_ID, stop=stop, initial_interval=0.0, max_interval=0.0))

    assert len(requests) == 1
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_stops_on_caller_timeout() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            )
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID, timeout=0.005, initial_interval=0.01, max_interval=0.01
            )
        ]

    assert len(requests) == 1
    await client.aclose()


def test_sync_iter_events_bounds_cursor_cycles() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureTransportError, match="bounded"):
        list(client.runs.iter_events(RUN_ID, initial_interval=0.0, max_interval=0.0))

    assert 1 < len(requests) < 10
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_bounds_cursor_cycles() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="running",
                event_cursor="cursor_aaa",
            )
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureTransportError, match="bounded"):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID, initial_interval=0.0, max_interval=0.0
            )
        ]

    assert 1 < len(requests) < 10
    await client.aclose()


def _artifact_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "artifact_ref": "art_abcdefgh",
            "content_hash": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "content_type": "application/pdf",
        },
    )


def test_sync_attach_artifact_streams_iterable_with_declared_size_and_hash() -> None:
    content = b"streamed bytes"
    chunks = iter((content[:8], content[8:]))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-length"] == str(len(content))
        assert request.read() == content
        return _artifact_response(content)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=chunks,
        byte_size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-stream-1",
    )

    assert result.artifact_ref == "art_abcdefgh"
    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_streams_async_iterable_with_declared_size_and_hash() -> None:
    content = b"async streamed bytes"

    async def chunks() -> AsyncIterator[bytes]:
        yield content[:5]
        yield content[5:]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-length"] == str(len(content))
        assert await request.aread() == content
        return _artifact_response(content)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=chunks(),
        byte_size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-stream-1",
    )

    assert result.artifact_ref == "art_abcdefgh"
    await client.aclose()


def test_sync_attach_artifact_streams_file_without_closing_caller_source() -> None:
    content = b"sync file bytes"
    source = io.BytesIO(content)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.read() == content
        return _artifact_response(content)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=source,
        byte_size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-file-1",
    )

    assert result.artifact_ref == "art_abcdefgh"
    assert not source.closed
    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_streams_file_without_closing_caller_source() -> None:
    content = b"async file bytes"
    source = io.BytesIO(content)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert await request.aread() == content
        return _artifact_response(content)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=source,
        byte_size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-file-1",
    )

    assert result.artifact_ref == "art_abcdefgh"
    assert not source.closed
    await client.aclose()


def test_sync_attach_artifact_rejects_size_overrun_and_hash_mismatch() -> None:
    content = b"bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        return _artifact_response(content)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="exceeds declared byte_size"):
        client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=iter((b"bytes", b"extra")),
            byte_size=len(content),
            content_hash=hashlib.sha256(content + b"extra").hexdigest(),
            idempotency_key="artifact-stream-6",
        )
    with pytest.raises(ValueError, match="content_hash"):
        client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=iter((content,)),
            byte_size=len(content),
            content_hash=hashlib.sha256(b"other").hexdigest(),
            idempotency_key="artifact-stream-7",
        )

    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_rejects_size_overrun_and_hash_mismatch() -> None:
    content = b"bytes"

    async def overrun_chunks() -> AsyncIterator[bytes]:
        yield content
        yield b"extra"

    async def bad_hash_chunks() -> AsyncIterator[bytes]:
        yield content

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return _artifact_response(content)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="exceeds declared byte_size"):
        await client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=overrun_chunks(),
            byte_size=len(content),
            content_hash=hashlib.sha256(content + b"extra").hexdigest(),
            idempotency_key="artifact-stream-6",
        )
    with pytest.raises(ValueError, match="content_hash"):
        await client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=bad_hash_chunks(),
            byte_size=len(content),
            content_hash=hashlib.sha256(b"other").hexdigest(),
            idempotency_key="artifact-stream-7",
        )

    await client.aclose()


def test_sync_attach_artifact_rejects_stream_without_declared_size() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _artifact_response(b"bytes")

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="byte_size"):
        client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=iter((b"bytes",)),
            content_hash=hashlib.sha256(b"bytes").hexdigest(),
            idempotency_key="artifact-stream-2",
        )

    assert not requests
    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_rejects_stream_without_declared_size() -> None:
    requests: list[httpx.Request] = []

    async def chunks() -> AsyncIterator[bytes]:
        yield b"bytes"

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _artifact_response(b"bytes")

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="byte_size"):
        await client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=chunks(),
            content_hash=hashlib.sha256(b"bytes").hexdigest(),
            idempotency_key="artifact-stream-2",
        )

    assert not requests
    await client.aclose()


def test_sync_attach_artifact_rejects_non_bytes_chunk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        return _artifact_response(b"bytes")

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="chunks must be bytes"):
        client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=cast("Any", iter((b"byte", "s"))),
            byte_size=5,
            content_hash=hashlib.sha256(b"bytes").hexdigest(),
            idempotency_key="artifact-stream-3",
        )

    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_rejects_non_bytes_chunk() -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield b"byte"
        yield "s"

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return _artifact_response(b"bytes")

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match="chunks must be bytes"):
        await client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=chunks(),
            byte_size=5,
            content_hash=hashlib.sha256(b"bytes").hexdigest(),
            idempotency_key="artifact-stream-3",
        )

    await client.aclose()


def test_sync_attach_artifact_does_not_retry_one_shot_file() -> None:
    content = b"one-shot bytes"
    source = io.BytesIO(content)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("upload connection reset")

    client = ZentureClient(
        api_key=API_KEY,
        max_retries=2,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureTransportError):
        client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=source,
            byte_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            idempotency_key="artifact-stream-4",
        )

    assert attempts == 1
    assert not source.closed
    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_does_not_retry_one_shot_file() -> None:
    content = b"one-shot async bytes"
    source = io.BytesIO(content)
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("upload connection reset")

    client = AsyncZentureClient(
        api_key=API_KEY,
        max_retries=2,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureTransportError):
        await client.runs.attach_artifact(
            upload_id="upload_abcdefgh",
            file_name="answer.pdf",
            mime_type="application/pdf",
            content=source,
            byte_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            idempotency_key="artifact-stream-4",
        )

    assert attempts == 1
    assert not source.closed
    await client.aclose()


def test_sync_attach_artifact_retries_replayable_bytes() -> None:
    content = b"replayable bytes"
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("upload connection reset")
        assert request.read() == content
        return _artifact_response(content)

    client = ZentureClient(
        api_key=API_KEY,
        max_retries=1,
        initial_retry_backoff=0.0,
        max_retry_backoff=0.0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-stream-5",
    )

    assert result.artifact_ref == "art_abcdefgh"
    assert attempts == 2
    client.close()


@pytest.mark.asyncio
async def test_async_attach_artifact_retries_replayable_bytes() -> None:
    content = b"replayable async bytes"
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("upload connection reset")
        assert await request.aread() == content
        return _artifact_response(content)

    client = AsyncZentureClient(
        api_key=API_KEY,
        max_retries=1,
        initial_retry_backoff=0.0,
        max_retry_backoff=0.0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        idempotency_key="artifact-stream-5",
    )

    assert result.artifact_ref == "art_abcdefgh"
    assert attempts == 2
    await client.aclose()


def test_sync_wait_returns_when_queued_run_reaches_completed() -> None:
    responses = [_run(), _run(status="completed")]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/runs/{RUN_ID}"
        seen.append(request)
        return httpx.Response(200, json=responses[min(len(responses) - 1, len(seen) - 1)])

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.wait(
        RUN_ID,
        timeout=0.02,
        initial_interval=0.001,
        max_interval=0.001,
    )

    assert result.status.value == "completed"
    assert len(seen) == 2
    client.close()


def test_sync_wait_uses_first_server_deadline_once_and_reports_context_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 1_000.0
    sleeps: list[float] = []
    responses = [
        _run(deadline_at="1970-01-01T00:16:06Z"),
        _run(deadline_at="1970-01-01T00:16:06Z"),
    ]
    calls = 0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(
        runs_module, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep, time=lambda: 1_000.0)
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[min(calls, len(responses) - 1)]
        calls += 1
        return httpx.Response(200, json=response)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        client.runs.wait(RUN_ID, initial_interval=2.0, max_interval=2.0)

    assert calls == 2
    assert sleeps == [2.0, 1.0]
    error = exc_info.value
    assert error.last_status == "queued"
    assert error.observed_deadline == "1970-01-01T00:16:06+00:00"
    assert "answer" not in str(error)
    client.close()


def test_sync_wait_explicit_timeout_wins_over_server_deadline() -> None:
    responses = [_run(deadline_at="2099-01-01T00:00:00Z")]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[0])

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        client.runs.wait(RUN_ID, timeout=0.001, initial_interval=0.01, max_interval=0.01)

    assert exc_info.value.observed_deadline == "2099-01-01T00:00:00+00:00"
    client.close()


def test_sync_wait_reports_deadline_when_terminal_response_crosses_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 1_000.0
    deadline_at = "1970-01-01T00:33:20Z"

    def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(
        runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: now, sleep=sleep),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal now
        now += 0.6
        return httpx.Response(200, json=_run(status="completed", deadline_at=deadline_at))

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        client.runs.wait(RUN_ID, timeout=0.5)

    assert exc_info.value.last_status == "completed"
    assert exc_info.value.observed_deadline == "1970-01-01T00:33:20+00:00"
    client.close()


def test_sync_wait_passes_remaining_budget_to_each_run_request() -> None:
    read_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_run(status="completed"))

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.wait(RUN_ID, timeout=1.0)

    assert result.status.value == "completed"
    assert len(read_timeouts) == 1
    assert 0 < read_timeouts[0] <= 1.0
    client.close()


def test_sync_wait_does_not_extend_for_a_deadline_already_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 1_000.0
    calls = 0

    def monotonic() -> float:
        return now

    def wall_time() -> float:
        return now

    def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        runs_module,
        "time",
        SimpleNamespace(monotonic=monotonic, time=wall_time, sleep=sleep),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run(deadline_at="1970-01-01T00:15:00Z"))

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        client.runs.wait(RUN_ID, initial_interval=1.0, max_interval=1.0)

    assert calls == 1
    client.close()


def test_sync_wait_honors_stop_after_an_inflight_response() -> None:
    stop_values = iter((False, True))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run(status="completed"))

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        client.runs.wait(RUN_ID, timeout=1.0, stop=lambda: next(stop_values))

    client.close()


def test_sync_wait_null_deadline_keeps_the_existing_finite_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    now = 1_000.0
    sleeps: list[float] = []
    calls = 0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(
        runs_module,
        "time",
        SimpleNamespace(monotonic=monotonic, time=lambda: 1_000.0, sleep=sleep),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run())

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        client.runs.wait(RUN_ID, initial_interval=60.0, max_interval=60.0)

    assert calls == 2
    assert sleeps == [60.0, 60.0]
    assert exc_info.value.last_status == "queued"
    assert exc_info.value.observed_deadline is None
    client.close()


def test_sync_wait_rejects_a_changed_server_deadline() -> None:
    responses = [
        _run(deadline_at="2099-01-01T00:00:00Z"),
        _run(deadline_at="2099-01-01T00:01:00Z"),
    ]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=response)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureResponseError, match="deadline_at"):
        client.runs.wait(RUN_ID, timeout=1.0, initial_interval=0.0, max_interval=0.0)

    assert calls == 2
    client.close()


@pytest.mark.asyncio
async def test_async_wait_uses_finite_default_for_null_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module

    now = 1_000.0
    sleeps: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(
        async_runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: 1_000.0),
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run())

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        await client.runs.wait(RUN_ID, initial_interval=60.0, max_interval=60.0)

    assert calls == 2
    assert sleeps == [60.0, 60.0]
    assert exc_info.value.last_status == "queued"
    assert exc_info.value.observed_deadline is None
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_honors_stop_after_an_inflight_response() -> None:
    stop_values = iter((False, True))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run(status="completed"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        await client.runs.wait(RUN_ID, timeout=1.0, stop=lambda: next(stop_values))

    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_explicit_timeout_wins_over_a_server_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module

    now = 1_000.0
    sleeps: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(
        async_runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: 1_000.0),
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run(deadline_at="1970-01-01T00:33:20Z"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        await client.runs.wait(RUN_ID, timeout=3.0, initial_interval=2.0, max_interval=2.0)

    assert calls == 2
    assert sleeps == [2.0, 1.0]
    assert exc_info.value.observed_deadline == "1970-01-01T00:33:20+00:00"
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_reports_deadline_when_terminal_response_crosses_explicit_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module

    now = 1_000.0
    deadline_at = "1970-01-01T00:33:20Z"
    monkeypatch.setattr(
        async_runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: now),
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal now
        now += 0.6
        return httpx.Response(200, json=_run(status="completed", deadline_at=deadline_at))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        await client.runs.wait(RUN_ID, timeout=0.5)

    assert exc_info.value.last_status == "completed"
    assert exc_info.value.observed_deadline == "1970-01-01T00:33:20+00:00"
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_uses_the_first_non_null_deadline_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module

    now = 1_000.0
    sleeps: list[float] = []
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(
        async_runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: 1_000.0),
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run(deadline_at="1970-01-01T00:16:21Z"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError) as exc_info:
        await client.runs.wait(RUN_ID, initial_interval=20.0, max_interval=20.0)

    assert calls == 2
    assert sleeps == [20.0, 16.0]
    assert exc_info.value.last_status == "queued"
    assert exc_info.value.observed_deadline == "1970-01-01T00:16:21+00:00"
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_past_deadline_cannot_extend_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module

    now = 1_000.0
    calls = 0

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        async_runs_module,
        "time",
        SimpleNamespace(monotonic=lambda: now, time=lambda: 1_000.0),
    )
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_run(deadline_at="1970-01-01T00:15:00Z"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        await client.runs.wait(RUN_ID, initial_interval=1.0, max_interval=1.0)

    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_rejects_a_changed_server_deadline() -> None:
    responses = [
        _run(deadline_at="2099-01-01T00:00:00Z"),
        _run(deadline_at="2099-01-01T00:01:00Z"),
    ]
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return httpx.Response(200, json=response)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZentureResponseError, match="deadline_at"):
        await client.runs.wait(RUN_ID, timeout=1.0, initial_interval=0.0, max_interval=0.0)

    assert calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_async_wait_passes_remaining_budget_to_each_run_request() -> None:
    read_timeouts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_run(status="completed"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.wait(RUN_ID, timeout=1.0)

    assert result.status.value == "completed"
    assert len(read_timeouts) == 1
    assert 0 < read_timeouts[0] <= 1.0
    await client.aclose()


@pytest.mark.parametrize("operation", ["get", "wait", "cancel", "record_outcome"])
def test_sync_run_path_rejects_a_foreign_run_projection(operation: str) -> None:
    foreign_run_id = "run_aaaaaaaaaaaaaaaa"
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json=_run(status="completed", run_id=foreign_run_id)
                )
            )
        ),
    )

    if operation == "get":
        operation_call = partial(client.runs.get, RUN_ID)
    elif operation == "wait":
        operation_call = partial(client.runs.wait, RUN_ID, timeout=1.0, initial_interval=0.0)
    elif operation == "cancel":
        operation_call = partial(client.runs.cancel, RUN_ID, idempotency_key="cancel-replay-1")
    else:
        operation_call = partial(
            client.runs.record_outcome,
            RUN_ID,
            outcome="used",
            idempotency_key="outcome-replay-1",
        )

    with pytest.raises(ZentureResponseError, match="run_id did not match"):
        operation_call()
    client.close()


@pytest.mark.asyncio
async def test_async_wait_returns_when_queued_run_reaches_completed() -> None:
    responses = [_run(), _run(status="completed")]
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/runs/{RUN_ID}"
        seen.append(request)
        return httpx.Response(200, json=responses[min(len(responses) - 1, len(seen) - 1)])

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.wait(
        RUN_ID,
        timeout=0.02,
        initial_interval=0.001,
        max_interval=0.001,
    )

    assert result.status.value == "completed"
    assert len(seen) == 2
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "wait", "cancel", "record_outcome"])
async def test_async_run_path_rejects_a_foreign_run_projection(operation: str) -> None:
    foreign_run_id = "run_aaaaaaaaaaaaaaaa"
    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json=_run(status="completed", run_id=foreign_run_id)
                )
            )
        ),
    )

    if operation == "get":
        operation_call = client.runs.get(RUN_ID)
    elif operation == "wait":
        operation_call = client.runs.wait(RUN_ID, timeout=1.0, initial_interval=0.0)
    elif operation == "cancel":
        operation_call = client.runs.cancel(RUN_ID, idempotency_key="cancel-replay-1")
    else:
        operation_call = client.runs.record_outcome(
            RUN_ID,
            outcome="used",
            idempotency_key="outcome-replay-1",
        )

    with pytest.raises(ZentureResponseError, match="run_id did not match"):
        await operation_call
    await client.aclose()


def test_sync_run_helper_preserves_idempotency_and_wait_hint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/runs/prepare"):
            return httpx.Response(200, json=_proposal())
        assert request.url.path.endswith("/runs")
        assert request.headers["idempotency-key"] == "run-1:create"
        assert request.headers["prefer"] == "wait=15"
        return httpx.Response(202, json=_run())

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.run(
        task="Review this answer.",
        artifact={"type": "text", "value": "Selected answer"},
        idempotency_key="run-1",
        wait=99,
    )

    assert result.run_id == RUN_ID
    assert seen[0].headers["idempotency-key"] == "run-1:prepare"
    client.close()


def test_run_outcome_uses_canonical_adjudications_and_edited_artifact() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_run(status="completed"))

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.runs.record_outcome(
        RUN_ID,
        outcome="edited",
        finding_adjudications=[{"finding_ref": "finding:one", "outcome": "confirmed"}],
        edited_artifact_ref="art_edited_001",
        idempotency_key="outcome-1",
    )

    assert result.status.value == "completed"
    assert seen[0].read().decode() == (
        '{"outcome":"edited","finding_adjudications":[{"finding_ref":"finding:one",'
        '"outcome":"confirmed"}],"edited_artifact_ref":"art_edited_001"}'
    )
    client.close()


def test_run_resources_reject_non_canonical_run_references_before_network() -> None:
    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: pytest.fail("invalid Run reference reached the transport")
            )
        ),
    )

    with pytest.raises(ValueError, match="canonical public Run reference"):
        client.runs.get("run:invalid")
    client.close()


@pytest.mark.asyncio
async def test_async_run_outcome_uses_canonical_adjudications_and_edited_artifact() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_run(status="completed"))

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.runs.record_outcome(
        RUN_ID,
        outcome="edited",
        finding_adjudications=[{"finding_ref": "finding:one", "outcome": "confirmed"}],
        edited_artifact_ref="art_edited_001",
        idempotency_key="outcome-1",
    )

    assert result.status.value == "completed"
    assert b'"finding_adjudications":[{"finding_ref":"finding:one"' in seen[0].read()
    await client.aclose()


def test_sync_run_sse_parser_supports_event_and_heartbeat() -> None:
    stream = b"""event: run.event
data: {\"type\":\"run.event\",\"event_id\":\"event_aaaaaaaa\",\"run_id\":\"run_33333333333343338333333333333333\",\"sequence\":1,\"status\":\"queued\",\"message_key\":\"run.status.queued\",\"event_cursor\":\"cursor_aaa\"}

event: run.heartbeat
data: {\"type\":\"run.heartbeat\",\"event_id\":\"heartbeat_aaa\",\"run_id\":\"run_33333333333343338333333333333333\",\"sequence\":1}

event: run.event
data: {\"type\":\"run.event\",\"event_id\":\"event_bbbbbbbb\",\"run_id\":\"run_33333333333343338333333333333333\",\"sequence\":2,\"status\":\"completed\",\"message_key\":\"run.status.completed\",\"event_cursor\":\"cursor_bbb\"}

"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(client.runs.iter_events(RUN_ID))

    assert isinstance(messages[0], PublicRunEvent)
    assert isinstance(messages[1], PublicRunHeartbeat)
    client.close()


def test_sync_attach_artifact_sends_bytes_with_upload_intent() -> None:
    content = b"%PDF-1.7\nbytes"
    content_hash = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/run-artifacts"
        assert request.headers["content-type"] == "application/pdf"
        assert request.headers["x-upload-id"] == "upload_abcdefgh"
        assert request.content == content
        return httpx.Response(
            201,
            json={
                "artifact_ref": "art_abcdefgh",
                "content_hash": content_hash,
                "byte_size": len(content),
                "content_type": "application/pdf",
            },
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    artifact = client.runs.attach_artifact(
        upload_id="upload_abcdefgh",
        file_name="answer.pdf",
        mime_type="application/pdf",
        content=content,
        content_hash=content_hash,
        idempotency_key="artifact-1",
    )

    assert artifact.artifact_ref == "art_abcdefgh"
    client.close()


def test_sync_signed_upload_omits_optional_upload_id() -> None:
    content_hash = hashlib.sha256(b"bytes").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/run-artifacts/signed-upload"
        assert "upload_id" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "upload_id": "upload_abcdefgh",
                "expires_at": "2026-06-15T10:00:00Z",
                "upload_url": "/v1/run-artifacts",
            },
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    upload = client.runs.signed_upload(
        file_name="answer.txt",
        mime_type="text/plain",
        byte_size=5,
        content_hash=content_hash,
        idempotency_key="signed-upload-1",
    )

    assert upload.upload_id == "upload_abcdefgh"
    client.close()


@pytest.mark.asyncio
async def test_async_signed_upload_omits_optional_upload_id() -> None:
    content_hash = hashlib.sha256(b"bytes").hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/run-artifacts/signed-upload"
        assert "upload_id" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "upload_id": "upload_abcdefgh",
                "expires_at": "2026-06-15T10:00:00Z",
                "upload_url": "/v1/run-artifacts",
            },
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    upload = await client.runs.signed_upload(
        file_name="answer.txt",
        mime_type="text/plain",
        byte_size=5,
        content_hash=content_hash,
        idempotency_key="signed-upload-1",
    )

    assert upload.upload_id == "upload_abcdefgh"
    await client.aclose()


@pytest.mark.asyncio
async def test_async_run_resources_match_sync_event_surface() -> None:
    stream = b'event: run.event\ndata: {"type":"run.event","event_id":"event_aaaaaaaa","run_id":"run_33333333333343338333333333333333","sequence":1,"status":"completed","message_key":"run.status.completed","event_cursor":"cursor_aaa"}\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/events/stream")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    messages = [message async for message in client.runs.iter_events(RUN_ID)]

    assert isinstance(messages[0], PublicRunEvent)
    assert messages[0].status == "completed"
    await client.aclose()


class _StopAwareSyncStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield _sse_heartbeat(event_id="heartbeat_aaa", sequence=1)
        raise AssertionError("idle stream was read after caller stop")

    def close(self) -> None:
        self.closed = True


class _IdleSyncStream(httpx.SyncByteStream):
    def __init__(self, read_timeout: float) -> None:
        self.read_timeout = read_timeout
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        time.sleep(self.read_timeout + 0.01)
        raise httpx.ReadTimeout("idle stream read timed out")

    def close(self) -> None:
        self.closed = True


class _HeartbeatThenTimeoutSyncStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield _sse_heartbeat(event_id="heartbeat_aaa", sequence=1)
        raise httpx.ReadTimeout("heartbeat checkpoint")

    def close(self) -> None:
        self.closed = True


class _StopAwareAsyncStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield _sse_heartbeat(event_id="heartbeat_aaa", sequence=1)
        raise AssertionError("idle stream was read after caller stop")

    async def aclose(self) -> None:
        self.closed = True


class _IdleAsyncStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.sleep(60.0)
        yield b""

    async def aclose(self) -> None:
        self.closed = True


class _ImmediateAsyncStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def aclose(self) -> None:
        return None


class _ExpiringAsyncTimeout:
    async def __aenter__(self) -> _ExpiringAsyncTimeout:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        raise TimeoutError("test read window expired")


def test_sync_iter_events_stops_before_reading_idle_stream() -> None:
    requests: list[httpx.Request] = []
    streams: list[_StopAwareSyncStream] = []
    stop_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = _StopAwareSyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    def stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        list(client.runs.iter_events(RUN_ID, stop=stop, initial_interval=0.0, max_interval=0.0))

    assert len(requests) == 1
    assert streams[0].closed
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_stops_before_reading_idle_stream() -> None:
    requests: list[httpx.Request] = []
    streams: list[_StopAwareAsyncStream] = []
    stop_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = _StopAwareAsyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    def stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID, stop=stop, initial_interval=0.0, max_interval=0.0
            )
        ]

    assert len(requests) == 1
    assert streams[0].closed
    await client.aclose()


def test_sync_iter_events_checks_timeout_after_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    clock_values = iter((0.0, 0.0, 0.0, 1.0))
    fake_time = SimpleNamespace(monotonic=lambda: next(clock_values))
    monkeypatch.setattr(runs_module, "time", fake_time)
    streams: list[_StopAwareSyncStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = _StopAwareSyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        list(client.runs.iter_events(RUN_ID, timeout=1.0))

    assert streams[0].closed
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_checks_timeout_after_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module
    import zenture._resources.runs as runs_module

    clock_values = iter((0.0, 0.0, 0.0, 1.0))
    fake_time = SimpleNamespace(monotonic=lambda: next(clock_values))
    monkeypatch.setattr(runs_module, "time", fake_time)
    monkeypatch.setattr(async_runs_module, "time", fake_time)
    streams: list[_StopAwareAsyncStream] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        stream = _StopAwareAsyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        [message async for message in client.runs.iter_events(RUN_ID, timeout=1.0)]

    assert streams[0].closed
    await client.aclose()


@pytest.mark.asyncio
async def test_async_iter_events_polls_stop_during_an_idle_stream() -> None:
    streams: list[_IdleAsyncStream] = []
    stop_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        stream = _IdleAsyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    def stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID, stop=stop, initial_interval=0.0, max_interval=0.0
            )
        ]

    assert stop_calls >= 2
    assert streams[0].closed
    await client.aclose()


@pytest.mark.asyncio
async def test_async_iter_events_rechecks_absolute_deadline_after_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module
    import zenture._resources.runs as runs_module

    clock_values = iter((0.0, 0.0, 0.0, 15.0, 15.0, 30.0))
    fake_time = SimpleNamespace(monotonic=lambda: next(clock_values, 30.0))
    monkeypatch.setattr(async_runs_module, "time", fake_time)
    monkeypatch.setattr(runs_module, "time", fake_time)
    windows: list[float | None] = []

    def timeout(seconds: float | None) -> _ExpiringAsyncTimeout:
        windows.append(seconds)
        return _ExpiringAsyncTimeout()

    monkeypatch.setattr(asyncio, "timeout", timeout)
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ImmediateAsyncStream(),
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        [message async for message in client.runs.iter_events(RUN_ID, timeout=30.0)]

    assert requests == 1
    assert windows == [15.0]
    await client.aclose()


@pytest.mark.asyncio
async def test_async_iter_events_rechecks_stop_after_explicit_deadline_idle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.async_runs as async_runs_module
    import zenture._resources.runs as runs_module

    fake_time = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(async_runs_module, "time", fake_time)
    monkeypatch.setattr(runs_module, "time", fake_time)
    windows: list[float | None] = []

    def timeout(seconds: float | None) -> _ExpiringAsyncTimeout:
        windows.append(seconds)
        return _ExpiringAsyncTimeout()

    monkeypatch.setattr(asyncio, "timeout", timeout)
    stop_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ImmediateAsyncStream(),
        )

    def stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingStoppedError):
        [
            message
            async for message in client.runs.iter_events(
                RUN_ID, timeout=30.0, stop=stop, initial_interval=0.0, max_interval=0.0
            )
        ]

    assert stop_calls == 2
    assert windows == [0.25]
    await client.aclose()


@pytest.mark.asyncio
async def test_async_iter_events_interrupts_open_idle_stream() -> None:
    streams: list[_IdleAsyncStream] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        stream = _IdleAsyncStream()
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ZenturePollingTimeoutError):
        [message async for message in client.runs.iter_events(RUN_ID, timeout=0.005)]

    assert streams[0].closed
    await client.aclose()


def test_sync_iter_events_passes_remaining_timeout_to_stream() -> None:
    read_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="completed",
                event_cursor="cursor_aaa",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    list(client.runs.iter_events(RUN_ID, timeout=5.0))

    assert read_timeouts
    assert 0 < read_timeouts[0] <= 5.0
    client.close()


def test_sync_iter_events_bounds_idle_read_and_closes_stream() -> None:
    streams: list[_IdleSyncStream] = []
    read_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read_timeout = request.extensions["timeout"]["read"]
        read_timeouts.append(read_timeout)
        stream = _IdleSyncStream(read_timeout)
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    started = time.monotonic()
    with pytest.raises(ZenturePollingTimeoutError):
        list(
            client.runs.iter_events(
                RUN_ID,
                timeout=0.03,
                initial_interval=0.0,
                max_interval=0.0,
            )
        )
    elapsed = time.monotonic() - started

    assert read_timeouts
    assert read_timeouts[0] <= 0.03
    assert elapsed < 0.2
    assert streams[0].closed
    client.close()


def test_sync_timeout_only_stream_uses_heartbeat_safe_checkpoint() -> None:
    streams: list[_HeartbeatThenTimeoutSyncStream] = []
    read_timeouts: list[float] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        read_timeouts.append(request.extensions["timeout"]["read"])
        if len(requests) == 1:
            stream = _HeartbeatThenTimeoutSyncStream()
            streams.append(stream)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        return _stream_response(
            _sse_event(
                event_id="event_bbb",
                sequence=2,
                status="completed",
                event_cursor="cursor_bbb",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(
        client.runs.iter_events(
            RUN_ID,
            timeout=30.0,
            initial_interval=0.0,
            max_interval=0.0,
        )
    )

    assert [message.event_id for message in messages] == ["heartbeat_aaa", "event_bbb"]
    assert len(requests) == 2
    assert read_timeouts[0] == 15.0
    assert streams[0].closed
    client.close()


def test_sync_timeout_only_stream_reconfigures_at_final_deadline_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zenture._resources.runs as runs_module

    clock_values = iter((0.0, 0.0, 0.0))

    def monotonic() -> float:
        return next(clock_values, 5.0)

    def sleep(_seconds: float) -> None:
        return None

    fake_time = SimpleNamespace(monotonic=monotonic, sleep=sleep)
    monkeypatch.setattr(runs_module, "time", fake_time)
    requests: list[httpx.Request] = []
    read_timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        read_timeouts.append(request.extensions["timeout"]["read"])
        if len(requests) == 1:
            return _stream_response(
                _sse_event(
                    event_id="event_aaa",
                    sequence=1,
                    status="running",
                    event_cursor="cursor_aaa",
                )
            )
        return _stream_response(
            _sse_event(
                event_id="event_bbb",
                sequence=2,
                status="completed",
                event_cursor="cursor_bbb",
            )
        )

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    messages = list(
        client.runs.iter_events(
            RUN_ID,
            timeout=20.0,
            initial_interval=0.0,
            max_interval=0.0,
        )
    )

    assert [message.event_id for message in messages] == ["event_aaa", "event_bbb"]
    assert read_timeouts == [15.0, 15.0]
    assert requests[1].headers["last-event-id"] == "cursor_aaa"
    client.close()


@pytest.mark.asyncio
async def test_async_iter_events_passes_remaining_timeout_to_stream() -> None:
    read_timeouts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return _stream_response(
            _sse_event(
                event_id="event_aaa",
                sequence=1,
                status="completed",
                event_cursor="cursor_aaa",
            )
        )

    client = AsyncZentureClient(
        api_key=API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    [message async for message in client.runs.iter_events(RUN_ID, timeout=5.0)]

    assert read_timeouts
    assert 0 < read_timeouts[0] <= 5.0
    await client.aclose()


def test_run_event_dedupe_memory_is_bounded_for_long_streams() -> None:
    state = RunEventStreamState(cursor=None, initial_interval=0.0, max_interval=0.0)

    for sequence in range(1, 2_049):
        state.accept(
            PublicRunEvent.model_validate(
                {
                    "type": "run.event",
                    "event_id": f"event_{sequence}",
                    "run_id": RUN_ID,
                    "sequence": sequence,
                    "status": "running",
                    "message_key": "run.status.running",
                    "event_cursor": f"cursor_{sequence}",
                }
            )
        )

    assert len(state.seen_event_ids) <= 1_024
