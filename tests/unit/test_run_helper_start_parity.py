"""Prepare-to-Start parity tests for the public Run convenience helpers."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest

from zenture import AsyncZentureClient, ZentureClient
from zenture.errors import ZentureAPIError

# Deterministic synthetic fixture for MockTransport-only tests; no real credentials/network.
API_KEY = f"zt_live_{UUID(int=0).hex}"
RUN_ID = "run_33333333333343338333333333333333"
PROPOSAL_ID = "33333333-3333-4333-8333-333333333333"
PROPOSAL_HASH = "b" * 64


def _proposal() -> dict[str, object]:
    return {
        "proposal_id": PROPOSAL_ID,
        "proposal_version": 1,
        "expires_at": "2026-09-30T12:30:00Z",
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
        "start_admissible": False,
        "proposal_hash": PROPOSAL_HASH,
    }


def _run() -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "generation": 1,
        "family": "knowledge",
        "work_type": "answer",
        "profile": "standard",
        "status": "queued",
        "created_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z",
        "queue": {"queue_reason": "queue:admitted", "jobs_ahead": 0},
        "cancellation_requested": False,
    }


def _proposal_error() -> dict[str, object]:
    return {
        "error": {
            "code": "proposal_expired",
            "message": "The Prepare proposal is no longer valid.",
        },
        "request_id": "req_00000000000000000000000000000000",
    }


def _assert_start_request(request: httpx.Request) -> None:
    assert request.method == "POST"
    assert request.url.path == "/v1/runs"
    assert json.loads(request.content) == {
        "proposal_id": PROPOSAL_ID,
        "proposal_hash": PROPOSAL_HASH,
    }
    assert request.headers["idempotency-key"] == "run-parity:create"
    assert request.headers["prefer"] == "wait=15"


def test_sync_run_forwards_inadmissible_prepare_to_start() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/runs/prepare":
            assert request.headers["idempotency-key"] == "run-parity:prepare"
            return httpx.Response(200, json=_proposal())
        _assert_start_request(request)
        return httpx.Response(202, json=_run())

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )

    try:
        result = client.runs.run(
            task="Review this answer.",
            artifact={"type": "text", "value": "Selected answer"},
            idempotency_key="run-parity",
            wait=15,
        )
    finally:
        client.close()

    assert result.run_id == RUN_ID
    assert [request.url.path for request in seen] == ["/v1/runs/prepare", "/v1/runs"]


def test_sync_run_forwards_server_rejection_as_typed_api_error() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/runs/prepare":
            assert request.headers["idempotency-key"] == "run-parity:prepare"
            return httpx.Response(200, json=_proposal())
        _assert_start_request(request)
        return httpx.Response(409, json=_proposal_error())

    client = ZentureClient(
        api_key=API_KEY,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )

    try:
        with pytest.raises(ZentureAPIError) as exc_info:
            client.runs.run(
                task="Review this answer.",
                artifact={"type": "text", "value": "Selected answer"},
                idempotency_key="run-parity",
                wait=15,
            )
    finally:
        client.close()

    assert type(exc_info.value) is ZentureAPIError
    assert exc_info.value.error_code == "proposal_expired"
    assert exc_info.value.status_code == 409
    assert [request.url.path for request in seen] == ["/v1/runs/prepare", "/v1/runs"]


def test_async_run_forwards_inadmissible_prepare_to_start() -> None:
    async def exercise() -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/runs/prepare":
                assert request.headers["idempotency-key"] == "run-parity:prepare"
                return httpx.Response(200, json=_proposal())
            _assert_start_request(request)
            return httpx.Response(202, json=_run())

        client = AsyncZentureClient(
            api_key=API_KEY,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )

        try:
            result = await client.runs.run(
                task="Review this answer.",
                artifact={"type": "text", "value": "Selected answer"},
                idempotency_key="run-parity",
                wait=15,
            )
        finally:
            await client.aclose()

        assert result.run_id == RUN_ID
        assert [request.url.path for request in seen] == ["/v1/runs/prepare", "/v1/runs"]

    asyncio.run(exercise())


def test_async_run_forwards_server_rejection_as_typed_api_error() -> None:
    async def exercise() -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/runs/prepare":
                assert request.headers["idempotency-key"] == "run-parity:prepare"
                return httpx.Response(200, json=_proposal())
            _assert_start_request(request)
            return httpx.Response(409, json=_proposal_error())

        client = AsyncZentureClient(
            api_key=API_KEY,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )

        try:
            with pytest.raises(ZentureAPIError) as exc_info:
                await client.runs.run(
                    task="Review this answer.",
                    artifact={"type": "text", "value": "Selected answer"},
                    idempotency_key="run-parity",
                    wait=15,
                )
        finally:
            await client.aclose()

        assert type(exc_info.value) is ZentureAPIError
        assert exc_info.value.error_code == "proposal_expired"
        assert exc_info.value.status_code == 409
        assert [request.url.path for request in seen] == ["/v1/runs/prepare", "/v1/runs"]

    asyncio.run(exercise())
