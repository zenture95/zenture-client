"""Scenario 5: the CLI and a Python MCP process share one stored record without reuse-revoke."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import RecordKey

if TYPE_CHECKING:
    from file_store import FileStore

WORKER = Path(__file__).with_name("client_worker.py")


@pytest.fixture
def ready(env: Environment, file_store: FileStore, lock_dir: Path) -> FileStore:
    opener, _ = browser_opener()
    LoginFlow(runtime_for(file_store, lock_dir, opener)).login(endpoint=env.endpoint)
    env.issuer.refresh_requests.clear()
    return file_store


def _spawn(
    mode: str, env: Environment, store: FileStore, lock_dir: Path, start_at: float
) -> subprocess.Popen[str]:
    config = {
        "mode": mode,
        "start_at": start_at,
        "endpoint": env.endpoint,
        "store": str(store.path),
        "lock_dir": str(lock_dir),
    }
    return subprocess.Popen(
        [sys.executable, str(WORKER), json.dumps(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=WORKER.parent,
    )


def test_cli_status_and_mcp_client_rotate_the_same_record_serially(
    env: Environment, ready: FileStore, lock_dir: Path
) -> None:
    env.issuer.refresh_delay = 0.4
    initial = ready.load(RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID))
    assert initial is not None
    start_at = time.time() + 5

    processes = [
        _spawn(mode, env, ready, lock_dir, start_at) for mode in ("cli", "mcp", "cli", "mcp")
    ]
    outcomes = [process.communicate(timeout=120)[0].strip() for process in processes]

    assert outcomes == ["exit:0", "ok", "exit:0", "ok"]
    assert len(env.issuer.refresh_requests) == 4  # one rotation per process refresh
    assert len(set(env.issuer.refresh_requests)) == 4  # no refresh token was presented twice
    assert env.issuer.refresh_requests[0] == initial.refresh_token
    assert env.issuer.max_concurrent_refreshes == 1
    assert env.issuer.reuse_events == 0
    assert not env.issuer.grant_revoked()
    final = ready.load(RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID))
    assert final is not None
    assert final.state == "ready"
    assert final.refresh_token not in env.issuer.refresh_requests  # the newest, unused token
