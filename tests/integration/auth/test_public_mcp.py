"""Scenarios 3 and 7: public ``zenture.mcp`` peers share one session and never retry mutations."""

from __future__ import annotations

import inspect
import threading
from typing import TYPE_CHECKING

import pytest
from auth_harness import HANG, Environment, browser_opener, runtime_for

import zenture.mcp
from zenture._auth import login as login_module
from zenture._auth.errors import AuthorizationRequired
from zenture._auth.login import LoginFlow
from zenture._auth.store import MemoryStore
from zenture.errors import ZentureMCPError
from zenture.mcp import AsyncMcpClient, McpClient

if TYPE_CHECKING:
    from pathlib import Path

    from zenture._auth.session import AuthSession


def _login(env: Environment, store: MemoryStore, lock_dir: Path) -> AuthSession:
    opener, _ = browser_opener()
    return LoginFlow(runtime_for(store, lock_dir, opener)).login(endpoint=env.endpoint)


def _stored_environment(
    env: Environment, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> MemoryStore:
    """A completed login whose record the non-interactive path must find on its own."""

    store = MemoryStore()
    _login(env, store, lock_dir)

    def never_open(_url: str) -> bool:
        raise AssertionError("a browser must not be started")

    runtime = runtime_for(store, lock_dir, never_open)
    monkeypatch.setattr(login_module, "default_runtime", lambda: runtime)
    return store


def test_public_module_exposes_exactly_the_two_peers() -> None:
    assert sorted(zenture.mcp.__all__) == ["AsyncMcpClient", "McpClient"]
    tool_methods = {"run", "attach_artifact", "list_runs", "get_run", "cancel_run"}
    tool_methods |= {"record_run_outcome", "replay_events", "list_tools", "require_product_tools"}
    for name in tool_methods:
        sync_parameters = inspect.signature(getattr(McpClient, name)).parameters
        async_parameters = inspect.signature(getattr(AsyncMcpClient, name)).parameters
        assert list(sync_parameters) == list(async_parameters)
        assert inspect.iscoroutinefunction(getattr(AsyncMcpClient, name))
        assert not inspect.iscoroutinefunction(getattr(McpClient, name))
    for peer in (McpClient, AsyncMcpClient):
        parameters = inspect.signature(peer.connect).parameters
        assert list(parameters) == ["endpoint", "session", "bearer_token", "timeout"]
        assert all(
            parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            for name in ("session", "bearer_token", "timeout")
        )
        assert parameters["endpoint"].default is None
        assert parameters["timeout"].default == 30.0


@pytest.mark.asyncio
async def test_async_peer_without_credentials_uses_the_stored_binding_non_interactively(
    env: Environment, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_environment(env, lock_dir, monkeypatch)

    async with AsyncMcpClient.connect(env.endpoint) as client:
        assert sorted(await client.list_tools()) == ["echo", "ping"]

    assert len(env.issuer.auth_requests) == 1  # only the original login authorized
    assert len(env.issuer.refresh_requests) == 1  # the stored refresh authority supplied access


@pytest.mark.asyncio
async def test_async_peer_without_credentials_or_record_requires_login(
    env: Environment, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_for(MemoryStore(), lock_dir, lambda _url: pytest.fail("browser started"))
    monkeypatch.setattr(login_module, "default_runtime", lambda: runtime)

    with pytest.raises(AuthorizationRequired) as raised:
        async with AsyncMcpClient.connect(env.endpoint):
            pass

    assert "zenture auth login" in str(raised.value)
    assert env.issuer.auth_requests == []
    assert env.mcp.guard.requests == []


def test_sync_peer_runs_the_same_implementation_with_a_session(
    env: Environment, lock_dir: Path
) -> None:
    session = _login(env, MemoryStore(), lock_dir)

    with McpClient.connect(env.endpoint, session=session) as client:
        assert sorted(client.list_tools()) == ["echo", "ping"]

    assert env.issuer.refresh_requests == []
    assert all(entry["bearer"] for entry in env.mcp.guard.requests)


def test_sync_peer_without_credentials_uses_the_stored_binding(
    env: Environment, lock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_environment(env, lock_dir, monkeypatch)

    with McpClient.connect(env.endpoint) as client:
        assert sorted(client.list_tools()) == ["echo", "ping"]

    assert len(env.issuer.refresh_requests) == 1


def test_sync_peer_accepts_a_static_bearer_and_rejects_session_plus_bearer(
    env: Environment, lock_dir: Path
) -> None:
    session = _login(env, MemoryStore(), lock_dir)
    token = session._core._access
    assert token is not None

    with McpClient.connect(env.endpoint, bearer_token=token) as client:
        assert sorted(client.list_tools()) == ["echo", "ping"]
    with (
        pytest.raises(ValueError, match="exactly one"),
        McpClient.connect(env.endpoint, session=session, bearer_token=token),
    ):
        pass


@pytest.mark.asyncio
async def test_sync_peer_inside_a_running_event_loop_points_to_the_async_peer(
    env: Environment,
) -> None:
    with (
        pytest.raises(RuntimeError, match="AsyncMcpClient"),
        McpClient.connect(env.endpoint, bearer_token="x"),
    ):
        pass

    assert env.mcp.guard.requests == []


def _connect_and_use(
    env: Environment, session: AuthSession, body_error: type[BaseException] | None
) -> list[McpClient]:
    opened: list[McpClient] = []
    with McpClient.connect(env.endpoint, session=session) as client:
        opened.append(client)
        assert threading.active_count() > 1  # the portal's loop thread is running
        client.list_tools()
        if body_error is not None:
            raise body_error
    return opened


@pytest.mark.parametrize("body_error", [None, ValueError, KeyboardInterrupt])
def test_sync_peer_closes_deterministically_and_leaks_no_threads(
    env: Environment, lock_dir: Path, body_error: type[BaseException] | None
) -> None:
    session = _login(env, MemoryStore(), lock_dir)
    baseline = threading.active_count()
    closed: list[McpClient] = []

    for _ in range(3):
        if body_error is None:
            closed = _connect_and_use(env, session, None)
        else:
            with pytest.raises(body_error):  # the caller's own exception, unwrapped
                _connect_and_use(env, session, body_error)

    assert threading.active_count() <= baseline  # joined on exit; a leaked thread would exceed it
    if closed:
        with pytest.raises(ZentureMCPError):
            closed[0].list_tools()  # the transport is closed with the context


# -- Scenario 7: mutation safety -------------------------------------------------------------


def _tool_calls(env: Environment) -> list[int]:
    """Status of every ``tools/call`` request that reached the server, in order."""

    return [entry["status"] for entry in env.mcp.guard.requests if entry["rpc"] == "tools/call"]


@pytest.mark.asyncio
async def test_server_failure_on_a_tool_call_is_never_retried(
    env: Environment, lock_dir: Path
) -> None:
    session = _login(env, MemoryStore(), lock_dir)

    async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
        env.mcp.guard.requests.clear()
        env.mcp.guard.forced_tool_calls = [503]
        with pytest.raises(ZentureMCPError):
            await client._call("echo", {})

    assert _tool_calls(env) == [503]
    assert env.issuer.refresh_requests == []


async def _call_that_times_out(env: Environment, session: AuthSession) -> None:
    async with AsyncMcpClient.connect(env.endpoint, session=session, timeout=0.5) as client:
        env.mcp.guard.requests.clear()
        env.mcp.guard.forced_tool_calls = [HANG]
        await client._call("echo", {})


@pytest.mark.asyncio
async def test_timeout_on_a_tool_call_is_never_retried(env: Environment, lock_dir: Path) -> None:
    session = _login(env, MemoryStore(), lock_dir)

    with pytest.raises(ZentureMCPError) as raised:
        await _call_that_times_out(env, session)

    assert raised.value.retryable  # reported, but never replayed by the client
    assert _tool_calls(env) == [HANG]


@pytest.mark.asyncio
async def test_auth_rejected_tool_call_is_retried_exactly_once_after_one_refresh(
    env: Environment, lock_dir: Path
) -> None:
    session = _login(env, MemoryStore(), lock_dir)

    async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
        env.mcp.guard.requests.clear()
        env.mcp.guard.forced_tool_calls = [401]
        result = await client._call("echo", {})

    assert result == {"state": "done"}
    assert _tool_calls(env) == [401, 200]
    assert len(env.issuer.refresh_requests) == 1
    assert len(env.issuer.auth_requests) == 1  # the retry never starts a new authorization
