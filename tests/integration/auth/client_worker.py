"""Child process: one ``zenture auth status`` or one ``McpClient`` session on the stored record."""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

from auth_harness import runtime_for
from file_store import FileStore

from zenture._auth import login as login_module
from zenture._auth.errors import AuthError
from zenture.cli import main
from zenture.mcp import McpClient


def _wait(start_at: float) -> None:
    while time.time() < start_at:
        time.sleep(0.001)


def main_worker() -> None:
    config = json.loads(sys.argv[1])
    runtime = runtime_for(
        FileStore(Path(config["store"])),
        Path(config["lock_dir"]),
        lambda _url: False,
        lock_wait_seconds=60,
    )
    login_module.default_runtime = lambda: runtime
    _wait(config["start_at"])
    if config["mode"] == "cli":
        code = main(["auth", "status"], endpoint=config["endpoint"], stdout=io.StringIO())
        print(f"exit:{code}", flush=True)
        return
    try:
        with McpClient.connect(config["endpoint"]) as client:
            client.list_tools()
    except AuthError as exc:
        print(type(exc).__name__, flush=True)
    else:
        print("ok", flush=True)


if __name__ == "__main__":
    main_worker()
