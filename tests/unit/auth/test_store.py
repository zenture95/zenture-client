"""Scenario 5: explicit native backend selection and the bounded, token-free record."""

from __future__ import annotations

import importlib
import json
import sys
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from zenture._auth.errors import SecureStoreUnavailable
from zenture._auth.model import Binding
from zenture._auth.store import (
    MAX_RECORD_BYTES,
    KeyringStore,
    MemoryStore,
    RecordKey,
    StoredRecord,
    StoreError,
    native_backend,
)

KEY = RecordKey("https://mcp-auth.zenture.app", "https://mcp.zenture.app", "zenture-client")
BINDING = Binding("sub-1", "8d1f3a4c-0000-4000-8000-000000000001", 12, 345)


def _record(refresh: str = "rt_" + "a" * 43, **kw: Any) -> StoredRecord:
    return StoredRecord(KEY, BINDING, refresh, **kw)


class FakeBackend:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


@pytest.mark.parametrize(
    ("platform", "module", "name"),
    [
        ("darwin", "keyring.backends.macOS", "Keyring"),
        ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
        ("linux", "keyring.backends.SecretService", "Keyring"),
    ],
)
def test_native_backend_is_chosen_explicitly_per_os_never_by_keyring_selection(
    monkeypatch: pytest.MonkeyPatch, platform: str, module: str, name: str
) -> None:
    import keyring.core

    requested: list[str] = []

    class Backend:
        viable = True

    def fake_import(requested_name: str) -> object:
        requested.append(requested_name)
        return SimpleNamespace(**{name: Backend})

    def forbidden(*_a: object, **_k: object) -> object:
        raise AssertionError("global keyring selection must never run")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setattr(keyring.core, "get_keyring", forbidden)
    monkeypatch.setattr(keyring.core, "init_backend", forbidden)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")

    chosen = native_backend(platform)

    assert isinstance(chosen, Backend)
    assert requested == [module]


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_non_viable_backend_suggests_session_only(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    class Dead:
        viable = False

    monkeypatch.setattr(
        importlib, "import_module", lambda _n: SimpleNamespace(Keyring=Dead, WinVaultKeyring=Dead)
    )

    with pytest.raises(SecureStoreUnavailable) as caught:
        native_backend(platform)

    assert caught.value.next_action == "use_session_only"


def test_unsupported_platform_and_broken_import_are_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SecureStoreUnavailable):
        native_backend("plan9")

    def broken(_n: str) -> object:
        raise ImportError

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(SecureStoreUnavailable):
        native_backend("linux")


def test_keyring_store_keeps_one_item_per_issuer_resource_client() -> None:
    backend = FakeBackend()
    store = KeyringStore(backend)
    other = RecordKey(KEY.issuer, "https://mcp-int.zenture.app", KEY.client_id)

    store.save(_record())
    store.save(_record())  # rewriting the same identity replaces the item
    store.save(StoredRecord(other, BINDING, "rt_other"))

    assert len(backend.items) == 2
    assert store.load(KEY) == _record()
    assert store.load(other) is not None
    store.delete(KEY)
    assert store.load(KEY) is None
    assert len(backend.items) == 1
    store.delete(KEY)  # deleting an absent item is not an error


def test_record_has_no_access_token_and_stays_within_one_kibibyte() -> None:
    raw = _record().to_json()
    data = json.loads(raw)

    assert len(raw.encode()) <= MAX_RECORD_BYTES
    assert not [key for key in data if "access" in key and key != "authorization_generation"]
    assert data["state"] == "ready"
    assert {"v", "issuer", "resource", "client_id", "refresh_token", "rotation_id"} <= set(data)


@pytest.mark.parametrize("store", [KeyringStore(FakeBackend()), MemoryStore()])
def test_record_over_the_size_limit_is_refused_at_write(store: Any) -> None:
    with pytest.raises(StoreError):
        store.save(_record("r" * 2000))


def test_foreign_or_corrupt_items_read_as_absent() -> None:
    backend = FakeBackend()
    store = KeyringStore(backend)
    store.save(_record())
    (item,) = backend.items
    backend.items[item] = backend.items[item].replace(
        "https://mcp.zenture.app", "https://x.example"
    )
    assert store.load(KEY) is None
    backend.items[item] = "{not json"
    assert store.load(KEY) is None
    backend.items[item] = json.dumps({"v": 99})
    assert store.load(KEY) is None


def test_backend_errors_surface_as_store_errors() -> None:
    class Failing(FakeBackend):
        def set_password(self, service: str, username: str, password: str) -> None:
            raise RuntimeError("locked")

        def get_password(self, service: str, username: str) -> str | None:
            raise RuntimeError("locked")

    store = KeyringStore(Failing())
    with pytest.raises(StoreError):
        store.save(_record())
    with pytest.raises(StoreError):
        store.load(KEY)


@pytest.mark.native_keyring
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain proof only")
def test_real_macos_keychain_roundtrip_with_the_explicit_backend() -> None:
    backend = native_backend()
    assert type(backend).__module__ == "keyring.backends.macOS"
    identity = RecordKey(f"https://test-{uuid.uuid4().hex}.invalid", KEY.resource, KEY.client_id)
    store = KeyringStore(backend)
    record = StoredRecord(identity, BINDING, "rt_" + "b" * 43)
    try:
        store.save(record)
        assert store.load(identity) == record
        store.save(record.with_state("rotating", "r1"))
        assert store.load(identity) == record.with_state("rotating", "r1")
    finally:
        store.delete(identity)
    assert store.load(identity) is None
