"""Bounded, non-redirecting JSON HTTP used by discovery and token requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import httpx

MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
#: Errors raised before any request byte can have reached the server.
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


@dataclass(frozen=True, slots=True)
class JsonResponse:
    status: int
    body: dict[str, object] | None


def new_client(timeout: httpx.Timeout | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout or DEFAULT_TIMEOUT,
        follow_redirects=False,
        headers={"Accept": "application/json"},
    )


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    form: dict[str, str] | None = None,
) -> JsonResponse:
    """Send one request; return status and a bounded JSON object body (or None).

    Transport errors propagate as ``httpx.HTTPError`` so callers can tell
    whether a request may already have been sent.
    """

    with client.stream(method, url, data=form) as response:
        received = bytearray()
        for chunk in response.iter_bytes():
            received.extend(chunk)
            if len(received) > MAX_BODY_BYTES:
                return JsonResponse(response.status_code, None)
        status = response.status_code
    try:
        parsed = json.loads(bytes(received))
    except ValueError:
        return JsonResponse(status, None)
    if not isinstance(parsed, dict):
        return JsonResponse(status, None)
    body = cast("dict[object, object]", parsed)
    return JsonResponse(status, {str(key): value for key, value in body.items()})
