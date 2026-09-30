"""The lock directory is private and the lock file is never reached through a symlink."""

from __future__ import annotations

import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest

from zenture._auth import lock as lock_module
from zenture._auth.errors import AuthUnavailable
from zenture._auth.lock import binding_lock
from zenture._auth.store import RecordKey

if TYPE_CHECKING:
    from pathlib import Path

KEY = RecordKey("https://a.example", "https://r.example", "zenture-client")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and O_NOFOLLOW")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
def test_default_lock_directory_and_its_parent_are_made_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "zenture"
    locks = root / "locks"
    locks.mkdir(parents=True)
    root.chmod(0o755)
    locks.chmod(0o755)
    monkeypatch.setattr(lock_module, "default_lock_directory", lambda: locks)

    with binding_lock(KEY):
        pass

    assert _mode(root) == 0o700
    assert _mode(locks) == 0o700


@posix_only
def test_freshly_created_lock_directories_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = tmp_path / "cache" / "zenture" / "locks"
    monkeypatch.setattr(lock_module, "default_lock_directory", lambda: locks)
    previous = os.umask(0o022)
    try:
        with binding_lock(KEY):
            pass
    finally:
        os.umask(previous)

    assert _mode(locks) == 0o700
    assert _mode(locks.parent) == 0o700


@posix_only
def test_lock_file_symlink_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    directory = tmp_path / "locks"
    directory.mkdir(mode=0o700)
    target = tmp_path / "victim"
    target.write_text("keep")
    (directory / f"{KEY.digest()}.lock").symlink_to(target)

    with pytest.raises(AuthUnavailable) as caught, binding_lock(KEY, directory=directory):
        pass

    assert caught.value.code == "lock_unavailable"
    assert target.read_text() == "keep"
