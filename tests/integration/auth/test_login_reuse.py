"""Scenario 8: explicit login reuses a stored authorization (D-35)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthUnavailable
from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import RecordKey

if TYPE_CHECKING:
    from pathlib import Path

    from file_store import FileStore


def _flow(store: FileStore, lock_dir: Path, opener: object) -> LoginFlow:
    return LoginFlow(runtime_for(store, lock_dir, opener))


class _RefusedAfterDiscovery(LoginFlow):
    """Discovery succeeds, then the MCP server stops answering (connection refused)."""

    def __init__(self, runtime: Any, env: Environment) -> None:
        super().__init__(runtime)
        self._env = env

    def prepare(self, *, session_only: bool, endpoint: str | None) -> Any:
        prepared = super().prepare(session_only=session_only, endpoint=endpoint)
        self._env.mcp.stop()
        return prepared


def _outage(env: Environment, mode: str, file_store: FileStore, lock_dir: Path) -> LoginFlow:
    opener = browser_opener()[0]
    runtime = runtime_for(file_store, lock_dir, opener)
    if mode == "refused":
        return _RefusedAfterDiscovery(runtime, env)
    env.mcp.guard.forced = [503] * 20
    return LoginFlow(runtime)


def _first_login(env: Environment, store: FileStore, lock_dir: Path) -> object:
    opener, opened = browser_opener()
    session = _flow(store, lock_dir, opener).login(endpoint=env.endpoint)
    assert len(opened) == 1
    return session.binding


def test_stored_ready_record_is_refreshed_and_probed_without_a_browser(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    binding = _first_login(env, file_store, lock_dir)
    env.mcp.guard.requests.clear()
    opener, opened = browser_opener()

    session = _flow(file_store, lock_dir, opener).login(endpoint=env.endpoint)

    assert opened == []
    assert session.binding == binding
    assert len(env.issuer.refresh_requests) == 1
    methods = [entry["method"] for entry in env.mcp.guard.requests]
    assert methods
    assert all(entry["status"] == 200 for entry in env.mcp.guard.requests)
    assert "POST" in methods  # initialize + tools/list probe over the real MCP client
    assert len(env.issuer.auth_requests) == 1  # still only the first login's authorization


@pytest.mark.asyncio
async def test_async_login_reuses_the_stored_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    binding = _first_login(env, file_store, lock_dir)
    opener, opened = browser_opener()

    session = await _flow(file_store, lock_dir, opener).login_async(endpoint=env.endpoint)

    assert opened == []
    assert session.binding == binding
    assert len(env.issuer.refresh_requests) == 1


def test_authorization_required_on_refresh_leads_to_a_new_browser_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    _first_login(env, file_store, lock_dir)
    env.issuer.refresh_behaviors = ["invalid_grant"]
    opener, opened = browser_opener()

    session = _flow(file_store, lock_dir, opener).login(endpoint=env.endpoint)

    assert len(opened) == 1
    assert len(env.issuer.auth_requests) == 2
    stored = file_store.load(RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID))
    assert stored is not None
    assert stored.state == "ready"
    assert stored.binding == session.binding


def test_unusable_access_at_the_probe_leads_to_a_new_browser_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    _first_login(env, file_store, lock_dir)
    env.mcp.guard.forced = [401, 401]
    opener, opened = browser_opener()

    _flow(file_store, lock_dir, opener).login(endpoint=env.endpoint)

    assert len(opened) == 1
    assert len(env.issuer.refresh_requests) == 2  # probe retry refresh + initial reuse refresh


def test_unavailable_store_during_reuse_never_starts_a_new_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    _first_login(env, file_store, lock_dir)
    file_store.fail_next()  # the `rotating` journal write fails
    opener, opened = browser_opener()

    with pytest.raises(AuthUnavailable):
        _flow(file_store, lock_dir, opener).login(endpoint=env.endpoint)

    assert opened == []
    assert env.issuer.refresh_requests == []


@pytest.mark.parametrize("mode", ["server_error", "refused"])
def test_mcp_outage_at_the_probe_keeps_the_record_and_starts_no_authorization(
    env: Environment, file_store: FileStore, lock_dir: Path, mode: str
) -> None:
    _first_login(env, file_store, lock_dir)
    key = RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)
    before = file_store.load(key)
    flow = _outage(env, mode, file_store, lock_dir)

    with pytest.raises(AuthUnavailable) as raised:
        flow.login(endpoint=env.endpoint)

    assert raised.value.code == "mcp_unavailable"
    assert raised.value.next_action == "retry_later"
    assert len(env.issuer.auth_requests) == 1  # no new authorization
    after = file_store.load(key)
    assert after is not None
    assert after.state == "ready"
    assert before is not None
    assert after.binding == before.binding


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["server_error", "refused"])
async def test_async_mcp_outage_at_the_probe_is_unavailable_not_a_crash(
    env: Environment, file_store: FileStore, lock_dir: Path, mode: str
) -> None:
    _first_login(env, file_store, lock_dir)
    flow = _outage(env, mode, file_store, lock_dir)

    with pytest.raises(AuthUnavailable) as raised:
        await flow.login_async(endpoint=env.endpoint)

    assert raised.value.code == "mcp_unavailable"
    assert len(env.issuer.auth_requests) == 1
