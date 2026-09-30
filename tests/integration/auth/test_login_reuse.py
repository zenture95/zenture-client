"""Scenario 8: explicit login reuses a stored authorization (D-35)."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
