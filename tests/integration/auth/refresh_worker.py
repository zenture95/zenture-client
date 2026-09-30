"""Child process for cross-process refresh tests: one rotation, prints its outcome."""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

from file_store import FileStore

from zenture._auth.discovery import discover
from zenture._auth.errors import AuthError
from zenture._auth.http import new_client
from zenture._auth.lock import binding_lock
from zenture._auth.model import Target
from zenture._auth.session import SessionCore


def main() -> None:
    config = json.loads(sys.argv[1])
    with new_client() as client:
        discovery = discover(client, Target.from_endpoint(config["endpoint"]))
    directory = Path(config["lock_dir"])

    def lock(identity):  # type: ignore[no-untyped-def]
        if config.get("no_lock"):
            return contextlib.nullcontext()
        return binding_lock(identity, directory=directory, wait_seconds=60)

    core = SessionCore(discovery=discovery, store=FileStore(Path(config["store"])), lock=lock)
    while time.time() < config.get("start_at", 0):
        time.sleep(0.001)
    try:
        core.refresh()
    except AuthError as exc:
        print(type(exc).__name__, flush=True)
    else:
        print("ok", flush=True)


if __name__ == "__main__":
    main()
