"""Scenario 1: importing the package performs no I/O and touches no credential store."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from zenture import __version__

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
    if os.name == "nt":
        env.update({key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ})

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


def test_cli_version_exits_before_auth_without_side_effects(tmp_path: Path) -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    home.mkdir()
    probe = (
        PROBE.replace('print("clean")', "")
        + """
zenture.cli.login_module.default_runtime = record("runtime")
zenture.cli.LoginFlow = record("login")
zenture.cli.check_status = record("status")
zenture.cli.clear_stored = record("clear")
sys.exit(zenture.cli.main(["--version"]))
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": "",
            "PYTHONPATH": str(root / "src"),
            **(
                {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
                if os.name == "nt"
                else {}
            ),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"zenture {__version__}\n"
    assert result.stderr == ""
    assert list(home.rglob("*")) == []
