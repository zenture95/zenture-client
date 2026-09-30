"""Protected credential records: explicit native backend per OS, or memory only."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import threading
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from zenture._auth.errors import SecureStoreUnavailable
from zenture._auth.model import Binding

SERVICE_NAME = "zenture-client"
RECORD_VERSION = 1
MAX_RECORD_BYTES = 1024

RecordState = Literal["ready", "rotating"]

# Explicit per-OS backend modules; never ``keyring.get_keyring()``, plugins or env selection.
_NATIVE_BACKENDS: dict[str, tuple[str, str]] = {
    "darwin": ("keyring.backends.macOS", "Keyring"),
    "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
    "linux": ("keyring.backends.SecretService", "Keyring"),
}


class StoreError(Exception):
    """A record could not be read, written or deleted (no detail kept)."""


@dataclass(frozen=True, slots=True)
class RecordKey:
    """One record per issuer + resource + client (D-33)."""

    issuer: str
    resource: str
    client_id: str

    @classmethod
    def local(cls, resource: str, client_id: str) -> RecordKey:
        """Key for operations that need no issuer (lock, delete); never verifies a record."""

        return cls("", resource, client_id)

    def digest(self) -> str:
        """Item name: resource and client only, so local removal needs no discovery.

        The issuer stays inside the record and is verified on every load.
        """

        material = "\n".join((self.resource, self.client_id)).encode()
        return hashlib.sha256(material).hexdigest()[:40]


@dataclass(frozen=True, slots=True, repr=False)
class StoredRecord:
    """Persisted refresh authority. Access tokens are never part of it."""

    identity: RecordKey
    binding: Binding
    refresh_token: str
    state: RecordState = "ready"
    rotation_id: str = ""

    def __repr__(self) -> str:
        return f"StoredRecord(state={self.state!r}, binding={self.binding!r})"

    def with_state(self, state: RecordState, rotation_id: str = "") -> StoredRecord:
        return replace(self, state=state, rotation_id=rotation_id)

    def to_json(self) -> str:
        encoded = json.dumps(
            {
                "v": RECORD_VERSION,
                "issuer": self.identity.issuer,
                "resource": self.identity.resource,
                "client_id": self.identity.client_id,
                "sub": self.binding.sub,
                "connection_id": self.binding.connection_id,
                "owner_epoch": self.binding.owner_epoch,
                "authorization_generation": self.binding.authorization_generation,
                "refresh_token": self.refresh_token,
                "state": self.state,
                "rotation_id": self.rotation_id,
            },
            separators=(",", ":"),
        )
        if len(encoded.encode()) > MAX_RECORD_BYTES:
            raise StoreError
        return encoded

    @classmethod
    def from_json(cls, raw: str, identity: RecordKey) -> StoredRecord | None:
        """Parse a stored record; anything malformed or foreign reads as absent."""

        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("v") != RECORD_VERSION:
                return None
            claimed = (data["issuer"], data["resource"], data["client_id"])
            if claimed != (identity.issuer, identity.resource, identity.client_id):
                return None
            state = data["state"]
            values = (
                data["sub"],
                data["connection_id"],
                data["refresh_token"],
                data["rotation_id"],
            )
            epoch = data["owner_epoch"]
            generation = data["authorization_generation"]
        except (ValueError, KeyError):
            return None
        if state not in ("ready", "rotating") or not all(isinstance(v, str) for v in values):
            return None
        if type(epoch) is not int or type(generation) is not int:
            return None
        sub, connection_id, refresh_token, rotation_id = values
        return cls(
            identity=identity,
            binding=Binding(sub, connection_id, epoch, generation),
            refresh_token=refresh_token,
            state=state,
            rotation_id=rotation_id,
        )


class RecordStore(Protocol):
    """Persistence port; implementations raise :class:`StoreError` on failure."""

    def load(self, identity: RecordKey) -> StoredRecord | None: ...

    def save(self, record: StoredRecord) -> None: ...

    def delete(self, identity: RecordKey) -> bool:
        """Remove the record; return whether one existed."""
        ...


class MemoryStore:
    """Process-local store for session-only login; nothing is persisted."""

    def __init__(self) -> None:
        self._records: dict[RecordKey, StoredRecord] = {}
        self._guard = threading.Lock()

    def load(self, identity: RecordKey) -> StoredRecord | None:
        with self._guard:
            return self._records.get(identity)

    def save(self, record: StoredRecord) -> None:
        record.to_json()  # same size contract as the native store
        with self._guard:
            self._records[record.identity] = record

    def delete(self, identity: RecordKey) -> bool:
        with self._guard:
            doomed = [key for key in self._records if key.digest() == identity.digest()]
            for key in doomed:
                del self._records[key]
            return bool(doomed)


class KeyringStore:
    """One protected item per record identity in an explicitly selected native backend."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def load(self, identity: RecordKey) -> StoredRecord | None:
        try:
            raw = self._backend.get_password(SERVICE_NAME, identity.digest())
        except Exception as exc:
            raise StoreError from exc
        if raw is None:
            return None
        return StoredRecord.from_json(str(raw), identity)

    def save(self, record: StoredRecord) -> None:
        payload = record.to_json()
        try:
            self._backend.set_password(SERVICE_NAME, record.identity.digest(), payload)
        except Exception as exc:
            raise StoreError from exc

    def delete(self, identity: RecordKey) -> bool:
        name = identity.digest()
        try:
            existed = self._backend.get_password(SERVICE_NAME, name) is not None
        except Exception as exc:
            raise StoreError from exc
        if not existed:
            return False
        try:
            self._backend.delete_password(SERVICE_NAME, name)
        except Exception as exc:
            # An item that vanished in between is the desired end state.
            try:
                gone = self._backend.get_password(SERVICE_NAME, name) is None
            except Exception:
                gone = False
            if not gone:
                raise StoreError from exc
        return True


def native_backend(platform: str | None = None) -> Any:
    """Return the viable native keyring backend of this OS or raise."""

    target = sys.platform if platform is None else platform
    selected = _NATIVE_BACKENDS.get(target)
    if selected is None:
        raise SecureStoreUnavailable
    module_name, class_name = selected
    try:
        backend_class = getattr(importlib.import_module(module_name), class_name)
        if not backend_class.viable:
            raise SecureStoreUnavailable
        return backend_class()
    except SecureStoreUnavailable:
        raise
    except Exception as exc:
        raise SecureStoreUnavailable from exc


def native_store() -> RecordStore:
    return KeyringStore(native_backend())
