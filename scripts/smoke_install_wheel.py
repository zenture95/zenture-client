"""Install the built wheel into a fresh venv and import the public SDK surface."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def _one_wheel(dist_dir: Path) -> Path:
    matches = sorted(dist_dir.glob("*.whl"))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one wheel artifact, found {len(matches)}.")
    return matches[0]


def _venv_python(venv_dir: Path) -> Path:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return venv_dir / bin_dir / "python"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()

    wheel = _one_wheel(args.dist_dir)
    with tempfile.TemporaryDirectory() as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = _venv_python(venv_dir)

        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheel)],
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "from importlib.metadata import metadata, version; "
                    "import zenture; "
                    "from zenture import AsyncZentureClient, ZentureClient; "
                    "assert version('zenture') == zenture.__version__; "
                    "assert metadata('zenture')['Name'] == 'zenture'; "
                    "assert not hasattr(zenture, 'Zenture'); "
                    "assert not hasattr(zenture, 'AsyncZenture'); "
                    "assert 'Repository, https://github.com/zenture95/zenture-client' "
                    "in metadata('zenture').get_all('Project-URL'); "
                    "assert ZentureClient.__name__ == 'ZentureClient'; "
                    "assert AsyncZentureClient.__name__ == 'AsyncZentureClient'"
                ),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
