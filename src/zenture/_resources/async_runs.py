"""Typed asynchronous public Run resources."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from typing import TYPE_CHECKING, Any, cast

from zenture._contract import (
    ArtifactUploadRequest,
    ArtifactUploadResponse,
    CreateRunRequest,
    ListRunEventsResponse,
    ListRunsResponse,
    PrepareKnowledgeRunRequest,
    PrepareKnowledgeRunResponse,
    PublicRunEvent,
    PublicRunHeartbeat,
    PublicRunResponse,
    PublicRunStreamError,
    PublicRunStreamMessage,
    RecordRunOutcomeRequest,
    SignedUploadResponse,
)
from zenture._contract.run_references import validate_run_cursor
from zenture._resources._utils import idempotency_headers, parse_response, path_segment
from zenture._resources.runs import (
    ARTIFACT_CHUNK_SIZE,
    MAX_STREAM_EVENT_BYTES,
    RunEventStreamState,
    add_wait_header,
    async_stream_timeout,
    bounded_stream_retry_delay,
    deadline_bound,
    is_retryable_stream_error,
    is_terminal_status,
    is_terminal_stream_message,
    parse_run_response,
    phase_key,
    polling_timeout,
    required_artifact_size,
    run_list_params,
    validate_run_event_replay,
    validate_run_id,
)
from zenture.errors import (
    ZentureAPIError,
    ZenturePollingStoppedError,
    ZenturePollingTimeoutError,
    ZentureResponseError,
    ZentureTransportError,
)
from zenture.polling import validate_wait_parameters

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
    from datetime import datetime
    from typing import BinaryIO

    from zenture._transport import AsyncTransport


async def _validated_async_artifact_chunks(
    source: object, *, expected_size: int, expected_hash: str
) -> AsyncIterator[bytes]:
    digest = hashlib.sha256()
    total = 0
    read = getattr(source, "read", None)
    if callable(read):
        while True:
            chunk = read(ARTIFACT_CHUNK_SIZE)
            if inspect.isawaitable(chunk):
                chunk = await chunk
            if chunk == b"":
                break
            yield_chunk = chunk
            if not isinstance(yield_chunk, bytes):
                raise ValueError("content chunks must be bytes")
            total += len(yield_chunk)
            if total > expected_size:
                raise ValueError("content exceeds declared byte_size")
            digest.update(yield_chunk)
            yield yield_chunk
    else:
        async_source = cast("AsyncIterable[object]", source)
        async for chunk in async_source:
            if not isinstance(chunk, bytes):
                raise ValueError("content chunks must be bytes")
            total += len(chunk)
            if total > expected_size:
                raise ValueError("content exceeds declared byte_size")
            digest.update(chunk)
            yield chunk

    if total != expected_size:
        raise ValueError("content length does not match declared byte_size")
    if digest.hexdigest() != expected_hash:
        raise ValueError("content_hash does not match content")


def _prepare_async_artifact_content(
    content: object,
    *,
    byte_size: int | None,
    file_name: str,
    mime_type: str,
    upload_id: str,
    content_hash: str,
) -> tuple[bytes | AsyncIterable[bytes], int, bool]:
    if isinstance(content, bytes):
        if not content:
            raise ValueError("content must be non-empty bytes")
        upload_size = len(content)
        if byte_size is not None and (type(byte_size) is not int or byte_size != upload_size):
            raise ValueError("byte_size does not match content")
        ArtifactUploadRequest(
            upload_id=upload_id,
            file_name=file_name,
            mime_type=mime_type,
            byte_size=upload_size,
            content_hash=content_hash,
        )
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise ValueError("content_hash does not match content")
        return content, upload_size, True

    if isinstance(content, bytearray | memoryview | str):
        raise ValueError("content must be bytes, an async iterable, or a binary file")
    upload_size = required_artifact_size(byte_size)
    ArtifactUploadRequest(
        upload_id=upload_id,
        file_name=file_name,
        mime_type=mime_type,
        byte_size=upload_size,
        content_hash=content_hash,
    )
    if not callable(getattr(content, "read", None)) and not hasattr(content, "__aiter__"):
        raise ValueError("content must be bytes, an async iterable, or a binary file")
    return (
        _validated_async_artifact_chunks(
            content, expected_size=upload_size, expected_hash=content_hash
        ),
        upload_size,
        False,
    )


class AsyncRunsResource:
    """Asynchronous REST and event access for canonical public Runs."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def prepare(
        self,
        *,
        task: str,
        artifact: dict[str, Any],
        idempotency_key: str,
        profile: str = "standard",
    ) -> PrepareKnowledgeRunResponse:
        body = PrepareKnowledgeRunRequest.model_validate(
            {"task": task, "artifact": artifact, "profile": profile}
        )
        payload = await self._transport.request_json(
            "POST",
            "/runs/prepare",
            headers=idempotency_headers(phase_key(idempotency_key, "prepare")),
            json=body.model_dump(mode="json", exclude_defaults=True),
        )
        return parse_response(PrepareKnowledgeRunResponse, payload)

    async def create(
        self,
        *,
        proposal_id: str,
        proposal_hash: str,
        idempotency_key: str,
        wait: int | None = None,
    ) -> PublicRunResponse:
        body = CreateRunRequest.model_validate(
            {"proposal_id": proposal_id, "proposal_hash": proposal_hash}
        )
        headers = idempotency_headers(phase_key(idempotency_key, "create"))
        add_wait_header(headers, wait)
        payload = await self._transport.request_json(
            "POST", "/runs", headers=headers, json=body.model_dump(mode="json")
        )
        return parse_response(PublicRunResponse, payload)

    async def run(
        self,
        *,
        task: str,
        artifact: dict[str, Any],
        idempotency_key: str,
        profile: str = "standard",
        wait: int | None = None,
    ) -> PublicRunResponse:
        proposal = await self.prepare(
            task=task,
            artifact=artifact,
            idempotency_key=idempotency_key,
            profile=profile,
        )
        return await self.create(
            proposal_id=str(proposal.proposal_id),
            proposal_hash=proposal.proposal_hash,
            idempotency_key=idempotency_key,
            wait=wait,
        )

    async def list(
        self,
        *,
        status: Sequence[str] | None = None,
        decision: Sequence[str] | None = None,
        profile: Sequence[str] | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> ListRunsResponse:
        params = run_list_params(
            status=status,
            decision=decision,
            profile=profile,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            cursor=cursor,
        )
        payload = await self._transport.request_json("GET", "/runs", params=params)
        return parse_response(ListRunsResponse, payload)

    async def get(
        self, run_id: str, *, view: str = "summary", _timeout: float | None = None
    ) -> PublicRunResponse:
        validate_run_id(run_id)
        if view not in {"summary", "full"}:
            raise ValueError("view must be summary or full")
        payload = await self._transport.request_json(
            "GET", f"/runs/{path_segment(run_id)}", params={"view": view}, timeout=_timeout
        )
        return parse_run_response(payload, expected_run_id=run_id)

    async def list_events(
        self, run_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> ListRunEventsResponse:
        validate_run_id(run_id)
        if type(limit) is not int or limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = validate_run_cursor(cursor)
        payload = await self._transport.request_json(
            "GET", f"/runs/{path_segment(run_id)}/events", params=params
        )
        response = parse_response(ListRunEventsResponse, payload)
        return validate_run_event_replay(response, expected_run_id=run_id)

    async def iter_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        timeout: float | None = None,
        initial_interval: float = 1.0,
        max_interval: float = 8.0,
        stop: Callable[[], bool] | None = None,
    ) -> AsyncIterator[PublicRunStreamMessage]:
        validate_run_id(run_id)
        validate_wait_parameters(
            timeout=timeout, initial_interval=initial_interval, max_interval=max_interval
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        state = RunEventStreamState(
            cursor=last_event_id,
            initial_interval=initial_interval,
            max_interval=max_interval,
        )

        while True:
            if callable(stop) and stop():
                raise ZenturePollingStoppedError(operation_id=run_id)
            if deadline is not None and time.monotonic() >= deadline:
                raise ZenturePollingTimeoutError(operation_id=run_id)
            headers = {"Accept": "text/event-stream"}
            if state.cursor is not None:
                headers["Last-Event-ID"] = state.cursor
            progressed = False
            try:
                stream_timeout = async_stream_timeout(run_id=run_id, deadline=deadline, stop=stop)
                async with asyncio.timeout(stream_timeout):
                    async with self._transport.stream(
                        "GET",
                        f"/runs/{path_segment(run_id)}/events/stream",
                        headers=headers,
                        timeout=stream_timeout,
                    ) as response:
                        async for message in _parse_sse_events_async(
                            response.aiter_lines(), expected_run_id=run_id
                        ):
                            emit, message_progressed = state.accept(message)
                            progressed = progressed or message_progressed
                            if not emit:
                                continue
                            yield message
                            if is_terminal_stream_message(message):
                                return
                            if callable(stop) and stop():
                                raise ZenturePollingStoppedError(operation_id=run_id)
                            if deadline is not None and time.monotonic() >= deadline:
                                raise ZenturePollingTimeoutError(operation_id=run_id)
                            if isinstance(message, PublicRunStreamError):
                                if message.retryable:
                                    break
                                return
            except TimeoutError as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ZenturePollingTimeoutError(operation_id=run_id) from exc
                if deadline is None and not callable(stop):
                    raise
            except (ZentureAPIError, ZentureTransportError) as exc:
                if not is_retryable_stream_error(exc):
                    raise

            if callable(stop) and stop():
                raise ZenturePollingStoppedError(operation_id=run_id)
            if deadline is not None and time.monotonic() >= deadline:
                raise ZenturePollingTimeoutError(operation_id=run_id)
            state.finish_stream(progressed=progressed)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            delay = bounded_stream_retry_delay(
                interval=state.reconnect_interval,
                max_interval=max(state.max_interval, 0.01),
                remaining=remaining,
            )
            if delay > 0:
                await asyncio.sleep(delay)

    async def wait(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        initial_interval: float = 1.0,
        max_interval: float = 8.0,
        stop: Any = None,
    ) -> PublicRunResponse:
        validate_run_id(run_id)
        validate_wait_parameters(
            timeout=timeout, initial_interval=initial_interval, max_interval=max_interval
        )
        deadline = time.monotonic() + (timeout if timeout is not None else 120.0)
        interval = initial_interval
        observed_deadline: datetime | None = None
        last_status = None
        while True:
            if callable(stop) and stop():
                raise ZenturePollingStoppedError(operation_id=run_id)
            if time.monotonic() >= deadline:
                raise polling_timeout(
                    run_id=run_id,
                    last_status=last_status,
                    observed_deadline=observed_deadline,
                )
            result = await self.get(run_id, _timeout=max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise polling_timeout(
                    run_id=run_id,
                    last_status=result.status,
                    observed_deadline=observed_deadline,
                )
            if callable(stop) and stop():
                raise ZenturePollingStoppedError(operation_id=run_id)
            last_status = result.status
            if (
                observed_deadline is not None
                and result.deadline_at is not None
                and result.deadline_at != observed_deadline
            ):
                raise ZentureResponseError(
                    "Public API response deadline_at changed during Run polling."
                )
            if observed_deadline is None and result.deadline_at is not None:
                observed_deadline = result.deadline_at
                if timeout is None:
                    deadline = deadline_bound(
                        observed_deadline,
                        now_monotonic=time.monotonic(),
                        now_wall=time.time(),
                        max_interval=max_interval,
                    )
            if is_terminal_status(result.status):
                return result
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 2, max_interval)

    async def cancel(self, run_id: str, *, idempotency_key: str) -> PublicRunResponse:
        validate_run_id(run_id)
        payload = await self._transport.request_json(
            "POST",
            f"/runs/{path_segment(run_id)}/cancel",
            headers=idempotency_headers(idempotency_key),
            json={},
        )
        return parse_run_response(payload, expected_run_id=run_id)

    async def record_outcome(
        self,
        run_id: str,
        *,
        outcome: str,
        idempotency_key: str,
        finding_adjudications: Sequence[dict[str, Any]] | None = None,
        edited_artifact_ref: str | None = None,
    ) -> PublicRunResponse:
        validate_run_id(run_id)
        body = RecordRunOutcomeRequest.model_validate(
            {
                "outcome": outcome,
                "finding_adjudications": list(finding_adjudications or ()),
                "edited_artifact_ref": edited_artifact_ref,
            }
        )
        payload = await self._transport.request_json(
            "POST",
            f"/runs/{path_segment(run_id)}/outcome",
            headers=idempotency_headers(idempotency_key),
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return parse_run_response(payload, expected_run_id=run_id)

    async def signed_upload(
        self,
        *,
        file_name: str,
        mime_type: str,
        byte_size: int,
        content_hash: str,
        idempotency_key: str,
    ) -> SignedUploadResponse:
        body = ArtifactUploadRequest(
            file_name=file_name,
            mime_type=mime_type,
            byte_size=byte_size,
            content_hash=content_hash,
        )
        payload = await self._transport.request_json(
            "POST",
            "/run-artifacts/signed-upload",
            headers=idempotency_headers(idempotency_key),
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return parse_response(SignedUploadResponse, payload)

    async def attach_artifact(
        self,
        *,
        upload_id: str,
        file_name: str,
        mime_type: str,
        content: bytes | AsyncIterable[bytes] | BinaryIO,
        byte_size: int | None = None,
        content_hash: str,
        idempotency_key: str,
    ) -> ArtifactUploadResponse:
        upload_content, upload_size, replayable = _prepare_async_artifact_content(
            content,
            byte_size=byte_size,
            file_name=file_name,
            mime_type=mime_type,
            upload_id=upload_id,
            content_hash=content_hash,
        )
        headers = idempotency_headers(idempotency_key)
        headers.update(
            {
                "Content-Type": mime_type,
                "X-Upload-ID": upload_id,
                "Content-Length": str(upload_size),
            }
        )
        payload = await self._transport.request_json(
            "POST",
            "/run-artifacts",
            headers=headers,
            content=upload_content,
            replayable=replayable,
        )
        return parse_response(ArtifactUploadResponse, payload)


async def _parse_sse_events_async(
    lines: AsyncIterator[str],
    *,
    expected_run_id: str,
) -> AsyncIterator[PublicRunStreamMessage]:
    data: list[str] = []
    event_type = ""
    event_bytes = 0
    async for raw_line in lines:
        line = str(raw_line)
        event_bytes += len(line.encode("utf-8")) + 1
        if event_bytes > MAX_STREAM_EVENT_BYTES:
            raise ValueError("public Run event stream record is too large")
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except (TypeError, ValueError):
                    raise ValueError("public Run event stream contained invalid JSON") from None
                yield _parse_stream_message(payload, event_type, expected_run_id=expected_run_id)
                data.clear()
                event_type = ""
            event_bytes = 0
            continue
        if line.startswith(":") or line.startswith("id:"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip())


def _parse_stream_message(
    payload: object, event_type: str, *, expected_run_id: str
) -> PublicRunStreamMessage:
    if not isinstance(payload, dict):
        raise ValueError("public Run event payload must be an object")
    mapping = cast("dict[str, object]", payload)
    message_type = mapping.get("type") or event_type
    if message_type == "run.event":
        message: PublicRunStreamMessage = PublicRunEvent.model_validate(mapping)
    elif message_type == "run.heartbeat":
        message = PublicRunHeartbeat.model_validate(mapping)
    elif message_type == "run.error":
        message = PublicRunStreamError.model_validate(mapping)
    else:
        raise ValueError("public Run event type is unsupported")
    if message.run_id != expected_run_id:
        raise ValueError("public Run event run_id did not match the requested Run")
    return message


__all__ = ["AsyncRunsResource"]
