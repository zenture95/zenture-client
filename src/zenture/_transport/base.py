"""Shared HTTP transport helpers."""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from time import time
from typing import TYPE_CHECKING, cast

import httpx

from zenture._version import __version__

if TYPE_CHECKING:
    from zenture.config import ZentureConfig

DEFAULT_USER_AGENT = f"zenture-python/{__version__}"
MAX_RESPONSE_BYTES = 512 * 1024


def bounded_response_body(body: bytes, *, label: str) -> bytes:
    """Return a response body only when it is within the public byte ceiling."""

    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError(f"{label} was too large")
    return body


def decode_bounded_json(body: bytes, *, label: str) -> object:
    """Decode JSON only after enforcing the public response byte ceiling."""

    return json.loads(bounded_response_body(body, label=label))


def decode_bounded_text(body: bytes, *, encoding: str, label: str) -> str:
    """Decode text only after enforcing the public response byte ceiling."""

    return bounded_response_body(body, label=label).decode(encoding, errors="replace")


def default_headers(
    config: ZentureConfig,
    user_agent: str | None = None,
    *,
    auth: bool = True,
) -> dict[str, str]:
    """Build default API request headers."""

    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    if auth:
        headers["Authorization"] = f"Bearer {config.api_key_value}"
    return headers


def build_url(config: ZentureConfig, path: str) -> str:
    """Build an absolute API URL for owned or injected HTTPX clients."""

    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{config.api_base_url}{normalized_path}"


def build_timeout(config: ZentureConfig) -> httpx.Timeout:
    """Build the explicit HTTP timeout policy for SDK-owned clients."""

    return httpx.Timeout(
        connect=config.connect_timeout,
        read=config.read_timeout,
        write=config.write_timeout,
        pool=config.pool_timeout,
    )


def has_idempotency_key(headers: dict[str, str]) -> bool:
    """Return whether request headers contain a non-empty Idempotency-Key."""

    return any(key.lower() == "idempotency-key" and bool(value) for key, value in headers.items())


def extract_error_code(payload: object) -> str | None:
    """Extract a public API error code from a parsed error response payload."""

    if not isinstance(payload, dict):
        return None
    payload_dict = cast("dict[str, object]", payload)
    error = payload_dict.get("error")
    if not isinstance(error, dict):
        return None
    error_dict = cast("dict[str, object]", error)
    code = error_dict.get("code")
    if not isinstance(code, str):
        return None
    return code


def retry_delay(
    *,
    retry_after: str | None,
    attempt: int,
    initial_backoff: float,
    max_backoff: float,
) -> float:
    """Return retry delay using Retry-After when present, otherwise exponential backoff."""

    if retry_after is not None:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return min(parsed, max_backoff)

    if initial_backoff <= 0 or max_backoff <= 0:
        return 0.0
    exponent = attempt - 1
    if exponent < 0:
        exponent = 0
    delay = initial_backoff * (2.0**exponent)
    if delay > max_backoff:
        return max_backoff
    return delay


def _parse_retry_after(value: str) -> float | None:
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    retry_at_timestamp = retry_at.timestamp()
    return max(0.0, retry_at_timestamp - time())


def request_error_details(headers: dict[str, str], path: str) -> None:
    """Opt scoped REST requests into safe details, preserving explicit overrides."""
    normalized = path if path.startswith("/") else "/" + path
    if not any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in ("/runs", "/run-artifacts")
    ):
        return
    if not any(key.lower() == "x-zenture-error-details" for key in headers):
        headers["X-Zenture-Error-Details"] = "issues"
