"""The built wheel and sdist install into a fresh environment and expose the CLI and peers."""

from __future__ import annotations

import os
import site
import subprocess
import sys
import tarfile
import venv
import zipfile
from email import message_from_bytes
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from email.message import Message

ROOT = Path(__file__).resolve().parents[2]

QUIET_IMPORT = """
import socket, sys, webbrowser
from importlib.metadata import entry_points
from pathlib import Path
def deny(*args, **kwargs):
    raise AssertionError("side effect during import")
socket.socket.connect = deny
socket.getaddrinfo = deny
webbrowser.open = deny
import keyring
keyring.get_keyring = deny
keyring.get_password = deny
import zenture, zenture.auth, zenture.mcp, zenture.cli
assert Path(zenture.__file__).is_relative_to(Path(sys.prefix))
(point,) = [e for e in entry_points(group="console_scripts") if e.name == "zenture"]
assert point.value == "zenture.cli:main"
"""


def _run(arguments: list[str], *, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "USERPROFILE": str(home), "PATH": os.environ.get("PATH", "")}
    return subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, check=False, env=env)


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    built = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out)],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert built.returncode == 0
    return out


def _metadata(dist: Path) -> Message:
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return message_from_bytes(archive.read(name))


def test_wheel_metadata_declares_name_dependencies_python_range_and_entry_point(
    dist: Path,
) -> None:
    metadata = _metadata(dist)
    assert metadata["Name"] == "zenture"
    assert metadata["Requires-Python"] == ">=3.11"
    classifiers = metadata.get_all("Classifier") or []
    for minor in ("11", "12", "13", "14"):
        assert f"Programming Language :: Python :: 3.{minor}" in classifiers
    required = [
        r.split(";")[0].replace(" ", "").lower() for r in metadata.get_all("Requires-Dist") or []
    ]
    for dependency in ("mcp", "keyring", "pyjwt", "httpx2"):
        assert any(r.startswith(dependency) for r in required), dependency
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        points = next(n for n in archive.namelist() if n.endswith("entry_points.txt"))
        assert "zenture = zenture.cli:main" in archive.read(points).decode()
    assert "zenture auth login" in metadata.get_payload()


def test_sdist_ships_docs_examples_and_agent_entry(dist: Path) -> None:
    (sdist,) = dist.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
    for required in (
        "AGENTS.md",
        "docs/authentication.md",
        "docs/mcp-client.md",
        "examples/mcp_async_login.py",
        "examples/mcp_sync_stored_login.py",
        "examples/mcp_device_login.py",
    ):
        assert any(n.endswith("/" + required) for n in names), required


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_installed_package_runs_the_cli_and_imports_without_side_effects(
    dist: Path, tmp_path: Path, artifact: str
) -> None:
    target = next(dist.glob("*.whl" if artifact == "wheel" else "*.tar.gz"))
    location = tmp_path / "venv"
    # Dependencies come from the locked local runtime; the package itself installs fresh.
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(location)
    bin_dir = location / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    packages = (
        next(location.glob("lib/python*/site-packages"), None) or location / "Lib" / "site-packages"
    )
    (packages / "dependency-runtime.pth").write_text("\n".join(site.getsitepackages()))
    home = tmp_path / "home"
    home.mkdir()
    install = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--ignore-installed",
            str(target),
        ],
        cwd=tmp_path,
        home=home,
    )
    assert install.returncode == 0

    before = sorted(home.rglob("*"))
    script = bin_dir / ("zenture.exe" if os.name == "nt" else "zenture")
    for words in ([], ["auth"], ["auth", "login"]):
        result = _run([str(script), *words, "--help"], cwd=tmp_path, home=home)
        assert result.returncode == 0
        assert "zenture" in result.stdout
    assert (
        "--session-only"
        in _run([str(script), "auth", "login", "--help"], cwd=tmp_path, home=home).stdout
    )

    quiet = _run([str(python), "-I", "-c", QUIET_IMPORT], cwd=tmp_path, home=home)
    assert quiet.returncode == 0, quiet.stderr[-400:]
    assert sorted(home.rglob("*")) == before  # no store, lock or config touched
