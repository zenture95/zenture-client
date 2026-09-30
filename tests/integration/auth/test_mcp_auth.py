"""Scenario 7: the session feeds per-request bearers into the real MCP client stack."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthorizationRequired, PermissionDenied
from zenture._auth.http_auth import SessionAuth
from zenture._auth.login import LoginFlow
from zenture._auth.store import MemoryStore
from zenture._mcp.client import AsyncMcpClient

if TYPE_CHECKING:
    from pathlib import Path

    from zenture._auth.session import AuthSession

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _session(env: Environment, lock_dir: Path) -> AuthSession:
    opener, _ = browser_opener()
    return LoginFlow(runtime_for(MemoryStore(), lock_dir, opener)).login(endpoint=env.endpoint)


async def _post(client: httpx2.AsyncClient, env: Environment) -> httpx2.Response:
    return await client.post(env.endpoint, json=INITIALIZE, headers=HEADERS)


@pytest.mark.asyncio
async def test_async_client_connects_with_a_session_and_lists_tools(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)

    async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
        assert sorted(await client.list_tools()) == ["echo", "ping"]

    assert env.mcp.guard.requests
    assert all(entry["bearer"] and entry["status"] == 200 for entry in env.mcp.guard.requests)
    assert env.issuer.refresh_requests == []  # the login access token was reused


@pytest.mark.asyncio
async def test_session_and_static_bearer_are_mutually_exclusive(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)

    with pytest.raises(ValueError, match="exactly one"):
        async with AsyncMcpClient.connect(env.endpoint, session=session, bearer_token="x"):
            pass


@pytest.mark.asyncio
async def test_static_bearer_path_is_unchanged(env: Environment, lock_dir: Path) -> None:
    session = _session(env, lock_dir)
    token = session._core._access
    assert token is not None

    async with AsyncMcpClient.connect(env.endpoint, bearer_token=token) as client:
        assert sorted(await client.list_tools()) == ["echo", "ping"]


@pytest.mark.asyncio
async def test_401_triggers_exactly_one_refresh_and_one_retry(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    env.issuer.expire_access_tokens()  # server now rejects the cached access token

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        response = await _post(client, env)

    assert response.status_code == 200
    assert len(env.issuer.refresh_requests) == 1
    assert [entry["status"] for entry in env.mcp.guard.requests] == [401, 200]


@pytest.mark.asyncio
async def test_concurrent_401s_share_one_serialized_refresh(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    env.issuer.expire_access_tokens()
    env.issuer.refresh_delay = 0.2

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        responses = await asyncio.gather(*[_post(client, env) for _ in range(5)])

    assert [r.status_code for r in responses] == [200] * 5
    assert len(env.issuer.refresh_requests) == 1
    assert env.issuer.reuse_events == 0


@pytest.mark.asyncio
async def test_second_401_requires_authorization_without_a_third_attempt(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    env.mcp.guard.forced = [401, 401]

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        with pytest.raises(AuthorizationRequired):
            await _post(client, env)

    assert len(env.issuer.refresh_requests) == 1
    assert len(env.mcp.guard.requests) == 2
    assert len(env.issuer.auth_requests) == 1  # only the original login; no new browser login


@pytest.mark.asyncio
async def test_403_is_final_permission_denied_without_refresh_or_login(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    env.mcp.guard.forced = [403]

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        with pytest.raises(PermissionDenied):
            await _post(client, env)

    assert env.issuer.refresh_requests == []
    assert len(env.issuer.auth_requests) == 1
    assert len(env.mcp.guard.requests) == 1


def test_sync_auth_flow_shares_the_same_401_rules(env: Environment, lock_dir: Path) -> None:
    session = _session(env, lock_dir)
    env.issuer.expire_access_tokens()

    with httpx2.Client(auth=SessionAuth(session)) as client:
        response = client.post(env.endpoint, json=INITIALIZE, headers=HEADERS)

    assert response.status_code == 200
    assert len(env.issuer.refresh_requests) == 1


@pytest.mark.asyncio
async def test_expired_cached_access_token_is_refreshed_before_the_request(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    session._core.clock = lambda: session._core._expires_at + 1

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        response = await _post(client, env)

    assert response.status_code == 200
    assert len(env.issuer.refresh_requests) == 1
    assert [entry["status"] for entry in env.mcp.guard.requests] == [200]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("forced", "expected"),
    [
        ([401, 401], AuthorizationRequired),
        ([403], PermissionDenied),
        ([0, 0, 401, 401], AuthorizationRequired),  # fails on tools/list, after initialize
        ([0, 0, 403], PermissionDenied),
    ],
)
async def test_official_client_surfaces_auth_errors_unwrapped(
    env: Environment, lock_dir: Path, forced: list[int], expected: type[Exception]
) -> None:
    session = _session(env, lock_dir)
    env.mcp.guard.forced = list(forced)
    caught: Any = None

    try:
        async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
            await client.list_tools()
    except Exception as exc:
        caught = exc

    assert isinstance(caught, expected)
