"""A session whose stored authority was replaced by a different binding never refreshes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthorizationRequired
from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import RecordKey

if TYPE_CHECKING:
    from pathlib import Path

    from file_store import FileStore


def test_older_session_is_refused_before_any_refresh_when_another_login_replaced_the_record(
    env: Environment, file_store: FileStore, lock_dir: Path
) -> None:
    opener, _ = browser_opener()
    flow = LoginFlow(runtime_for(file_store, lock_dir, opener))
    identity = RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)
    older = flow.login(endpoint=env.endpoint)
    file_store.delete(identity)  # a different user logs in into the same store
    env.issuer.next_connection_ids = ["conn-2"]
    newer = flow.login(endpoint=env.endpoint)
    newest_record = file_store.load(identity)
    assert newer.binding.connection_id == "conn-2"
    older._core.clock = lambda: older._core._expires_at + 1  # pyright: ignore[reportPrivateUsage]  # force a refresh attempt

    with pytest.raises(AuthorizationRequired) as caught:
        older._core.access_token()  # pyright: ignore[reportPrivateUsage]  # white-box test of internals

    assert caught.value.code == "binding_changed"
    assert env.issuer.refresh_requests == []
    assert file_store.load(identity) == newest_record
