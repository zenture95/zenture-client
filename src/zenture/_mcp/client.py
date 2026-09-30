"""Peer-level MCP Run clients backed by the canonical SDK contracts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Generator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

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
    PRODUCT_TOOL_NAMES,
    McpArtifactRequest,
    McpEndpoint,
    McpFindingAdjudication,
    McpGetRunRequest,
    McpListRunsRequest,
    McpOutcomeRequest,
    McpRunRead,
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

_MAX_ARGUMENT_BYTES = 256 * 1024
_MAX_RESULT_BYTES = 256 * 1024
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
    if not isinstance(content, (list, tuple)) or len(content) != 1:
        return result_mapping if result_mapping is not None else None
    block = content[0]
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


def _decode_tool_result(result: object) -> dict[str, object]:
    structured = _structured_content(result)
    payload = _validate_safe_payload(structured)
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
                    yield cls(_PortalTransport(portal, peer._transport))
                except BaseException as exc:
                    # Close the loop side cleanly; the caller sees its own exception unwrapped.
                    failure = exc
            if failure is not None:
                raise failure

    def list_tools(self) -> tuple[str, ...]:
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
        names = set(self.list_tools())
        if not set(PRODUCT_TOOL_NAMES).issubset(names):
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
        return _decode_tool_result(result)

    def run(
        self, *, task: str, artifact: dict[str, Any], profile: str = "standard"
    ) -> PublicRunResponse:
        return _parse_model(
            PublicRunResponse,
            self._call("run", _run_request(task=task, artifact=artifact, profile=profile)),
        )

    def attach_artifact(
        self, *, file_name: str, mime_type: str, byte_size: int, content_hash: str
    ) -> ArtifactUploadResponse:
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
        names = set(await self.list_tools())
        if not set(PRODUCT_TOOL_NAMES).issubset(names):
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
        return _decode_tool_result(result)

    async def run(
        self, *, task: str, artifact: dict[str, Any], profile: str = "standard"
    ) -> PublicRunResponse:
        return _parse_model(
            PublicRunResponse,
            await self._call("run", _run_request(task=task, artifact=artifact, profile=profile)),
        )

    async def attach_artifact(
        self, *, file_name: str, mime_type: str, byte_size: int, content_hash: str
    ) -> ArtifactUploadResponse:
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
