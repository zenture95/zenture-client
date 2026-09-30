"""Per-binding cross-process OS file lock; the lock file carries no secret."""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from zenture._auth.errors import AuthUnavailable
from zenture._auth.store import RecordKey

LOCK_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.05

LockFactory = Callable[[RecordKey], contextlib.AbstractContextManager[None]]


def default_lock_directory() -> Path:
    """User cache directory for lock files (never a credential location)."""

    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Caches"
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".cache"
    return base / "zenture" / "locks"


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


def binding_lock(
    identity: RecordKey,
    *,
    directory: Path | None = None,
    wait_seconds: float = LOCK_WAIT_SECONDS,
) -> contextlib.AbstractContextManager[None]:
    """Hold the exclusive lock of one record for the duration of a rotation."""

    chosen = directory or default_lock_directory()
    # The default layout is ``<cache>/zenture/locks``: both levels are private.
    return _held(identity, chosen, chosen.parent if directory is None else chosen, wait_seconds)


@contextlib.contextmanager
def _held(
    identity: RecordKey, directory: Path, private_root: Path, wait_seconds: float
) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform != "win32":
            private_root.chmod(0o700)
            directory.chmod(0o700)
        fd = os.open(directory / f"{identity.digest()}.lock", flags, 0o600)
    except OSError as exc:
        raise AuthUnavailable("lock_unavailable") from exc
    try:
        deadline = time.monotonic() + wait_seconds
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise AuthUnavailable("lock_timeout")
            time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
