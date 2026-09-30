from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from auth_harness import Environment
from file_store import FileStore

from zenture._auth.store import MemoryStore, RecordStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def env() -> Iterator[Environment]:
    environment = Environment().start()
    try:
        yield environment
    finally:
        environment.stop()


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "locks"


@pytest.fixture
def store() -> RecordStore:
    return MemoryStore()


@pytest.fixture
def file_store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "record.json")
