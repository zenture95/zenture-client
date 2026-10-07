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
        """Prepare a proposal for reviewing selected content; this does not start execution.

        Args:
            task: Original user task and explicit evaluation criteria; non-blank,
                at most 20,000 Unicode code points. Do not invent requirements.
            artifact: Selected content as {"type": "text", "value": "..."} (1-50,000
                code points). A bare URL is not selected text. The schema retains
                zenture_ref, but Run preparation currently rejects registered-file
                inputs; registration does not enable file evaluation.
            profile: "standard" by default; "fast" and "detailed" require explicit
                user intent. Actual capabilities and costs remain service-defined.
            idempotency_key: Caller-owned key saved before dispatch. Reuse the same
                key and identical request after an uncertain outcome; never use a new
                key as a retry fallback. Keep credentials and personal data out of it.
                The SDK derives a :prepare key; reserve space for this suffix within
                the REST 255-character limit.

        Returns PrepareKnowledgeRunResponse. Inspect proposal_id, proposal_hash,
        expires_at, start_admissible, planned_checks, unavailable_checks and
        billing_projection. Preserve the
        returned ID/hash together for create(); a proposal is not a completed Run.
        Local invalid input raises ValueError/ValidationError; API failures raise
        ZentureAPIError subclasses. See docs/run-guide.md for the REST workflow.

        Async variant: await this call.
        """

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
        predecessor_run_id: str | None = None,
    ) -> PublicRunResponse:
        """Start a previously prepared proposal and return its current Run state.

        proposal_id and proposal_hash must be copied together from prepare(); do
        not compute a hash. idempotency_key is a saved caller key (the SDK appends
        :create). wait is an optional integer HTTP wait preference, clamped to
        0-15 seconds; it does not guarantee completion. predecessor_run_id is an
        optional owned run_... reference to an earlier Run; None means no predecessor.
        It is sent only at start. Preserve it when replaying the same key; changing
        it returns idempotency_conflict (409). An unavailable predecessor returns
        predecessor_run_invalid (422).

        Returns PublicRunResponse with run_id/status. Starting may consume Credits
        or a Guest Trial slot. Use get()/wait() for progress and get(view="full")
        for available results. Invalid local inputs raise ValueError/ValidationError;
        server rejections raise ZentureAPIError subclasses. Recover uncertainty with
        the same proposal and key, never a replacement operation.

        Async variant: await this call.
        """

        body = CreateRunRequest.model_validate(
            {
                "proposal_id": proposal_id,
                "proposal_hash": proposal_hash,
                "predecessor_run_id": predecessor_run_id,
            }
        )
        headers = idempotency_headers(phase_key(idempotency_key, "create"))
        add_wait_header(headers, wait)
        payload = await self._transport.request_json(
            "POST", "/runs", headers=headers, json=body.model_dump(mode="json", exclude_none=True)
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
        predecessor_run_id: str | None = None,
    ) -> PublicRunResponse:
        """Prepare and start one selected evaluation; does not poll to completion.

        Args:
            task: Original user task and explicit evaluation criteria; non-blank,
                at most 20,000 Unicode code points. Do not invent requirements.
            artifact: Selected content as {"type": "text", "value": "..."} (1-50,000
                code points). A bare URL is not selected text. The schema retains
                zenture_ref, but Run preparation currently rejects registered-file
                inputs; registration does not enable file evaluation.
            profile: "standard" by default; "fast" and "detailed" require explicit
                user intent. Actual capabilities and costs remain service-defined.
            idempotency_key: Caller-owned key saved before dispatch. Reuse the same
                key and identical request after an uncertain outcome; never use a new
                key as a retry fallback. Keep credentials and personal data out of it.
                The SDK derives :prepare and :create keys; do not add them yourself.
            wait: Optional integer wait preference, clamped to 0-15 seconds.
            predecessor_run_id: Optional owned earlier run_... reference, forwarded
                only to create. None means none. Preserve it on recovery with the
                same key; a changed predecessor returns idempotency_conflict (409).
                An unavailable predecessor returns predecessor_run_invalid (422).

        Returns PublicRunResponse, possibly still queued/running. Save run_id, use
        wait() or get() for progress, then get(view="full") for the result. Unlike
        chat.run/evaluations.run, this method does not wait until terminal status.
        Starting can consume Credits or a Trial slot. Local invalid inputs raise
        ValueError/ValidationError; API/transport failures use the typed SDK errors.
        See docs/run-guide.md for uncertain starts and decision interpretation.

        Async variant: await this call.
        """

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
            predecessor_run_id=predecessor_run_id,
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
        """Return one compact page of owned Runs, newest first; never auto-page.

        Args:
            status: Categories active/completed/failed/cancelled; succeeded is a
                deprecated completed alias. None or [] applies no status filter.
            decision: ready/revise/human_review/insufficient_evidence; None or []
                applies no decision filter.
            profile: fast/standard/detailed; None or [] applies no profile filter.
            created_after, created_before: Timezone-aware datetime objects, or
                None for open bounds; after must not be later than before.
            limit: 1-50, default 5.
            cursor: Previous next_cursor copied unchanged, or None for first page.

        Returns ListRunsResponse with runs and next_cursor. Use get(view="full") for details.
        Preserve filters when following a cursor. Clarify ambiguous matches before
        starting/cancelling a Run. Invalid local bounds/cursors raise ValueError;
        API errors raise ZentureAPIError subclasses.

        Async variant: await this call.
        """

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
        """Read one owned Run and return PublicRunResponse directly.

        run_id is the identifier from run/create/list. view="summary" is the default
        for progress; view="full" requests available evaluation content. Read
        safe_result_content.status before using its content; unavailable is not an
        empty successful result. Invalid ID/view raises ValueError; malformed or
        foreign-ID responses raise ZentureResponseError; API errors are typed.
        _timeout is reserved for the SDK polling budget; ordinary callers omit it.
        This read never starts, retries or cancels a Run.

        Async variant: await this call.
        """

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
        """Return one bounded event replay page, without streaming or auto-pagination.

        run_id identifies the owned Run. cursor is an opaque previous next_cursor;
        omit for first page. limit is 1-50, default 50. Returns ListRunEventsResponse
        with events, has_more and next_cursor. Pass returned cursors unchanged.
        Invalid ID/limit/cursor or foreign event IDs raise ValueError; malformed
        responses and API errors use the SDK response/API exceptions.

        Async variant: await this call.
        """

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
        """Stream typed Run events, heartbeats and stream errors, reconnecting as needed.

        run_id identifies the owned Run. last_event_id is a saved event_cursor,
        not event_id or a list position. timeout bounds listening in seconds; None
        has no explicit overall deadline. initial_interval/max_interval control
        reconnect backoff (defaults 1/8 seconds). stop is a synchronous predicate
        that stops local listening, never the Run itself.

        Yields PublicRunStreamMessage variants; inspect each variant before reading
        its fields. Terminal events end the iterator. Stop/timeout raise
        ZenturePollingStoppedError/ZenturePollingTimeoutError; malformed streams
        raise ZentureResponseError. See docs/run-guide.md for replay versus streaming.

        Async variant: consume with async for; use a synchronous stop predicate.
        """

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
        """Poll an existing Run until terminal; return its summary PublicRunResponse.

        run_id comes from run/create/list. timeout is an optional positive seconds
        budget. None starts with 120 seconds and uses the first observed server
        Run deadline plus recovery allowance when supplied; waiting remains finite.
        initial_interval/max_interval set exponential polling delay (defaults 1/8
        seconds). stop is a synchronous predicate to stop waiting without cancellation.

        Returns terminal states including failed/cancelled/expired/budget_exhausted,
        not just completed. Fetch get(view="full") for content and inspect
        acceptance_decision separately. Local timeout/stop raise
        ZenturePollingTimeoutError/ZenturePollingStoppedError with the Run ID in
        operation_id; retain it and read the same Run later. Never start a replacement.

        Async variant: await this call.
        """

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
                    observed_deadline=observed_deadline or result.deadline_at,
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
        """Request cancellation of one owned active Run after an explicit user stop.

        run_id is the known Run ID; idempotency_key is a saved REST key for this
        cancellation request, reused unchanged on retry. Returns PublicRunResponse.
        cancel_requested is pending; get()/wait() observes the actual final state.
        Do not infer refund/zero charge. Local invalid IDs raise ValueError;
        service rejection raises ZentureAPIError subclasses.

        Async variant: await this call.
        """

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
        """Record explicit user feedback for one completed owned Run.

        Args:
            run_id: Existing Run ID from run/create/list/get.
            idempotency_key: Saved REST key for this feedback request; reuse on retry.
            outcome: used (as-is), edited (after changes), rejected (not used),
                escalated (further review), or not_sure (undecided).
            finding_adjudications: Up to 20 {"finding_ref": "...", "outcome": "..."}
                objects. Copy finding_ref from this Run's full result; each outcome
                is confirmed/rejected/partially_valid/not_sure. None/[] skips them.
            edited_artifact_ref: Registered owned art_... reference, required exactly
                for edited; otherwise omit or use None. It is not an inline diff.

        Returns the updated PublicRunResponse, preserving the original acceptance
        verdict. Does not re-evaluate edited content. ValidationError rejects invalid
        combinations; API/transport failures use the typed SDK errors. Do not infer
        feedback from silence or comments.

        Async variant: await this call.
        """

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
        """Issue temporary upload authorization for one selected file; no Run starts.

        file_name (1-255 chars), mime_type (1-127 chars), byte_size (1 byte to 10 MiB) and
        content_hash (64 lowercase SHA-256 hex chars) must describe the actual bytes.
        idempotency_key is a saved REST key for this authorization request.

        Returns SignedUploadResponse: preserve upload_id and expires_at, then send
        bytes with attach_artifact before expiry. upload_url may be absent; the SDK
        attach_artifact method uses the public artifact route. Do not treat upload_id
        or upload_url as an artifact_ref. Invalid inputs raise ValidationError;
        API errors raise ZentureAPIError subclasses.

        Async variant: await this call.
        """

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
        """Upload selected bytes using authorization from signed_upload; no Run starts.

        Args:
            upload_id: Copy from signed_upload, before its expires_at.
            file_name, mime_type: Match the selected file's authorized metadata.
            content: Nonempty bytes, an async iterable of bytes, or a binary file.
            byte_size: May be omitted for bytes; required exact size for streams.
                Maximum is 10 MiB. Streams are not automatically replayable.
            content_hash: SHA-256 of the complete bytes as 64 lowercase hex chars.
            idempotency_key: Saved REST key for this upload, distinct from issuance.

        Returns ArtifactUploadResponse with the registered artifact_ref. Registration
        does not enable evaluation: Run preparation currently rejects zenture_ref.
        The SDK validates
        byte count/hash; mismatch raises ValueError (stream validation occurs while
        sending). Other local validation/API failures use typed SDK errors.
        Unlike MCP attach_artifact, this method receives and sends real bytes.

        Async variant: await this call.
        """

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
