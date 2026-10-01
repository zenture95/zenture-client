"""The session bearer is sent only to the resource the session was discovered for."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthError
from zenture._auth.http_auth import SessionAuth
from zenture._auth.login import LoginFlow
from zenture._auth.store import MemoryStore
from zenture._mcp.client import AsyncMcpClient, McpClient

if TYPE_CHECKING:
    from collections.abc import Iterator
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


@pytest.fixture
def other() -> Iterator[Environment]:
    environment = Environment().start()
    try:
        yield environment
    finally:
        environment.stop()


def _session(env: Environment, lock_dir: Path) -> AuthSession:
    opener, _ = browser_opener()
    return LoginFlow(runtime_for(MemoryStore(), lock_dir, opener)).login(endpoint=env.endpoint)


def _foreign_urls(env: Environment, other: Environment) -> list[str]:
    return [
        other.endpoint,  # other loopback port
        f"http://localhost:{env.mcp.port}/",  # other host, same port
        f"https://127.0.0.1:{env.mcp.port}/",  # other scheme
    ]


def _assert_nothing_sent(env: Environment, other: Environment) -> None:
    assert other.mcp.guard.requests == []
    assert env.mcp.guard.requests == []
    assert env.issuer.refresh_requests == []


@pytest.mark.asyncio
async def test_async_peer_refuses_a_foreign_endpoint_without_sending_a_bearer(
    env: Environment, other: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    session._core.clock = lambda: session._core._expires_at + 1  # pyright: ignore[reportPrivateUsage]  # a token fetch would refresh

    for url in _foreign_urls(env, other):
        with pytest.raises(ValueError, match="resource"):
            async with AsyncMcpClient.connect(url, session=session):
                pass

    _assert_nothing_sent(env, other)


def test_sync_peer_refuses_a_foreign_endpoint_without_sending_a_bearer(
    env: Environment, other: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    session._core.clock = lambda: session._core._expires_at + 1  # pyright: ignore[reportPrivateUsage]  # white-box test of internals

    for url in _foreign_urls(env, other):
        with pytest.raises(ValueError, match="resource"), McpClient.connect(url, session=session):
            pass

    _assert_nothing_sent(env, other)


@pytest.mark.asyncio
async def test_async_auth_flow_checks_every_request_before_fetching_a_token(
    env: Environment, other: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    session._core.clock = lambda: session._core._expires_at + 1  # pyright: ignore[reportPrivateUsage]  # white-box test of internals

    async with httpx2.AsyncClient(auth=SessionAuth(session)) as client:
        for url in _foreign_urls(env, other)[:2]:
            with pytest.raises(AuthError) as caught:
                await client.post(url, json=INITIALIZE, headers=HEADERS)
            assert caught.value.code == "resource_mismatch"

    _assert_nothing_sent(env, other)


def test_sync_auth_flow_checks_every_request_before_fetching_a_token(
    env: Environment, other: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)
    session._core.clock = lambda: session._core._expires_at + 1  # pyright: ignore[reportPrivateUsage]  # white-box test of internals

    with httpx2.Client(auth=SessionAuth(session)) as client:
        for url in _foreign_urls(env, other)[:2]:
            with pytest.raises(AuthError) as caught:
                client.post(url, json=INITIALIZE, headers=HEADERS)
            assert caught.value.code == "resource_mismatch"

    _assert_nothing_sent(env, other)


@pytest.mark.asyncio
async def test_the_session_own_resource_still_works_with_a_path(
    env: Environment, lock_dir: Path
) -> None:
    session = _session(env, lock_dir)

    async with AsyncMcpClient.connect(env.endpoint, session=session) as client:
        assert sorted(await client.list_tools()) == ["echo", "ping"]
