"""Peer-level MCP Run clients backed by the canonical SDK contracts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Generator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from zenture._contract import (
    ArtifactUploadResponse,
    ListRunEventsResponse,
    ListRunsResponse,
    PrepareKnowledgeRunRequest,
    PublicRunResponse,
    RunArtifact,
    RunProfile,
)
from zenture._mcp.contracts import (
    McpArtifactRequest,
    McpEndpoint,
    McpFindingAdjudication,
    McpGetRunRequest,
    McpListRunsRequest,
    McpOutcomeRequest,
    McpRunRead,
    McpRunResponse,
    validate_run_idempotency_key,
)
from zenture._mcp.transport import (
    AsyncBearerTokenProvider,
    AsyncMcpTransport,
    SyncMcpTransport,
    open_streamable_http_transport,
)
from zenture.errors import ZentureMCPError, ZentureMCPProtocolError

if TYPE_CHECKING:
    from zenture._auth.session import AuthSession

_OMITTED_RUN_KEY = object()

_MAX_ARGUMENT_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 256 * 1024
_REQUIRED_PRODUCT_TOOL_NAMES = frozenset(
    {"run", "list_runs", "get_run", "cancel_run", "record_run_outcome"}
)
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "prompt",
        "raw_prompt",
        "ciphertext",
        "provider_payload",
        "chain_of_thought",
        "user_id",
        "tenant_id",
        "api_token",
        "oauth_token",
    }
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast("Mapping[str, object]", value)


def _bounded_json(value: object, *, label: str, limit: int) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ZentureMCPProtocolError("invalid_json") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ZentureMCPProtocolError(f"{label}_too_large")


def _validate_safe_payload(value: object) -> dict[str, object]:
    mapping = _mapping(value)
    if mapping is None:
        raise ZentureMCPProtocolError("invalid_result")
    _bounded_json(mapping, label="result", limit=_MAX_RESULT_BYTES)

    def walk(node: object) -> None:
        node_mapping = _mapping(node)
        if node_mapping is not None:
            if any(type(key) is not str for key in node_mapping):
                raise ZentureMCPProtocolError("invalid_result_key")
            if any(key.lower() in _FORBIDDEN_KEYS for key in node_mapping):
                raise ZentureMCPProtocolError("forbidden_result_field")
            for child in node_mapping.values():
                walk(child)
        elif isinstance(node, list | tuple):
            for child in cast("Iterable[object]", node):
                walk(child)

    walk(mapping)
    return dict(mapping)


def _structured_content(result: object) -> object:
    result_mapping = _mapping(result)
    if result_mapping is not None:
        for key in ("structured_content", "structuredContent"):
            if key in result_mapping:
                value = result_mapping[key]
                if value is not None:
                    return value
        content = result_mapping.get("content")
    else:
        for name in ("structured_content", "structuredContent"):
            value = getattr(result, name, None)
            if value is not None:
                return value
        content = getattr(result, "content", None)
    if not isinstance(content, (list, tuple)) or len(cast("Sequence[object]", content)) != 1:
        return result_mapping if result_mapping is not None else None
    block = cast("Sequence[object]", content)[0]
    block_mapping = _mapping(block)
    block_type = (
        block_mapping.get("type") if block_mapping is not None else getattr(block, "type", None)
    )
    text = block_mapping.get("text") if block_mapping is not None else getattr(block, "text", None)
    if block_type != "text" or not isinstance(text, str):
        return result_mapping if result_mapping is not None else None
    if len(text.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ZentureMCPProtocolError("result_too_large")
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ZentureMCPProtocolError("invalid_result") from exc


def _is_error_result(result: object) -> bool:
    result_mapping = _mapping(result)
    if result_mapping is not None:
        return result_mapping.get("is_error") is True or result_mapping.get("isError") is True
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))


def _error_from_payload(payload: Mapping[str, object]) -> ZentureMCPError:
    raw = _mapping(payload.get("error"))
    if raw is None:
        return ZentureMCPProtocolError("invalid_error")
    code = raw.get("code")
    status_code = raw.get("http_status")
    retryable = raw.get("retryable")
    next_action = raw.get("next_action")
    request_id = raw.get("request_id")
    retry_after = raw.get("retry_after_seconds")
    if not isinstance(code, str) or _TOOL_NAME.fullmatch(code) is None:
        return ZentureMCPProtocolError("invalid_error_code")
    if type(status_code) is not int or not 400 <= status_code <= 599:
        status_code = 502
    if type(retryable) is not bool:
        retryable = False
    if not isinstance(next_action, str) or len(next_action) > 128:
        next_action = "check_request"
    if not isinstance(request_id, str) or len(request_id) > 128:
        request_id = None
    if type(retry_after) is not int or not 0 <= retry_after <= 3600:
        retry_after = None
    return ZentureMCPError(
        code,
        status_code=status_code,
        retryable=retryable,
        next_action=next_action,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _decode_tool_result(result: object, *, expected_key: str | None = None) -> dict[str, object]:
    structured = _structured_content(result)
    payload = _validate_safe_payload(structured)
    if expected_key is not None and payload.get("idempotency_key") != expected_key:
        raise ZentureMCPProtocolError("idempotency_key_mismatch")
    if _is_error_result(result) or "error" in payload:
        raise _error_from_payload(payload)
    return payload


def _parse_model(model: type[ModelT], payload: Mapping[str, object]) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ZentureMCPProtocolError("invalid_result_contract") from exc


def _tool_names(result: object) -> tuple[str, ...]:
    result_mapping = _mapping(result)
    tools: object = (
        result_mapping.get("tools")
        if result_mapping is not None
        else getattr(result, "tools", None)
    )
    if not isinstance(tools, Iterable) or isinstance(tools, str | bytes | Mapping):
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    names: list[str] = []
    for tool in cast("Iterable[object]", tools):
        tool_mapping = _mapping(tool)
        name = tool_mapping.get("name") if tool_mapping is not None else getattr(tool, "name", None)
        if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
            raise ZentureMCPProtocolError("invalid_tool_catalog")
        if name in names:
            raise ZentureMCPProtocolError("duplicate_tool_name")
        names.append(name)
        if len(names) > 128:
            raise ZentureMCPProtocolError("tool_catalog_too_large")
    return tuple(names)


def _validate_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
    result = dict(arguments)
    _bounded_json(result, label="arguments", limit=_MAX_ARGUMENT_BYTES)
    return result


def _run_request(*, task: str, artifact: dict[str, Any], profile: str) -> dict[str, object]:
    request = PrepareKnowledgeRunRequest(
        task=task,
        artifact=cast("RunArtifact", artifact),
        profile=RunProfile(profile),
    )
    return cast("dict[str, object]", request.model_dump(mode="json"))


def _require_run_id(actual: str, expected: str) -> None:
    if actual != expected:
        raise ZentureMCPProtocolError("run_id_mismatch")


def _read_result(payload: dict[str, object], *, expected_run_id: str) -> McpRunRead:
    replay_payload = payload.get("event_replay")
    run_payload = {key: value for key, value in payload.items() if key != "event_replay"}
    run = _parse_model(PublicRunResponse, run_payload)
    _require_run_id(run.run_id, expected_run_id)
    replay = None
    if replay_payload is not None:
        replay_mapping = _mapping(replay_payload)
        if replay_mapping is None:
            raise ZentureMCPProtocolError("invalid_event_replay")
        replay = _parse_model(ListRunEventsResponse, replay_mapping)
        if any(event.run_id != expected_run_id for event in replay.events):
            raise ZentureMCPProtocolError("run_id_mismatch")
    return McpRunRead(run=run, event_replay=replay)


def _list_arguments(
    *,
    status: Sequence[str] | None,
    decision: Sequence[str] | None,
    profile: Sequence[str] | None,
    created_after: str | None,
    created_before: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, object]:
    request = McpListRunsRequest(
        status=tuple(status or ()),
        decision=tuple(decision or ()),
        profile=tuple(profile or ()),
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        cursor=cursor,
    )
    return cast("dict[str, object]", request.model_dump(mode="json", exclude_none=True))


class _PortalTransport:
    """Synchronous port that runs every call on the background loop of one portal."""

    def __init__(self, portal: Any, transport: AsyncMcpTransport) -> None:
        self._portal = portal
        self._transport = transport

    def list_tools(self) -> object:
        return self._portal.call(self._transport.list_tools)

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        return self._portal.call(self._transport.call_tool, name, arguments)


def _reject_running_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "McpClient is synchronous and cannot run inside an event loop; "
        "use AsyncMcpClient.connect() with 'async with' instead"
    )


class McpClient:
    """Synchronous MCP peer adapter over one caller-owned transport."""

    def __init__(self, transport: SyncMcpTransport) -> None:
        self._transport = transport

    @classmethod
    @contextmanager
    def connect(
        cls,
        endpoint: str | McpEndpoint | None = None,
        *,
        session: AuthSession | None = None,
        bearer_token: AsyncBearerTokenProvider | None = None,
        timeout: float = 30.0,
    ) -> Generator[McpClient, None, None]:
        """Connect like :meth:`AsyncMcpClient.connect` on a dedicated background event loop.

        The same asynchronous implementation runs on a loop thread that is
        stopped, with the transport closed, when the context exits.
        """

        _reject_running_event_loop()
        from anyio.from_thread import start_blocking_portal

        with start_blocking_portal() as portal:
            opened = AsyncMcpClient.connect(
                endpoint, session=session, bearer_token=bearer_token, timeout=timeout
            )
            failure: BaseException | None = None
            with portal.wrap_async_context_manager(opened) as peer:
                try:
                    # Sync and async peers share the internal transport port on the portal thread.
                    yield cls(_PortalTransport(portal, peer._transport))  # pyright: ignore[reportPrivateUsage]
                except BaseException as exc:
                    # Close the loop side cleanly; the caller sees its own exception unwrapped.
                    failure = exc
            if failure is not None:
                raise failure

    def list_tools(self) -> tuple[str, ...]:
        """Return advertised tool names as a tuple, without calling a product tool.

        This method does not return descriptions, input/output schemas or annotations.
        Use membership to check optional attach_artifact availability. Transport
        failure raises ZentureMCPError; malformed catalogs raise ZentureMCPProtocolError.
        """

        try:
            return _tool_names(self._transport.list_tools())
        except ZentureMCPError:
            raise
        except Exception as exc:
            raise ZentureMCPError(
                "mcp_transport_unavailable",
                status_code=503,
                retryable=True,
                next_action="retry_later",
            ) from exc

    def require_product_tools(self) -> None:
        """Check that run, list_runs, get_run, cancel_run and record_run_outcome exist.

        Returns None on success; missing core tools raise ZentureMCPProtocolError.
        attach_artifact is optional. This check establishes discovery, not permission,
        credits, file-byte availability or successful execution.
        """

        names = set(self.list_tools())
        if not _REQUIRED_PRODUCT_TOOL_NAMES.issubset(names):
            raise ZentureMCPProtocolError("tool_catalog_incomplete")

    def _call(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        validated = _validate_arguments(arguments)
        try:
            result = self._transport.call_tool(name, validated)
        except ZentureMCPError:
            raise
        except Exception as exc:
            raise ZentureMCPError(
                "mcp_transport_unavailable",
                status_code=503,
                retryable=True,
                next_action="retry_later",
            ) from exc
        return _decode_tool_result(
            result,
            expected_key=cast("str", validated["idempotency_key"]) if name == "run" else None,
        )

    def run(
        self,
        *,
        task: str,
        artifact: dict[str, Any],
        profile: str = "standard",
        idempotency_key: str = cast("str", _OMITTED_RUN_KEY),
    ) -> McpRunResponse:
        """Start evaluation of one explicitly selected answer; do not wait for completion.

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
                MCP keys allow 1-128 ASCII letters, digits, _ or -. Omission generates
                a random key per call; explicit None and whitespace are rejected.

        Returns:
            A PublicRunResponse-compatible object with run_id and possibly ongoing
            status. Its idempotency_key attribute is excluded from model_dump();
            persist the key separately before dispatch. Use get_run(run_id, view="summary") for
            progress and get_run(run_id, view="full").run for available evaluation content.

        Raises:
            ValueError/ValidationError for local inputs; ZentureMCPError for safe
            tool/transport failures (with the attempted key), and its subclass
            ZentureMCPProtocolError for invalid results or mismatched receipts.

        Starting may consume Credits or a Guest Trial slot. completed is technical
        completion, not a ready acceptance decision. No automatic timeout/server-error
        retry occurs. See docs/run-guide.md and docs/mcp-client.md for recovery.
        """

        key = validate_run_idempotency_key(
            uuid4().hex if idempotency_key is _OMITTED_RUN_KEY else idempotency_key
        )
        arguments = _run_request(task=task, artifact=artifact, profile=profile)
        arguments["idempotency_key"] = key
        try:
            return _parse_model(McpRunResponse, self._call("run", arguments))
        except ZentureMCPError as exc:
            exc.idempotency_key = key
            raise

    def attach_artifact(
        self, *, file_name: str, mime_type: str, byte_size: int, content_hash: str
    ) -> ArtifactUploadResponse:
        """Register host-selected file bytes using matching metadata; does not start a Run.

        Args:
            file_name: Selected name, 1-255 characters; not a path.
            mime_type: Actual media type, 1-127 characters, e.g. application/pdf.
            byte_size: Exact selected byte count, 1-10,485,760 (10 MiB).
            content_hash: SHA-256 of those bytes, 64 hexadecimal characters;
                uppercase and surrounding whitespace are normalized.

        Returns ArtifactUploadResponse with the registered artifact_ref. Registration
        does not enable evaluation: Run preparation currently rejects zenture_ref.
        The MCP host must supply the real bytes through its resolver. This method
        neither reads a Python file nor sends bytes in JSON. Check list_tools() first;
        an absent resolver cannot be repaired by inventing metadata or reselecting.
        Invalid metadata raises ValidationError; tool failures raise ZentureMCPError.
        """

        request = McpArtifactRequest(
            file_name=file_name,
            mime_type=mime_type,
            byte_size=byte_size,
            content_hash=content_hash,
        )
        return _parse_model(
            ArtifactUploadResponse, self._call("attach_artifact", request.model_dump(mode="json"))
        )

    def list_runs(
        self,
        *,
        status: Sequence[str] | None = None,
        decision: Sequence[str] | None = None,
        profile: Sequence[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
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
            created_after, created_before: ISO-8601 timestamps with timezone, or
                None for open bounds; after must not be later than before.
            limit: 1-50, default 5.
            cursor: Previous next_cursor copied unchanged, or None for first page.

        Returns ListRunsResponse with runs and next_cursor. Use get_run for details.
        Preserve filters when following a cursor. Clarify ambiguous matches before
        starting/cancelling a Run. Local invalid inputs raise ValidationError;
        server-rejected filters and transport failures raise ZentureMCPError.
        """

        return _parse_model(
            ListRunsResponse,
            self._call(
                "list_runs",
                _list_arguments(
                    status=status,
                    decision=decision,
                    profile=profile,
                    created_after=created_after,
                    created_before=created_before,
                    limit=limit,
                    cursor=cursor,
                ),
            ),
        )

    def get_run(
        self,
        run_id: str,
        *,
        view: Literal["summary", "full"] = "summary",
        replay_cursor: str | None = None,
        replay_limit: int = 50,
        include_event_replay: bool = False,
    ) -> McpRunRead:
        """Read one owned Run without starting, retrying or cancelling evaluation.

        Args:
            run_id: Copy run_id from run or list_runs; never invent an identifier.
            view: "summary" for progress (default), "full" for available results.
            replay_cursor: Previous event_replay.next_cursor; None for first page.
            replay_limit: 1-50 events, default 50.
            include_event_replay: False by default. Set True to request replay;
                a cursor alone does not enable it.

        Returns a wrapper: .run is PublicRunResponse, .event_replay is the optional
        ListRunEventsResponse. Read .run.safe_result_content availability before
        using findings. Technical completion differs from acceptance_decision.
        ValidationError rejects bad inputs; ZentureMCPError reports tool failures;
        ZentureMCPProtocolError rejects a foreign run_id or malformed response.
        """

        request = McpGetRunRequest(
            run_id=run_id,
            view=view,
            replay_cursor=replay_cursor,
            replay_limit=replay_limit,
            include_event_replay=include_event_replay,
        )
        arguments = request.model_dump(mode="json", exclude_none=True)
        if not include_event_replay:
            arguments.pop("include_event_replay", None)
        return _read_result(
            self._call("get_run", arguments),
            expected_run_id=request.run_id,
        )

    def replay_events(
        self, run_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> ListRunEventsResponse:
        """Read one event page for an owned Run; no streaming or automatic pagination.

        run_id comes from run/list_runs. cursor copies the previous next_cursor, or
        None selects the first page. limit is 1-50 (default 50). Returns
        ListRunEventsResponse with events, has_more and next_cursor. Uses get_run
        with include_event_replay=True. Missing replay raises ZentureMCPProtocolError;
        other tool/transport failures raise ZentureMCPError.
        """

        read = self.get_run(
            run_id,
            replay_cursor=cursor,
            replay_limit=limit,
            include_event_replay=True,
        )
        if read.event_replay is None:
            raise ZentureMCPProtocolError("event_replay_missing")
        return read.event_replay

    def cancel_run(self, run_id: str) -> PublicRunResponse:
        """Request cancellation only for the user's explicit stop request.

        run_id identifies one owned active Run; resolve ambiguity with list_runs
        before calling. Returns PublicRunResponse. cancel_requested is pending:
        read the same Run with get_run until its actual final state is known.
        A timeout/disconnect does not authorize cancellation. Cancellation does not
        promise a refund. Invalid IDs raise ValidationError; service errors raise
        ZentureMCPError. No caller idempotency_key argument exists on this method.
        """

        request = McpGetRunRequest(run_id=run_id)
        result = _parse_model(
            PublicRunResponse,
            self._call("cancel_run", {"run_id": request.run_id}),
        )
        _require_run_id(result.run_id, request.run_id)
        return result

    def record_run_outcome(
        self,
        run_id: str,
        *,
        outcome: Literal["used", "edited", "rejected", "escalated", "not_sure"],
        finding_adjudications: Sequence[dict[str, str]] | None = None,
        edited_artifact_ref: str | None = None,
    ) -> PublicRunResponse:
        """Record explicit user feedback for one completed owned Run.

        Args:
            run_id: Existing Run ID from run/list_runs/get_run.
            outcome: used (as-is), edited (after changes), rejected (not used),
                escalated (further review), or not_sure (undecided).
            finding_adjudications: Up to 20 {"finding_ref": "...", "outcome": "..."}
                objects. Copy finding_ref from this Run's full result; each outcome
                is confirmed/rejected/partially_valid/not_sure. None/[] skips them.
            edited_artifact_ref: Registered owned art_... reference, required exactly
                for edited; otherwise omit or use None. It is not an inline diff.

        Returns the updated PublicRunResponse, preserving the original acceptance
        verdict. Does not re-evaluate edited content. ValidationError rejects invalid
        combinations; tool/transport failures raise ZentureMCPError. Do not infer
        feedback from silence or comments. No caller idempotency_key is accepted.
        """

        request = McpOutcomeRequest(
            run_id=run_id,
            outcome=outcome,
            finding_adjudications=tuple(
                McpFindingAdjudication.model_validate(item)
                for item in (finding_adjudications or ())
            ),
            edited_artifact_ref=edited_artifact_ref,
        )
        result = _parse_model(
            PublicRunResponse,
            self._call("record_run_outcome", request.model_dump(mode="json", exclude_none=True)),
        )
        _require_run_id(result.run_id, request.run_id)
        return result


class AsyncMcpClient:
    """Asynchronous MCP peer adapter over one caller-owned transport."""

    def __init__(self, transport: AsyncMcpTransport) -> None:
        self._transport = transport

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        endpoint: str | McpEndpoint | None = None,
        *,
        session: AuthSession | None = None,
        bearer_token: AsyncBearerTokenProvider | None = None,
        timeout: float = 30.0,
    ) -> AsyncGenerator[AsyncMcpClient, None]:
        """Connect through the official Streamable HTTP transport.

        With neither ``session`` nor ``bearer_token`` the stored authorization
        of the endpoint is used; this never starts a login and raises
        ``AuthorizationRequired`` when no authorization is stored.

        An exception raised by the caller inside the ``async with`` body is
        handed back as raised even though the official client runs in task
        groups. Only when another failure of the connection happens at the same
        time can the caller see a ``BaseExceptionGroup`` (catch it with
        ``except*``).
        """

        from zenture._auth.model import DEFAULT_ENDPOINT

        target = DEFAULT_ENDPOINT if endpoint is None else endpoint
        owned: AuthSession | None = None
        if session is None and bearer_token is None:
            from zenture._auth.stored import open_stored_session

            url = target.url if isinstance(target, McpEndpoint) else target
            owned = session = await asyncio.to_thread(open_stored_session, url)
        try:
            async with open_streamable_http_transport(
                target,
                bearer_token=bearer_token,
                session=session,
                timeout=timeout,
            ) as transport:
                yield cls(transport)
        finally:
            if owned is not None:
                owned.close()

    async def list_tools(self) -> tuple[str, ...]:
        """Return advertised tool names as a tuple, without calling a product tool.

        This method does not return descriptions, input/output schemas or annotations.
        Use membership to check optional attach_artifact availability. Transport
        failure raises ZentureMCPError; malformed catalogs raise ZentureMCPProtocolError.

        Async variant: await this call.
        """

        try:
            return _tool_names(await self._transport.list_tools())
        except ZentureMCPError:
            raise
        except Exception as exc:
            raise ZentureMCPError(
                "mcp_transport_unavailable",
                status_code=503,
                retryable=True,
                next_action="retry_later",
            ) from exc

    async def require_product_tools(self) -> None:
        """Check that run, list_runs, get_run, cancel_run and record_run_outcome exist.

        Returns None on success; missing core tools raise ZentureMCPProtocolError.
        attach_artifact is optional. This check establishes discovery, not permission,
        credits, file-byte availability or successful execution.

        Async variant: await this call.
        """

        names = set(await self.list_tools())
        if not _REQUIRED_PRODUCT_TOOL_NAMES.issubset(names):
            raise ZentureMCPProtocolError("tool_catalog_incomplete")

    async def _call(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        validated = _validate_arguments(arguments)
        try:
            result = await self._transport.call_tool(name, validated)
        except ZentureMCPError:
            raise
        except Exception as exc:
            raise ZentureMCPError(
                "mcp_transport_unavailable",
                status_code=503,
                retryable=True,
                next_action="retry_later",
            ) from exc
        return _decode_tool_result(
            result,
            expected_key=cast("str", validated["idempotency_key"]) if name == "run" else None,
        )

    async def run(
        self,
        *,
        task: str,
        artifact: dict[str, Any],
        profile: str = "standard",
        idempotency_key: str = cast("str", _OMITTED_RUN_KEY),
    ) -> McpRunResponse:
        """Start evaluation of one explicitly selected answer; do not wait for completion.

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
                MCP keys allow 1-128 ASCII letters, digits, _ or -. Omission generates
                a random key per call; explicit None and whitespace are rejected.

        Returns:
            A PublicRunResponse-compatible object with run_id and possibly ongoing
            status. Its idempotency_key attribute is excluded from model_dump();
            persist the key separately before dispatch. Use get_run(run_id, view="summary") for
            progress and get_run(run_id, view="full").run for available evaluation content.

        Raises:
            ValueError/ValidationError for local inputs; ZentureMCPError for safe
            tool/transport failures (with the attempted key), and its subclass
            ZentureMCPProtocolError for invalid results or mismatched receipts.

        Starting may consume Credits or a Guest Trial slot. completed is technical
        completion, not a ready acceptance decision. No automatic timeout/server-error
        retry occurs. See docs/run-guide.md and docs/mcp-client.md for recovery.

        Async variant: await this call.
        """

        key = validate_run_idempotency_key(
            uuid4().hex if idempotency_key is _OMITTED_RUN_KEY else idempotency_key
        )
        arguments = _run_request(task=task, artifact=artifact, profile=profile)
        arguments["idempotency_key"] = key
        try:
            return _parse_model(McpRunResponse, await self._call("run", arguments))
        except ZentureMCPError as exc:
            exc.idempotency_key = key
            raise

    async def attach_artifact(
        self, *, file_name: str, mime_type: str, byte_size: int, content_hash: str
    ) -> ArtifactUploadResponse:
        """Register host-selected file bytes using matching metadata; does not start a Run.

        Args:
            file_name: Selected name, 1-255 characters; not a path.
            mime_type: Actual media type, 1-127 characters, e.g. application/pdf.
            byte_size: Exact selected byte count, 1-10,485,760 (10 MiB).
            content_hash: SHA-256 of those bytes, 64 hexadecimal characters;
                uppercase and surrounding whitespace are normalized.

        Returns ArtifactUploadResponse with the registered artifact_ref. Registration
        does not enable evaluation: Run preparation currently rejects zenture_ref.
        The MCP host must supply the real bytes through its resolver. This method
        neither reads a Python file nor sends bytes in JSON. Check list_tools() first;
        an absent resolver cannot be repaired by inventing metadata or reselecting.
        Invalid metadata raises ValidationError; tool failures raise ZentureMCPError.

        Async variant: await this call.
        """

        request = McpArtifactRequest(
            file_name=file_name,
            mime_type=mime_type,
            byte_size=byte_size,
            content_hash=content_hash,
        )
        return _parse_model(
            ArtifactUploadResponse,
            await self._call("attach_artifact", request.model_dump(mode="json")),
        )

    async def list_runs(
        self,
        *,
        status: Sequence[str] | None = None,
        decision: Sequence[str] | None = None,
        profile: Sequence[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
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
            created_after, created_before: ISO-8601 timestamps with timezone, or
                None for open bounds; after must not be later than before.
            limit: 1-50, default 5.
            cursor: Previous next_cursor copied unchanged, or None for first page.

        Returns ListRunsResponse with runs and next_cursor. Use get_run for details.
        Preserve filters when following a cursor. Clarify ambiguous matches before
        starting/cancelling a Run. Local invalid inputs raise ValidationError;
        server-rejected filters and transport failures raise ZentureMCPError.

        Async variant: await this call.
        """

        return _parse_model(
            ListRunsResponse,
            await self._call(
                "list_runs",
                _list_arguments(
                    status=status,
                    decision=decision,
                    profile=profile,
                    created_after=created_after,
                    created_before=created_before,
                    limit=limit,
                    cursor=cursor,
                ),
            ),
        )

    async def get_run(
        self,
        run_id: str,
        *,
        view: Literal["summary", "full"] = "summary",
        replay_cursor: str | None = None,
        replay_limit: int = 50,
        include_event_replay: bool = False,
    ) -> McpRunRead:
        """Read one owned Run without starting, retrying or cancelling evaluation.

        Args:
            run_id: Copy run_id from run or list_runs; never invent an identifier.
            view: "summary" for progress (default), "full" for available results.
            replay_cursor: Previous event_replay.next_cursor; None for first page.
            replay_limit: 1-50 events, default 50.
            include_event_replay: False by default. Set True to request replay;
                a cursor alone does not enable it.

        Returns a wrapper: .run is PublicRunResponse, .event_replay is the optional
        ListRunEventsResponse. Read .run.safe_result_content availability before
        using findings. Technical completion differs from acceptance_decision.
        ValidationError rejects bad inputs; ZentureMCPError reports tool failures;
        ZentureMCPProtocolError rejects a foreign run_id or malformed response.

        Async variant: await this call.
        """

        request = McpGetRunRequest(
            run_id=run_id,
            view=view,
            replay_cursor=replay_cursor,
            replay_limit=replay_limit,
            include_event_replay=include_event_replay,
        )
        arguments = request.model_dump(mode="json", exclude_none=True)
        if not include_event_replay:
            arguments.pop("include_event_replay", None)
        return _read_result(
            await self._call("get_run", arguments),
            expected_run_id=request.run_id,
        )

    async def replay_events(
        self, run_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> ListRunEventsResponse:
        """Read one event page for an owned Run; no streaming or automatic pagination.

        run_id comes from run/list_runs. cursor copies the previous next_cursor, or
        None selects the first page. limit is 1-50 (default 50). Returns
        ListRunEventsResponse with events, has_more and next_cursor. Uses get_run
        with include_event_replay=True. Missing replay raises ZentureMCPProtocolError;
        other tool/transport failures raise ZentureMCPError.

        Async variant: await this call.
        """

        read = await self.get_run(
            run_id,
            replay_cursor=cursor,
            replay_limit=limit,
            include_event_replay=True,
        )
        if read.event_replay is None:
            raise ZentureMCPProtocolError("event_replay_missing")
        return read.event_replay

    async def cancel_run(self, run_id: str) -> PublicRunResponse:
        """Request cancellation only for the user's explicit stop request.

        run_id identifies one owned active Run; resolve ambiguity with list_runs
        before calling. Returns PublicRunResponse. cancel_requested is pending:
        read the same Run with get_run until its actual final state is known.
        A timeout/disconnect does not authorize cancellation. Cancellation does not
        promise a refund. Invalid IDs raise ValidationError; service errors raise
        ZentureMCPError. No caller idempotency_key argument exists on this method.

        Async variant: await this call.
        """

        request = McpGetRunRequest(run_id=run_id)
        result = _parse_model(
            PublicRunResponse,
            await self._call("cancel_run", {"run_id": request.run_id}),
        )
        _require_run_id(result.run_id, request.run_id)
        return result

    async def record_run_outcome(
        self,
        run_id: str,
        *,
        outcome: Literal["used", "edited", "rejected", "escalated", "not_sure"],
        finding_adjudications: Sequence[dict[str, str]] | None = None,
        edited_artifact_ref: str | None = None,
    ) -> PublicRunResponse:
        """Record explicit user feedback for one completed owned Run.

        Args:
            run_id: Existing Run ID from run/list_runs/get_run.
            outcome: used (as-is), edited (after changes), rejected (not used),
                escalated (further review), or not_sure (undecided).
            finding_adjudications: Up to 20 {"finding_ref": "...", "outcome": "..."}
                objects. Copy finding_ref from this Run's full result; each outcome
                is confirmed/rejected/partially_valid/not_sure. None/[] skips them.
            edited_artifact_ref: Registered owned art_... reference, required exactly
                for edited; otherwise omit or use None. It is not an inline diff.

        Returns the updated PublicRunResponse, preserving the original acceptance
        verdict. Does not re-evaluate edited content. ValidationError rejects invalid
        combinations; tool/transport failures raise ZentureMCPError. Do not infer
        feedback from silence or comments. No caller idempotency_key is accepted.

        Async variant: await this call.
        """

        request = McpOutcomeRequest(
            run_id=run_id,
            outcome=outcome,
            finding_adjudications=tuple(
                McpFindingAdjudication.model_validate(item)
                for item in (finding_adjudications or ())
            ),
            edited_artifact_ref=edited_artifact_ref,
        )
        result = _parse_model(
            PublicRunResponse,
            await self._call(
                "record_run_outcome",
                request.model_dump(mode="json", exclude_none=True),
            ),
        )
        _require_run_id(result.run_id, request.run_id)
        return result


__all__ = ["AsyncMcpClient", "McpClient"]
