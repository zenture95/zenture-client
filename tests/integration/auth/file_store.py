"""Cross-process record store double: same interface as the native store."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from zenture._auth.store import RecordKey, StoredRecord, StoreError

if TYPE_CHECKING:
    from pathlib import Path


class FileStore:
    """JSON file store; ``fail_attempts`` holds absolute save-attempt numbers that fail."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.attempts = 0
        self.fail_attempts: set[int] = set()

    def fail_next(self, count: int = 1) -> None:
        self.fail_attempts.update(range(self.attempts + 1, self.attempts + 1 + count))

    def fail_attempt_after(self, offset: int) -> None:
        self.fail_attempts.add(self.attempts + offset)

    def load(self, identity: RecordKey) -> StoredRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return StoredRecord.from_json(raw, identity)

    def save(self, record: StoredRecord) -> None:
        self.attempts += 1
        if self.attempts in self.fail_attempts:
            raise StoreError
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(record.to_json(), encoding="utf-8")
        temporary.replace(self.path)

    def delete(self, identity: RecordKey) -> None:
        self.path.unlink(missing_ok=True)
