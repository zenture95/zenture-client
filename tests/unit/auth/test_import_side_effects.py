"""Scenario 1: importing the package performs no I/O and touches no credential store."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

PROBE = """
import socket, sys, builtins
events = []
def record(name):
    def spy(*a, **k):
        events.append(name)
        raise AssertionError(name)
    return spy
socket.socket.connect = record("connect")
socket.socket.connect_ex = record("connect_ex")
socket.socket.bind = record("bind")
socket.create_connection = record("create_connection")
socket.getaddrinfo = record("getaddrinfo")
# Modules whose use would mean a browser launch, a credential-store access or network client.
for blocked in ("webbrowser", "keyring", "httpx2", "mcp"):
    sys.modules[blocked] = None
import zenture, zenture.auth, zenture.mcp, zenture.cli
assert events == [], events
assert zenture.auth.login and zenture.auth.login_async and zenture.auth.AuthSession
assert zenture.mcp.McpClient and zenture.mcp.AsyncMcpClient and zenture.cli.main
for name in ("AuthorizationRequired", "AuthUnavailable", "SecureStoreUnavailable", "LoginCancelled", "PermissionDenied"):
    assert issubclass(getattr(zenture.auth, name), Exception)
print("clean")
"""


def test_import_does_no_network_browser_keyring_or_lock_file_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home), "PATH": "/usr/bin:/bin"}

    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip() == "clean"
    assert list(home.rglob("*")) == []  # no lock directory, no cache, no credential files
