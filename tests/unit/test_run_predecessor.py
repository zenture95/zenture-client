"""Optional predecessor_run_id on the public Run create and run helpers."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest

from zenture import AsyncZentureClient, ZentureClient
from zenture._contract import PublicErrorCode
from zenture.errors import ZentureAPIError

# Deterministic synthetic fixture for MockTransport-only tests; no real credentials/network.
API_KEY = f"zt_live_{UUID(int=0).hex}"
RUN_ID = "run_33333333333343338333333333333333"
PREDECESSOR = "run_55555555555545558555555555555555"
PROPOSAL_ID = "33333333-3333-4333-8333-333333333333"
PROPOSAL_HASH = "b" * 64


def _run() -> dict[str, object]:
    return {
        "run_id": RUN_ID, "generation": 1, "family": "knowledge", "work_type": "answer",
        "profile": "standard", "status": "queued", "created_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z", "queue": {"queue_reason": "queue:admitted", "jobs_ahead": 0},
        "cancellation_requested": False,
    }


def _proposal() -> dict[str, object]:
    return {
        "proposal_id": PROPOSAL_ID, "proposal_version": 1, "expires_at": "2026-08-30T12:30:00Z",
        "inferred_work_type": "answer",
        "task_contract_summary": {"work_type": "answer", "summary_ref": "snapshot:task-summary-v1",
                                  "requirement_count": 1},
        "planned_checks": [], "unavailable_checks": [], "expected_duration_seconds": 30,
        "estimated_credits": "12", "maximum_credits": "24", "start_admissible": True,
        "proposal_hash": PROPOSAL_HASH,
    }


def _sync_client(bodies: list[dict[str, object]], *, status: int = 202,
                 payload: dict[str, object] | None = None) -> ZentureClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/prepare":
            return httpx.Response(200, json=_proposal())
        bodies.append(json.loads(request.content))
        return httpx.Response(status, json=payload if payload is not None else _run())

    return ZentureClient(
        api_key=API_KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)


def test_create_sends_the_predecessor_only_when_given() -> None:
    bodies: list[dict[str, object]] = []
    client = _sync_client(bodies)
    try:
        client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k")
        client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k",
                           predecessor_run_id=PREDECESSOR)
    finally:
        client.close()
    assert bodies == [
        {"proposal_id": PROPOSAL_ID, "proposal_hash": PROPOSAL_HASH},
        {"proposal_id": PROPOSAL_ID, "proposal_hash": PROPOSAL_HASH, "predecessor_run_id": PREDECESSOR},
    ]


def test_run_helper_forwards_the_predecessor_to_create() -> None:
    bodies: list[dict[str, object]] = []
    client = _sync_client(bodies)
    try:
        client.runs.run(task="Review.", artifact={"type": "text", "value": "x"}, idempotency_key="k",
                        predecessor_run_id=PREDECESSOR)
    finally:
        client.close()
    assert bodies == [
        {"proposal_id": PROPOSAL_ID, "proposal_hash": PROPOSAL_HASH, "predecessor_run_id": PREDECESSOR}]


@pytest.mark.parametrize("bad", ["", "x", "art_abcdef", "run_", "run_a", PREDECESSOR + "!", "run:" + "a" * 32])
def test_malformed_predecessor_fails_locally_before_any_request(bad: str) -> None:
    bodies: list[dict[str, object]] = []
    client = _sync_client(bodies)
    try:
        with pytest.raises(ValueError):
            client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k",
                               predecessor_run_id=bad)
    finally:
        client.close()
    assert bodies == []


def test_closed_predecessor_error_is_a_typed_public_error_code() -> None:
    bodies: list[dict[str, object]] = []
    error: dict[str, object]
    error = {"error": {"code": "predecessor_run_invalid", "message": "The predecessor run is not available."},
             "request_id": "req_00000000000000000000000000000000"}
    client = _sync_client(bodies, status=422, payload=error)
    try:
        with pytest.raises(ZentureAPIError) as caught:
            client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k",
                               predecessor_run_id=PREDECESSOR)
    finally:
        client.close()
    assert caught.value.error_code == "predecessor_run_invalid"
    assert PublicErrorCode("predecessor_run_invalid") is PublicErrorCode.PREDECESSOR_RUN_INVALID


def test_async_create_and_run_send_the_predecessor() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/prepare":
            return httpx.Response(200, json=_proposal())
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json=_run())

    async def scenario() -> None:
        client = AsyncZentureClient(
            api_key=API_KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            max_retries=0)
        try:
            await client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH,
                                     idempotency_key="k", predecessor_run_id=PREDECESSOR)
            await client.runs.run(task="Review.", artifact={"type": "text", "value": "x"},
                                  idempotency_key="k", predecessor_run_id=PREDECESSOR)
            await client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k")
        finally:
            await client.aclose()

    asyncio.run(scenario())
    linked = {"proposal_id": PROPOSAL_ID, "proposal_hash": PROPOSAL_HASH, "predecessor_run_id": PREDECESSOR}
    assert bodies == [linked, linked, {"proposal_id": PROPOSAL_ID, "proposal_hash": PROPOSAL_HASH}]


@pytest.mark.parametrize("path_failure", ["prepare", "create"])
def test_missing_legal_consent_is_a_typed_non_retried_403(path_failure: str) -> None:
    calls: list[str] = []
    envelope = {"error": {"code": "legal_consent_required", "message": "Accept the current Terms and Privacy Policy."},
                "request_id": "req_00000000000000000000000000000000"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(403, json=envelope)

    client = ZentureClient(
        api_key=API_KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=2)
    try:
        with pytest.raises(ZentureAPIError) as caught:
            if path_failure == "create":
                client.runs.create(proposal_id=PROPOSAL_ID, proposal_hash=PROPOSAL_HASH, idempotency_key="k")
            else:
                client.runs.prepare(task="Review this answer.", artifact={"type": "text", "value": "answer"})
    finally:
        client.close()
    assert caught.value.error_code == "legal_consent_required"
    assert caught.value.status_code == 403
    assert PublicErrorCode("legal_consent_required") is PublicErrorCode.LEGAL_CONSENT_REQUIRED
    assert len(calls) == 1
