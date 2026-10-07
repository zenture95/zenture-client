"""Filepath: tests/unit/test_error_details_negotiation.py
Purpose: Verify REST error-detail negotiation at actual HTTP request boundaries.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from zenture._transport import AsyncTransport, SyncTransport
from zenture.config import ZentureConfig
from zenture.errors import ZentureValidationError

# Deterministic synthetic fixture for MockTransport-only tests; no real credentials/network.
KEY = f"zt_live_{UUID(int=0).hex}"
PATHS = [
    "/runs",
    "/runs/prepare",
    "/runs/run_abc",
    "/runs/run_abc/outcome",
    "/runs/run_abc/cancel",
    "/runs/run_abc/events",
    "/run-artifacts",
    "/run-artifacts/signed-upload",
]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("path", PATHS)
def test_run_artifact_requests_opt_in_without_changing_dispatch(
    asynchronous: bool, path: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "unchanged"})

    config = ZentureConfig(api_key=KEY)
    headers = {"Idempotency-Key": "synthetic_saved_key", "X-Custom": "caller"}

    async def invoke() -> object:
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await AsyncTransport(config=config, client=client).request_json(
                    "POST", path, headers=headers, json={}
                )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return SyncTransport(config=config, client=client).request_json(
                "POST", path, headers=headers, json={}
            )

    assert asyncio.run(invoke()) == {"status": "unchanged"}
    assert len(seen) == 1
    assert seen[0].headers["X-Zenture-Error-Details"] == "issues"
    assert seen[0].headers["Idempotency-Key"] == "synthetic_saved_key"
    assert seen[0].headers["X-Custom"] == "caller"
    assert headers == {"Idempotency-Key": "synthetic_saved_key", "X-Custom": "caller"}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "path", ["/oauth/token", "/account", "/helloworld", "/runs-other", "/run-artifacts-other"]
)
def test_unrelated_requests_do_not_negotiate(asynchronous: bool, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def invoke() -> None:
        config = ZentureConfig(api_key=KEY)
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await AsyncTransport(config=config, client=client).request_json(
                    "POST", path, auth=False
                )
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                SyncTransport(config=config, client=client).request_json("POST", path, auth=False)

    asyncio.run(invoke())
    assert len(seen) == 1
    assert "X-Zenture-Error-Details" not in seen[0].headers
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "details",
    [
        None,
        [{"path": "/task", "category": "required"}],
        [{"path": "/PRIVATE_SENTINEL", "category": "required"}],
    ],
)
def test_requested_errors_preserve_old_valid_and_malformed_details(
    asynchronous: bool, details: object
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        error: dict[str, object] = {"code": "validation_failed", "message": "Validation failed"}
        if details is not None:
            error["issues"] = details
        return httpx.Response(422, json={"error": error})

    async def invoke() -> None:
        config = ZentureConfig(api_key=KEY)
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await AsyncTransport(config=config, client=client).request_json(
                    "POST", "/runs/prepare", json={}
                )
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                SyncTransport(config=config, client=client).request_json(
                    "POST", "/runs/prepare", json={}
                )

    with pytest.raises(ZentureValidationError) as caught:
        asyncio.run(invoke())
    assert caught.value.status_code == 422
    assert caught.value.error_code == "validation_failed"
    assert len(caught.value.issues) == (1 if details and "PRIVATE" not in str(details) else 0)
    assert "PRIVATE" not in str(caught.value) + repr(caught.value)
    assert len(seen) == 1
    assert seen[0].headers["X-Zenture-Error-Details"] == "issues"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stream_request_negotiates_and_preserves_explicit_header_override(
    asynchronous: bool,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="", headers={"Content-Type": "text/event-stream"})

    async def invoke() -> None:
        config = ZentureConfig(api_key=KEY)
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                transport = AsyncTransport(config=config, client=client)
                async with transport.stream(
                    "GET", "/runs/run_abc/events/stream", headers={"Last-Event-ID": "saved"}
                ):
                    pass
                await transport.request_text(
                    "GET", "/runs", headers={"x-zenture-error-details": "caller_override"}
                )
        else:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                sync_transport = SyncTransport(config=config, client=client)
                with sync_transport.stream(
                    "GET", "/runs/run_abc/events/stream", headers={"Last-Event-ID": "saved"}
                ):
                    pass
                sync_transport.request_text(
                    "GET", "/runs", headers={"x-zenture-error-details": "caller_override"}
                )

    asyncio.run(invoke())
    assert seen[0].headers["X-Zenture-Error-Details"] == "issues"
    assert seen[0].headers["Last-Event-ID"] == "saved"
    assert seen[1].headers.get_list("X-Zenture-Error-Details") == ["caller_override"]
