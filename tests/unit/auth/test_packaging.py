"""Scenario 10: base dependencies and a built wheel that imports the auth package."""

from __future__ import annotations

import email
import site
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

IMPORT_CHECK = """
import sys
import zenture, zenture.auth
from pathlib import Path
assert Path(zenture.__file__).is_relative_to(Path(sys.prefix))
assert zenture.auth.login and zenture.auth.login_async
"""


def test_base_dependencies_carry_the_native_auth_stack() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    dependencies = set(project["dependencies"])

    assert {
        "httpx>=0.27,<1",
        "httpx2>=2.5,<3",
        "pydantic>=2.7,<3",
        "mcp>=2.0.0,<2.1",
        "keyring>=25.6,<26",
        "pyjwt[crypto]>=2.10,<3",
    } <= dependencies
    assert "Programming Language :: Python :: 3.14" in project["classifiers"]
    assert "\n.venv/\n" in "\n" + (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_built_wheel_declares_the_dependencies_and_imports_in_a_fresh_environment(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    (wheel,) = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        names = set(archive.namelist())
    required = metadata.get_all("Requires-Dist") or []
    assert any(r.startswith("mcp") and "2.0.0" in r and "extra" not in r for r in required)
    assert any(r.startswith("keyring") for r in required)
    assert any(r.lower().startswith("pyjwt") and "crypto" in r for r in required)
    assert "zenture/auth/__init__.py" in names
    assert any(n.startswith("zenture/_auth/") for n in names)

    location = tmp_path / "fresh"
    # Fresh interpreter environment; the locked local dependency runtime stands in for PyPI.
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(location)
    packages = next((location / "lib").glob("python*/site-packages"))
    (packages / "dependency-runtime.pth").write_text("\n".join(site.getsitepackages()))
    python = location / "bin" / "python"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--ignore-installed",
            str(wheel),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run([str(python), "-I", "-c", IMPORT_CHECK], cwd=tmp_path, check=True)
