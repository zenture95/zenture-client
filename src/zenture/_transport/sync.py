"""Synchronous HTTPX transport wrapper."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import httpx

from zenture._transport.base import (
    MAX_RESPONSE_BYTES,
    build_timeout,
    build_url,
    decode_bounded_json,
    decode_bounded_text,
    default_headers,
    extract_error_code,
    has_idempotency_key,
    retry_delay,
)
from zenture.errors import (
    ZentureAPIError,
    ZentureResponseError,
    ZentureTransportError,
    error_from_response,
)
from zenture.redaction import redact_text
from zenture.retries import should_retry

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from zenture.config import ZentureConfig


class SyncTransport:
    """Owns or wraps a synchronous HTTPX client."""

    def __init__(
        self,
        *,
        config: ZentureConfig,
        client: httpx.Client | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.api_base_url,
            timeout=build_timeout(config),
        )
        self._config = config
        self._user_agent = user_agent

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    def _request_headers(self, *, auth: bool = True) -> dict[str, str]:
        return default_headers(self._config, self._user_agent, auth=auth)

    def request_text(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        params: dict[str, str] | None = None,
    ) -> str:
        """Request a text response and map public API errors."""

        response = self._request(
            method,
            path,
            auth=auth,
            headers=headers,
            json=json,
            params=params,
        )
        try:
            return decode_bounded_text(
                response.content,
                encoding=response.encoding or "utf-8",
                label="Public API response",
            )
        except ValueError:
            raise ZentureResponseError("Public API response was too large.") from None

    def request_json(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        params: dict[str, str] | None = None,
        content: bytes | Iterable[bytes] | None = None,
        replayable: bool = True,
        timeout: float | None = None,
    ) -> object:
        """Request a JSON response and map public API errors."""

        response = self._request(
            method,
            path,
            auth=auth,
            headers=headers,
            json=json,
            params=params,
            content=content,
            replayable=replayable,
            timeout=timeout,
        )
        response_error: ZentureResponseError | None = None
        try:
            return decode_bounded_json(response.content, label="Public API response")
        except ValueError as exc:
            response_error = ZentureResponseError(
                "Public API response was too large."
                if "too large" in str(exc)
                else "Public API response was not valid JSON."
            )
        raise response_error

    @contextmanager
    def stream(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Generator[httpx.Response, None, None]:
        """Open one non-buffered public stream without mutation retries."""

        request_headers = self._request_headers(auth=auth)
        if headers is not None:
            request_headers.update(headers)
        try:
            if timeout is None:
                stream = self._client.stream(
                    method,
                    build_url(self._config, path),
                    headers=request_headers,
                    params=params,
                )
            else:
                stream = self._client.stream(
                    method,
                    build_url(self._config, path),
                    headers=request_headers,
                    params=params,
                    timeout=timeout,
                )
            with stream as response:
                if response.status_code >= 400:
                    _raise_stream_error(response)
                yield response
        except (ZentureResponseError, ZentureAPIError):
            raise
        except httpx.HTTPError as exc:
            raise ZentureTransportError(redact_text(str(exc))) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool,
        headers: dict[str, str] | None,
        json: object | None,
        params: dict[str, str] | None,
        content: bytes | Iterable[bytes] | None = None,
        replayable: bool = True,
        timeout: float | None = None,
    ) -> httpx.Response:
        request_headers = self._request_headers(auth=auth)
        if headers is not None:
            request_headers.update(headers)

        attempt = 1
        max_attempts = self._config.max_retries + 1
        request_deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            response: httpx.Response | None = None
            transport_error: ZentureTransportError | None = None
            try:
                request_timeout = (
                    None
                    if request_deadline is None
                    else max(0.0, request_deadline - time.monotonic())
                )
                if request_timeout is not None and request_timeout <= 0:
                    raise httpx.ReadTimeout("request deadline exceeded")
                if content is None:
                    request_kwargs: dict[str, Any] = {
                        "headers": request_headers,
                        "params": params,
                        "json": json,
                    }
                else:
                    request_kwargs = {
                        "headers": request_headers,
                        "params": params,
                        "content": content,
                    }
                if request_timeout is not None:
                    request_kwargs["timeout"] = request_timeout
                response = self._client.request(
                    method,
                    build_url(self._config, path),
                    **request_kwargs,
                )
            except httpx.HTTPError as exc:
                decision = should_retry(
                    method=method,
                    status_code=503,
                    error_code="dependency_unavailable",
                    has_idempotency_key=has_idempotency_key(request_headers) and replayable,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if decision.retry:
                    delay = retry_delay(
                        retry_after=None,
                        attempt=attempt,
                        initial_backoff=self._config.initial_retry_backoff,
                        max_backoff=self._config.max_retry_backoff,
                    )
                    if request_deadline is not None:
                        delay = min(delay, max(0.0, request_deadline - time.monotonic()))
                    if delay > 0:
                        time.sleep(delay)
                    if request_deadline is not None and time.monotonic() >= request_deadline:
                        raise ZentureTransportError("request deadline exceeded") from None
                    attempt += 1
                    continue
                transport_error = ZentureTransportError(redact_text(str(exc)))
            if transport_error is not None:
                raise transport_error
            assert response is not None

            if response.status_code < 400:
                return response

            payload: object | None = None
            response_error: ZentureResponseError | None = None
            try:
                payload = decode_bounded_json(response.content, label="Public API error response")
            except ValueError as exc:
                response_error = ZentureResponseError(
                    "Public API error response was too large."
                    if "too large" in str(exc)
                    else "Public API error response was not valid JSON."
                )
            if response_error is not None:
                raise response_error
            assert payload is not None
            if not isinstance(payload, dict):
                raise ZentureResponseError("Public API error response was not an object.")
            payload_dict = cast("dict[str, object]", payload)

            decision = should_retry(
                method=method,
                status_code=response.status_code,
                error_code=extract_error_code(payload_dict),
                has_idempotency_key=has_idempotency_key(request_headers) and replayable,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            if decision.retry:
                delay = retry_delay(
                    retry_after=response.headers.get("Retry-After"),
                    attempt=attempt,
                    initial_backoff=self._config.initial_retry_backoff,
                    max_backoff=self._config.max_retry_backoff,
                )
                if request_deadline is not None:
                    delay = min(delay, max(0.0, request_deadline - time.monotonic()))
                if delay > 0:
                    time.sleep(delay)
                if request_deadline is not None and time.monotonic() >= request_deadline:
                    raise ZentureTransportError("request deadline exceeded") from None
                attempt += 1
                continue

            raise error_from_response(
                status_code=response.status_code,
                payload=payload_dict,
                headers=response.headers,
            )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SyncTransport:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _raise_stream_error(response: httpx.Response) -> None:
    try:
        body = bytearray()
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError("too large")
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("too large")
        payload = decode_bounded_json(bytes(body), label="Public API error response")
    except ValueError as exc:
        message = (
            "Public API error response was too large."
            if "too large" in str(exc)
            else "Public API error response was not valid JSON."
        )
        raise ZentureResponseError(message) from None
    if not isinstance(payload, dict):
        raise ZentureResponseError("Public API error response was not an object.")
    payload_dict = cast("dict[str, object]", payload)
    raise error_from_response(
        status_code=response.status_code,
        payload=payload_dict,
        headers=response.headers,
    )
