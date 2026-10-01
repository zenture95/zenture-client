"""MCP transport ports and the optional official Streamable HTTP binding."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

from zenture._auth.errors import AuthError
from zenture._mcp.contracts import BearerTokenProvider, McpEndpoint
from zenture.errors import ZentureMCPDependencyError, ZentureMCPError

if TYPE_CHECKING:
    from zenture._auth.session import AuthSession

AsyncBearerTokenProvider: TypeAlias = str | Callable[[], str | Awaitable[str]]


class SyncMcpTransport(Protocol):
    """Synchronous protocol port used by the sync adapter and local tests."""

    def list_tools(self) -> object:
        """Return the negotiated MCP tool catalog."""

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """Invoke one negotiated MCP tool."""


class AsyncMcpTransport(Protocol):
    """Asynchronous protocol port used by the official transport and tests."""

    async def list_tools(self) -> object:
        """Return the negotiated MCP tool catalog."""

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        """Invoke one negotiated MCP tool."""


def resolve_bearer_token(provider: BearerTokenProvider | AsyncBearerTokenProvider) -> str:
    """Resolve one caller value without implementing a token lifecycle."""

    value: object = provider() if callable(provider) else provider
    if inspect.isawaitable(value):
        raise TypeError("async bearer providers require the asynchronous transport")
    return _validate_bearer_value(value)


async def resolve_async_bearer_token(provider: AsyncBearerTokenProvider) -> str:
    """Resolve one asynchronous caller value without refreshing or storing it."""

    value: object = provider() if callable(provider) else provider
    if inspect.isawaitable(value):
        value = await value
    return _validate_bearer_value(value)


def _auth_failure(exc: BaseException, depth: int = 0) -> AuthError | None:
    """Find an authorization failure raised inside the official client's task group."""

    if isinstance(exc, AuthError):
        return exc
    if depth >= 4:
        return None
    children: list[BaseException] = list(getattr(exc, "exceptions", ()))
    if exc.__cause__ is not None:
        children.append(exc.__cause__)
    for child in children:
        found = _auth_failure(child, depth + 1)
        if found is not None:
            return found
    return None


def _leaves(exc: BaseException, depth: int = 0) -> list[BaseException]:
    children: list[BaseException] = list(getattr(exc, "exceptions", ()))
    if not children or depth >= 4:
        return [exc]
    return [leaf for child in children for leaf in _leaves(child, depth + 1)]


def _is_network_failure(exc: BaseException, network_error: type[BaseException] | None) -> bool:
    """Whether every leaf of a task-group failure is a transport error of the HTTP stack."""

    if network_error is None:
        return False
    return all(isinstance(leaf, network_error) for leaf in _leaves(exc))


def _validate_bearer_value(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        raise ValueError("bearer token must be a bounded non-empty value")
    return value


class _OfficialAsyncMcpTransport:
    """Small wrapper around one initialized official MCP client session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> object:
        return await self._session.list_tools()

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        return await self._session.call_tool(name, arguments=dict(arguments))


@asynccontextmanager
async def open_streamable_http_transport(
    endpoint: str | McpEndpoint,
    *,
    bearer_token: AsyncBearerTokenProvider | None = None,
    session: AuthSession | None = None,
    timeout: float = 30.0,
) -> AsyncGenerator[AsyncMcpTransport, None]:
    """Open an initialized official Streamable HTTP MCP client session.

    The optional ``mcp`` dependency is imported only when this explicit
    integration is opened. The yielded transport retains no credential beyond
    the lifetime managed by the official session. With ``session`` the
    authorization session supplies a bearer per request (one serialized refresh
    and retry on 401); ``session`` and ``bearer_token`` are mutually exclusive.
    """

    if (session is None) == (bearer_token is None):
        raise ValueError("pass exactly one of session or bearer_token")
    target = endpoint if isinstance(endpoint, McpEndpoint) else McpEndpoint.from_value(endpoint)
    if type(timeout) not in {int, float} or timeout <= 0 or timeout > 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    if session is not None:
        from zenture._auth.model import within_resource

        if not within_resource(target.url, session._core.discovery.resource):  # pyright: ignore[reportPrivateUsage]
            raise ValueError("endpoint is outside the resource of this authorization session")
    token = await resolve_async_bearer_token(bearer_token) if bearer_token is not None else None
    try:
        mcp_module = importlib.import_module("mcp")
        streamable_http_module = importlib.import_module("mcp.client.streamable_http")
        httpx2_module = importlib.import_module("httpx2")
        client_session = mcp_module.ClientSession
        streamablehttp_client = streamable_http_module.streamable_http_client
        async_client = httpx2_module.AsyncClient
        timeout_type = httpx2_module.Timeout
    except (ImportError, AttributeError) as exc:
        raise ZentureMCPDependencyError() from exc

    yielded = False
    body_completed = False
    caller_error: BaseException | None = None
    try:
        client_options: dict[str, Any] = {
            "timeout": timeout_type(timeout, read=timeout),
            "follow_redirects": False,
        }
        if session is not None:
            from zenture._auth.http_auth import SessionAuth

            client_options["auth"] = SessionAuth(session)
        else:
            client_options = {"headers": {"Authorization": f"Bearer {token}"}, **client_options}
        async with AsyncExitStack() as stack:
            http_client = await stack.enter_async_context(async_client(**client_options))
            streams = await stack.enter_async_context(
                streamablehttp_client(target.url, http_client=http_client)
            )
            read_stream, write_stream = streams
            mcp_session = await stack.enter_async_context(client_session(read_stream, write_stream))
            await mcp_session.initialize()
            yielded = True
            try:
                yield _OfficialAsyncMcpTransport(mcp_session)
            except BaseException as body_error:
                caller_error = body_error
                raise
            body_completed = True
    except asyncio.CancelledError:
        raise
    except ZentureMCPError:
        raise
    except Exception as exc:
        if body_completed:
            return
        if (
            caller_error is not None
            and isinstance(exc, BaseExceptionGroup)
            and _leaves(exc) == [caller_error]
        ):
            # The official task groups wrap the caller's own exception; hand it back as raised.
            exc = caller_error if isinstance(caller_error, Exception) else exc
        failure = _auth_failure(exc)
        if failure is not None:
            raise failure from None
        if (
            yielded
            and not body_completed
            and not _is_network_failure(exc, getattr(httpx2_module, "HTTPError", None))
        ):
            raise exc
        raise ZentureMCPError(
            "mcp_transport_unavailable",
            status_code=503,
            retryable=True,
            next_action="retry_later",
        ) from exc


__all__ = [
    "AsyncBearerTokenProvider",
    "AsyncMcpTransport",
    "SyncMcpTransport",
    "open_streamable_http_transport",
    "resolve_async_bearer_token",
    "resolve_bearer_token",
]
