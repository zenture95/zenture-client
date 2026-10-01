"""Exercise distribution metadata and API calls from built, installed artifacts."""

from __future__ import annotations

import os
import site
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

INSTALLED_BEHAVIOR = """
import asyncio
from importlib.metadata import metadata, version
from pathlib import Path
import sys
import httpx
import zenture
from uuid import UUID
from zenture import ZentureClient, AsyncZentureClient
assert Path(zenture.__file__).is_relative_to(Path(sys.prefix))
assert metadata("zenture")["Name"] == "zenture"
assert version("zenture") == zenture.__version__
assert "Repository, https://github.com/zenture95/zenture-client" in metadata("zenture").get_all("Project-URL")
assert zenture.__all__ == ("AsyncZentureClient", "ZentureClient", "__version__")
assert not hasattr(zenture, "Zenture") and not hasattr(zenture, "AsyncZenture")
assert ZentureClient.__name__ == "ZentureClient"
assert AsyncZentureClient.__name__ == "AsyncZentureClient"
API_KEY = f"zt_live_{UUID(int=0).hex}"
def reply(request):
    assert request.url.path == "/v1/helloworld"
    assert request.headers["user-agent"] == "zenture-python/" + zenture.__version__
    assert "authorization" not in request.headers
    return httpx.Response(200, text="package boundary")
with ZentureClient(api_key=API_KEY, http_client=httpx.Client(transport=httpx.MockTransport(reply))) as client:
    assert client.helloworld() == "package boundary"
async def main():
    async with AsyncZentureClient(api_key=API_KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply))) as client:
        assert await client.helloworld() == "package boundary"
asyncio.run(main())
"""


def _run(arguments: list[str], *, cwd: Path) -> None:
    result = subprocess.run(arguments, cwd=cwd, capture_output=True, check=False)
    assert result.returncode == 0, "Package build/install/behavior check failed"


def test_built_wheel_and_sdist_install_canonical_client(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist)],
        cwd=ROOT,
    )
    wheel = list(dist.glob("*.whl"))
    sdist = list(dist.glob("*.tar.gz"))
    assert len(wheel) == len(sdist) == 1
    assert wheel[0].name.startswith("zenture-")
    assert sdist[0].name.startswith("zenture-")
    _run([sys.executable, "scripts/check_package_artifacts.py", str(dist)], cwd=ROOT)
    _run([sys.executable, "scripts/check_release_safety.py"], cwd=ROOT)
    for index, artifact in enumerate([wheel[0], sdist[0]]):
        location = tmp_path / f"installed-{index}"
        # Reuse the locked local dependency runtime without network access;
        # package installation itself is fresh and must resolve from this venv.
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(location)
        packages = (
            next(location.glob("lib/python*/site-packages"), None)
            or location / "Lib" / "site-packages"
        )
        (packages / "dependency-runtime.pth").write_text("\n".join(site.getsitepackages()))
        bin_dir = location / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-build-isolation",
                "--ignore-installed",
                str(artifact),
            ],
            cwd=tmp_path,
        )
        _run([str(python), "-I", "-c", INSTALLED_BEHAVIOR], cwd=tmp_path)
