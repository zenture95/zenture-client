"""Typed synchronous public Run resources."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import httpx

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
    RunStatus,
    SignedUploadResponse,
)
from zenture._contract.run_references import validate_run_cursor
from zenture._resources._utils import (
    idempotency_headers,
    parse_response,
    path_segment,
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
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime
    from typing import BinaryIO

    from zenture._transport import SyncTransport

_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.EXPIRED,
        RunStatus.SUCCEEDED,
        RunStatus.BUDGET_EXHAUSTED,
    }
)
_TERMINAL_STREAM_STATUSES = frozenset({"completed", "failed", "cancelled"})
_RETRYABLE_STREAM_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_STREAM_ERROR_CODES = frozenset(
    {"capacity_unavailable", "dependency_unavailable", "internal_error", "rate_limited"}
)
_MAX_STREAM_CURSOR_CYCLES = 3
_MAX_SEEN_EVENT_IDS = 1_024
_SYNC_STREAM_STOP_POLL_INTERVAL = 0.25
_SYNC_STREAM_HEARTBEAT_INTERVAL = 15.0
_ASYNC_STREAM_STOP_POLL_INTERVAL = 0.25
_ASYNC_STREAM_HEARTBEAT_INTERVAL = 15.0
_MIN_STREAM_CHECKPOINT_BACKOFF = 0.01
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_STREAM_EVENT_BYTES = 512 * 1024
ARTIFACT_CHUNK_SIZE = 64 * 1024
_RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]{3,128}$")
_DEFAULT_WAIT_TIMEOUT = 120.0
_RUN_RECOVERY_BOUND_SECONDS = 35.0
_STREAM_RECONNECT_JITTER_RATIO = 0.25


def is_terminal_status(status: RunStatus) -> bool:
    """Return whether a public Run status is terminal."""

    return status in _TERMINAL_STATUSES


def validate_run_id(run_id: str) -> str:
    if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must be a canonical public Run reference")
    return run_id


def parse_run_response(payload: object, *, expected_run_id: str) -> PublicRunResponse:
    """Validate a Run projection and bind it to the requested path identity."""

    response = parse_response(PublicRunResponse, payload)
    if response.run_id != expected_run_id:
        raise ZentureResponseError("Public API response run_id did not match the requested Run.")
    return response


def _deadline_bound(
    deadline_at: object, *, now_monotonic: float, now_wall: float, max_interval: float
) -> float:
    timestamp = cast("datetime", deadline_at).timestamp()
    return now_monotonic + timestamp - now_wall + _RUN_RECOVERY_BOUND_SECONDS + max_interval


def _polling_timeout(
    *, run_id: str, last_status: RunStatus | None, observed_deadline: object | None
) -> ZenturePollingTimeoutError:
    status = last_status.value if last_status is not None else None
    deadline = (
        observed_deadline.isoformat()
        if observed_deadline is not None and hasattr(observed_deadline, "isoformat")
        else None
    )
    details: list[str] = []
    if status is not None:
        details.append(f"last_status={status}")
    if deadline is not None:
        details.append(f"observed_deadline={deadline}")
    message = "Run polling timed out before terminal status."
    if details:
        message = f"{message} ({', '.join(details)})"
    return ZenturePollingTimeoutError(
        message,
        operation_id=run_id,
        last_status=status,
        observed_deadline=deadline,
    )


@dataclass
class RunEventStreamState:
    """Shared sync/async state for one reconnecting Run event stream."""

    cursor: str | None
    initial_interval: float
    max_interval: float
    seen_event_ids: OrderedDict[str, None] = field(default_factory=lambda: OrderedDict[str, None]())
    last_event_sequence: int | None = None
    no_progress_cycles: int = 0
    reconnect_interval: float = field(init=False)

    def __post_init__(self) -> None:
        if self.cursor is not None:
            self.cursor = validate_run_cursor(self.cursor)
        self.reconnect_interval = max(0.0, self.initial_interval)

    def accept(self, message: PublicRunStreamMessage) -> tuple[bool, bool]:
        """Record a message and return ``(emit, cursor_progressed)``."""

        if isinstance(message, PublicRunEvent):
            if message.event_id in self.seen_event_ids or (
                self.last_event_sequence is not None
                and message.sequence <= self.last_event_sequence
            ):
                return False, False
            previous_cursor = self.cursor
            self._remember_event_id(message.event_id)
            self.last_event_sequence = message.sequence
            self.cursor = message.event_cursor
            return True, self.cursor != previous_cursor

        if message.event_id in self.seen_event_ids:
            return False, False
        self._remember_event_id(message.event_id)
        return True, False

    def _remember_event_id(self, event_id: str) -> None:
        self.seen_event_ids[event_id] = None
        self.seen_event_ids.move_to_end(event_id)
        if len(self.seen_event_ids) > _MAX_SEEN_EVENT_IDS:
            self.seen_event_ids.popitem(last=False)

    def finish_stream(self, *, progressed: bool, checkpoint: bool = False) -> None:
        """Update reconnect backoff and fail closed on a cursor cycle."""

        if checkpoint:
            self.no_progress_cycles = 0
            self.reconnect_interval = min(
                max(
                    self.reconnect_interval * 2.0,
                    self.initial_interval,
                    _MIN_STREAM_CHECKPOINT_BACKOFF,
                ),
                max(self.max_interval, _MIN_STREAM_CHECKPOINT_BACKOFF),
            )
            return

        if progressed:
            self.no_progress_cycles = 0
            self.reconnect_interval = max(0.0, self.initial_interval)
            return

        self.no_progress_cycles += 1
        if self.no_progress_cycles >= _MAX_STREAM_CURSOR_CYCLES:
            raise ZentureTransportError(
                "public Run event stream made no progress after bounded reconnects"
            )
        self.reconnect_interval = min(
            max(self.reconnect_interval * 2.0, self.initial_interval),
            max(0.0, self.max_interval),
        )


def is_terminal_stream_message(message: PublicRunStreamMessage) -> bool:
    """Return whether a stream message proves the Run stream is terminal."""

    if isinstance(message, PublicRunEvent):
        return message.status in _TERMINAL_STREAM_STATUSES
    return isinstance(message, PublicRunStreamError) and message.terminal


def is_retryable_stream_error(error: Exception) -> bool:
    """Return whether a stream-open/read error may be retried safely."""

    if isinstance(error, ZentureTransportError):
        return True
    if isinstance(error, ZentureAPIError):
        return (
            error.status_code in _RETRYABLE_STREAM_STATUS_CODES
            or error.error_code in _RETRYABLE_STREAM_ERROR_CODES
        )
    return False


def bounded_stream_retry_delay(
    *, interval: float, max_interval: float, remaining: float | None = None
) -> float:
    """Add bounded reconnect jitter without exceeding the active wait budget."""

    if interval <= 0.0:
        return 0.0
    jitter_limit = min(
        interval * _STREAM_RECONNECT_JITTER_RATIO,
        max(0.0, max_interval - interval),
    )
    delay = interval + random.uniform(0.0, jitter_limit)
    if remaining is not None:
        delay = min(delay, max(0.0, remaining))
    return delay


def _raise_if_stream_stopped(
    *,
    run_id: str,
    deadline: float | None,
    stop: Callable[[], bool] | None,
) -> None:
    if callable(stop) and stop():
        raise ZenturePollingStoppedError(operation_id=run_id)
    if deadline is not None and time.monotonic() >= deadline:
        raise ZenturePollingTimeoutError(operation_id=run_id)


def remaining_stream_timeout(*, run_id: str, deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ZenturePollingTimeoutError(operation_id=run_id)
    return remaining


def _sync_stream_timeout(
    *, run_id: str, deadline: float | None, stop: Callable[[], bool] | None
) -> float | None:
    """Bound blocking sync reads while local stream control is requested."""

    if deadline is None:
        return _SYNC_STREAM_STOP_POLL_INTERVAL if callable(stop) else None
    remaining = remaining_stream_timeout(run_id=run_id, deadline=deadline)
    assert remaining is not None
    if not callable(stop):
        return min(remaining, _SYNC_STREAM_HEARTBEAT_INTERVAL)
    return min(remaining, _SYNC_STREAM_STOP_POLL_INTERVAL)


def _async_stream_timeout(
    *, run_id: str, deadline: float | None, stop: Callable[[], bool] | None
) -> float | None:
    """Bound async reads while a local stop callback needs polling."""

    if deadline is None:
        return _ASYNC_STREAM_STOP_POLL_INTERVAL if callable(stop) else None
    remaining = remaining_stream_timeout(run_id=run_id, deadline=deadline)
    assert remaining is not None
    if not callable(stop):
        return min(remaining, _ASYNC_STREAM_HEARTBEAT_INTERVAL)
    return min(remaining, _ASYNC_STREAM_STOP_POLL_INTERVAL)


def _sync_stream_needs_final_window_reconfigure(
    *, deadline: float | None, stop: Callable[[], bool] | None
) -> bool:
    if deadline is None or callable(stop):
        return False
    return deadline - time.monotonic() <= _SYNC_STREAM_HEARTBEAT_INTERVAL


def _is_stream_read_timeout(error: Exception) -> bool:
    """Return whether transport wrapped an HTTPX sync read timeout."""

    return isinstance(error, ZentureTransportError) and isinstance(
        error.__cause__, httpx.ReadTimeout
    )


def required_artifact_size(byte_size: int | None) -> int:
    if type(byte_size) is not int:
        raise ValueError("byte_size is required for streaming content")
    if byte_size < 1 or byte_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("byte_size must be between 1 and 10485760")
    return byte_size


def _validated_sync_artifact_chunks(
    source: object, *, expected_size: int, expected_hash: str
) -> Iterator[bytes]:
    digest = hashlib.sha256()
    total = 0
    read = getattr(source, "read", None)
    if callable(read):
        chunks = iter(lambda: read(ARTIFACT_CHUNK_SIZE), b"")
    else:
        try:
            chunks = iter(cast("Iterable[object]", source))
        except TypeError as exc:
            raise ValueError("content must be bytes, an iterable, or a binary file") from exc

    for chunk in chunks:
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


def _prepare_sync_artifact_content(
    content: object,
    *,
    byte_size: int | None,
    file_name: str,
    mime_type: str,
    upload_id: str,
    content_hash: str,
) -> tuple[bytes | Iterable[bytes], int, bool]:
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
        raise ValueError("content must be bytes, an iterable, or a binary file")
    upload_size = required_artifact_size(byte_size)
    ArtifactUploadRequest(
        upload_id=upload_id,
        file_name=file_name,
        mime_type=mime_type,
        byte_size=upload_size,
        content_hash=content_hash,
    )
    if not callable(getattr(content, "read", None)) and not isinstance(content, Iterable):
        raise ValueError("content must be bytes, an iterable, or a binary file")
    return (
        _validated_sync_artifact_chunks(
            cast("Iterable[bytes] | BinaryIO", content),
            expected_size=upload_size,
            expected_hash=content_hash,
        ),
        upload_size,
        False,
    )


class RunsResource:
    """Synchronous REST and event access for canonical public Runs."""

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def prepare(
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
        payload = self._transport.request_json(
            "POST",
            "/runs/prepare",
            headers=idempotency_headers(phase_key(idempotency_key, "prepare")),
            json=body.model_dump(mode="json", exclude_defaults=True),
        )
        return parse_response(PrepareKnowledgeRunResponse, payload)

    def create(
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
        payload = self._transport.request_json(
            "POST",
            "/runs",
            headers=headers,
            json=body.model_dump(mode="json"),
        )
        return parse_response(PublicRunResponse, payload)

    def run(
        self,
        *,
        task: str,
        artifact: dict[str, Any],
        idempotency_key: str,
        profile: str = "standard",
        wait: int | None = None,
    ) -> PublicRunResponse:
        proposal = self.prepare(
            task=task,
            artifact=artifact,
            idempotency_key=idempotency_key,
            profile=profile,
        )
        return self.create(
            proposal_id=str(proposal.proposal_id),
            proposal_hash=proposal.proposal_hash,
            idempotency_key=idempotency_key,
            wait=wait,
        )

    def list(
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
        return parse_response(
            ListRunsResponse,
            self._transport.request_json("GET", "/runs", params=params),
        )

    def get(
        self, run_id: str, *, view: str = "summary", _timeout: float | None = None
    ) -> PublicRunResponse:
        validate_run_id(run_id)
        if view not in {"summary", "full"}:
            raise ValueError("view must be summary or full")
        payload = self._transport.request_json(
            "GET",
            f"/runs/{path_segment(run_id)}",
            params={"view": view},
            timeout=_timeout,
        )
        return parse_run_response(payload, expected_run_id=run_id)

    def list_events(
        self, run_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> ListRunEventsResponse:
        validate_run_id(run_id)
        if type(limit) is not int or limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = validate_run_cursor(cursor)
        response = parse_response(
            ListRunEventsResponse,
            self._transport.request_json(
                "GET", f"/runs/{path_segment(run_id)}/events", params=params
            ),
        )
        return validate_run_event_replay(response, expected_run_id=run_id)

    def iter_events(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        timeout: float | None = None,
        initial_interval: float = 1.0,
        max_interval: float = 8.0,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[PublicRunStreamMessage]:
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
        final_window_reconfigured = False

        while True:
            _raise_if_stream_stopped(run_id=run_id, deadline=deadline, stop=stop)
            headers = {"Accept": "text/event-stream"}
            if state.cursor is not None:
                headers["Last-Event-ID"] = state.cursor
            progressed = False
            checkpoint = False
            try:
                stream_timeout = _sync_stream_timeout(run_id=run_id, deadline=deadline, stop=stop)
                with self._transport.stream(
                    "GET",
                    f"/runs/{path_segment(run_id)}/events/stream",
                    headers=headers,
                    timeout=stream_timeout,
                ) as response:
                    for message in _parse_sse_events(response.iter_lines(), expected_run_id=run_id):
                        emit, message_progressed = state.accept(message)
                        progressed = progressed or message_progressed
                        if not emit:
                            continue
                        yield message
                        if is_terminal_stream_message(message):
                            return
                        _raise_if_stream_stopped(run_id=run_id, deadline=deadline, stop=stop)
                        if (
                            not final_window_reconfigured
                            and _sync_stream_needs_final_window_reconfigure(
                                deadline=deadline, stop=stop
                            )
                        ):
                            final_window_reconfigured = True
                            checkpoint = True
                            break
                        if isinstance(message, PublicRunStreamError):
                            if message.retryable:
                                break
                            return
            except (ZentureAPIError, ZentureTransportError) as exc:
                if not is_retryable_stream_error(exc):
                    raise
                checkpoint = _is_stream_read_timeout(exc) and (
                    deadline is not None or callable(stop)
                )

            _raise_if_stream_stopped(run_id=run_id, deadline=deadline, stop=stop)
            state.finish_stream(progressed=progressed, checkpoint=checkpoint)
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            delay = bounded_stream_retry_delay(
                interval=state.reconnect_interval,
                max_interval=max(state.max_interval, _MIN_STREAM_CHECKPOINT_BACKOFF),
                remaining=remaining,
            )
            if delay > 0:
                time.sleep(delay)

    def wait(
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
        deadline = time.monotonic() + (timeout if timeout is not None else _DEFAULT_WAIT_TIMEOUT)
        interval = initial_interval
        observed_deadline: object | None = None
        last_status: RunStatus | None = None
        while True:
            if callable(stop) and stop():
                raise ZenturePollingStoppedError(operation_id=run_id)
            if time.monotonic() >= deadline:
                raise _polling_timeout(
                    run_id=run_id,
                    last_status=last_status,
                    observed_deadline=observed_deadline,
                )
            result = self.get(run_id, _timeout=max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                raise _polling_timeout(
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
                    deadline = _deadline_bound(
                        observed_deadline,
                        now_monotonic=time.monotonic(),
                        now_wall=time.time(),
                        max_interval=max_interval,
                    )
            if is_terminal_status(result.status):
                return result
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 2, max_interval)

    def cancel(self, run_id: str, *, idempotency_key: str) -> PublicRunResponse:
        validate_run_id(run_id)
        payload = self._transport.request_json(
            "POST",
            f"/runs/{path_segment(run_id)}/cancel",
            headers=idempotency_headers(idempotency_key),
            json={},
        )
        return parse_run_response(payload, expected_run_id=run_id)

    def record_outcome(
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
        payload = self._transport.request_json(
            "POST",
            f"/runs/{path_segment(run_id)}/outcome",
            headers=idempotency_headers(idempotency_key),
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return parse_run_response(payload, expected_run_id=run_id)

    def signed_upload(
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
        payload = self._transport.request_json(
            "POST",
            "/run-artifacts/signed-upload",
            headers=idempotency_headers(idempotency_key),
            json=body.model_dump(mode="json", exclude_none=True),
        )
        return parse_response(SignedUploadResponse, payload)

    def attach_artifact(
        self,
        *,
        upload_id: str,
        file_name: str,
        mime_type: str,
        content: bytes | Iterable[bytes] | BinaryIO,
        byte_size: int | None = None,
        content_hash: str,
        idempotency_key: str,
    ) -> ArtifactUploadResponse:
        upload_content, upload_size, replayable = _prepare_sync_artifact_content(
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
        payload = self._transport.request_json(
            "POST",
            "/run-artifacts",
            headers=headers,
            content=upload_content,
            replayable=replayable,
        )
        return parse_response(ArtifactUploadResponse, payload)


def phase_key(value: str, suffix: str) -> str:
    if not value.strip():
        raise ValueError("idempotency_key must not be blank")
    result = f"{value.strip()}:{suffix}"
    if len(result) > 255:
        raise ValueError("idempotency_key is too long")
    return result


def add_wait_header(headers: dict[str, str], value: int | None) -> None:
    if value is None:
        return
    if type(value) is not int:
        raise ValueError("wait must be an integer")
    headers["Prefer"] = f"wait={min(max(value, 0), 15)}"


def run_list_params(
    *,
    status: Sequence[str] | None,
    decision: Sequence[str] | None,
    profile: Sequence[str] | None,
    created_after: datetime | None,
    created_before: datetime | None,
    limit: int,
    cursor: str | None,
) -> dict[str, str]:
    if type(limit) is not int or limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    params = {"limit": str(limit)}
    for name, values in (("status", status), ("decision", decision), ("profile", profile)):
        if values:
            if any(not value for value in values):
                raise ValueError(f"{name} contains an invalid value")
            params[name] = ",".join(values)
    if created_after is not None:
        params["created_after"] = _timestamp(created_after)
    if created_before is not None:
        params["created_before"] = _timestamp(created_before)
    if cursor is not None:
        params["cursor"] = validate_run_cursor(cursor)
    return params


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def validate_run_event_replay(
    response: ListRunEventsResponse, *, expected_run_id: str
) -> ListRunEventsResponse:
    """Bind every replay event to the Run selected by the request path."""

    if any(event.run_id != expected_run_id for event in response.events):
        raise ValueError("public Run event run_id did not match the requested Run")
    return response


def _parse_sse_events(
    lines: Iterator[str], *, expected_run_id: str
) -> Iterator[PublicRunStreamMessage]:
    data: list[str] = []
    event_type = ""
    event_bytes = 0
    for raw_line in lines:
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


__all__ = ["RunsResource"]
