"""Scenario 9: no credential, code or state in reprs, errors, logs or output."""

from __future__ import annotations

import logging
import pickle
import urllib.parse
from typing import TYPE_CHECKING

import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.errors import AuthorizationRequired
from zenture._auth.login import LoginFlow
from zenture._auth.store import MemoryStore

if TYPE_CHECKING:
    from pathlib import Path


def test_reprs_errors_logs_and_output_never_contain_secrets(
    env: Environment,
    lock_dir: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    opener, opened = browser_opener()
    caplog.set_level(logging.DEBUG, logger="zenture")

    session = LoginFlow(runtime_for(store, lock_dir, opener)).login(endpoint=env.endpoint)
    core = session._core
    identity = core.identity
    record = store.load(identity)
    assert record is not None
    assert core._access is not None
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    secrets_seen = {
        core._access,
        record.refresh_token,
        query["state"],
        query["code_challenge"],
        env.issuer.token_requests[0]["code"],
        env.issuer.token_requests[0]["code_verifier"],
    }

    failure = AuthorizationRequired("rotation_outcome_unknown")
    visible = "\n".join(
        [
            repr(session),
            str(session),
            repr(core),
            repr(record),
            str(failure),
            repr(failure),
            caplog.text,
            capsys.readouterr().out,
            capsys.readouterr().err,
        ]
    )

    for secret in secrets_seen:
        assert secret not in visible
    assert "user-1" in repr(session)
    assert "conn-1" in repr(session)
    with pytest.raises(TypeError):
        pickle.dumps(session)
