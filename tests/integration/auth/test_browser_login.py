"""Scenarios 3, 4 and 5: loopback browser authorization, redemption, and persistence."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import select
import socket
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from auth_harness import Environment, browser_opener, runtime_for

from zenture._auth.browser import bind_loopback, wait_for_callback
from zenture._auth.errors import (
    AuthorizationRequired,
    AuthUnavailable,
    LoginCancelled,
    SecureStoreUnavailable,
)
from zenture._auth.login import LoginFlow
from zenture._auth.model import CLIENT_ID
from zenture._auth.store import MemoryStore, RecordKey, RecordStore, StoreError

if TYPE_CHECKING:
    from pathlib import Path


def _login(env: Environment, store: RecordStore, lock_dir: Path, opener: Any, **kw: Any) -> Any:
    flow = LoginFlow(runtime_for(store, lock_dir, opener, **kw))
    return flow.login(endpoint=env.endpoint)


def _key(env: Environment) -> RecordKey:
    return RecordKey(env.issuer.issuer, env.mcp.resource, CLIENT_ID)


def test_authorization_request_carries_pkce_state_resource_and_loopback_redirect(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    opener, opened = browser_opener()

    session = _login(env, store, lock_dir, opener)

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    assert query["client_id"] == "zenture-client"
    assert query["response_type"] == "code"
    assert query["scope"] == "openid mcp:run offline_access"
    assert query["resource"] == env.mcp.resource
    assert query["code_challenge_method"] == "S256"
    assert len(base64.urlsafe_b64decode(query["state"] + "=" * (-len(query["state"]) % 4))) == 32
    assert query["redirect_uri"].startswith("http://127.0.0.1:")
    assert query["redirect_uri"].endswith("/oauth/callback")
    # Redemption proves verifier/challenge pairing, code, redirect, client and resource.
    redeem = env.issuer.token_requests[0]
    assert redeem["grant_type"] == "authorization_code"
    digest = hashlib.sha256(redeem["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    assert redeem["redirect_uri"] == query["redirect_uri"]
    assert redeem["client_id"] == CLIENT_ID
    assert redeem["resource"] == env.mcp.resource
    assert session.binding.sub == "user-1"
    assert session.binding.connection_id == "conn-1"
    assert session.binding.owner_epoch == 3
    assert session.binding.authorization_generation == 7


def test_listener_is_bound_before_the_browser_opens_and_only_on_loopback(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    observed: dict[str, Any] = {}

    def opener(url: str) -> object:
        redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["redirect_uri"]
        port = urllib.parse.urlsplit(redirect).port
        assert port is not None
        with socket.create_connection(("127.0.0.1", port), timeout=2) as probe:
            observed["connected_before_browser"] = True
            observed["local"] = probe.getpeername()[0]
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _login(env, store, lock_dir, opener)

    assert observed == {"connected_before_browser": True, "local": "127.0.0.1"}


def test_listener_is_closed_after_success_error_timeout_and_interrupt() -> None:
    def closed(listener: socket.socket) -> bool:
        return listener.fileno() == -1

    listener, redirect = bind_loopback()
    with pytest.raises(AuthUnavailable, match="login_timed_out"):
        wait_for_callback(
            listener, redirect_uri=redirect, state="s" * 43, verifier="v", issuer="i", timeout=0.3
        )
    assert closed(listener)

    listener, redirect = bind_loopback()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(LoginCancelled):
        wait_for_callback(
            listener,
            redirect_uri=redirect,
            state="s" * 43,
            verifier="v",
            issuer="i",
            cancel=cancel,
        )
    assert closed(listener)


def test_keyboard_interrupt_while_waiting_closes_the_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    listener, redirect = bind_loopback()

    def interrupted(*_args: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(select, "select", interrupted)
    with pytest.raises(KeyboardInterrupt):
        wait_for_callback(listener, redirect_uri=redirect, state="s" * 43, verifier="v", issuer="i")
    assert listener.fileno() == -1


def test_listener_binds_ipv6_loopback_when_ipv4_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    real_socket = socket.socket

    class V4Refused(real_socket):  # type: ignore[valid-type,misc]
        def bind(self, address: Any) -> None:
            if self.family == socket.AF_INET:
                raise OSError("no ipv4")
            super().bind(address)

    monkeypatch.setattr(socket, "socket", V4Refused)
    try:
        listener, redirect = bind_loopback()
    except AuthUnavailable:
        pytest.skip("IPv6 loopback unavailable on this host")
    try:
        assert redirect.startswith("http://[::1]:")
        assert listener.getsockname()[0] == "::1"
    finally:
        listener.close()


def test_unsolicited_and_replayed_requests_are_ignored_until_the_exact_callback(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    results: dict[str, Any] = {}

    def opener(url: str) -> object:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        redirect = query["redirect_uri"]
        base = redirect.removesuffix("/oauth/callback")

        def drive() -> None:
            with httpx.Client(timeout=5) as client:
                results["wrong_path"] = client.get(f"{base}/other?state={query['state']}")
                results["wrong_state"] = client.get(f"{redirect}?state=x&code=c&iss=i")
                results["no_state"] = client.get(f"{redirect}?code=c")
                results["post"] = client.post(redirect, data={"state": query["state"]})
                results["bad_host"] = client.get(
                    redirect, headers={"Host": "evil.example"}, params={"state": query["state"]}
                )
                location = httpx.get(url, follow_redirects=False).headers["location"]
                results["good"] = client.get(location)
                try:
                    results["replay"] = client.get(location)
                except httpx.HTTPError as exc:
                    results["replay"] = exc

        threading.Thread(target=drive, daemon=True).start()
        return True

    session = _login(env, store, lock_dir, opener)

    for name in ("wrong_path", "wrong_state", "no_state", "post", "bad_host"):
        assert results[name].status_code == 400, name
    deadline = time.monotonic() + 5
    while "replay" not in results and time.monotonic() < deadline:
        time.sleep(0.02)
    assert results["good"].status_code == 200
    assert isinstance(results["replay"], httpx.HTTPError)  # listener already closed
    assert session.binding.sub == "user-1"
    # only one code was ever redeemed
    assert (
        len([r for r in env.issuer.token_requests if r["grant_type"] == "authorization_code"]) == 1
    )


@pytest.mark.parametrize("variant", ["omit_iss", "wrong_iss"])
def test_missing_or_wrong_iss_is_rejected_before_redemption(
    env: Environment, store: RecordStore, lock_dir: Path, variant: str
) -> None:
    if variant == "omit_iss":
        env.issuer.omit_iss = True
    else:
        env.issuer.response_iss = "http://127.0.0.1:1"
    opener, opened = browser_opener()

    with pytest.raises(AuthUnavailable) as caught:
        _login(env, store, lock_dir, opener)

    assert caught.value.code == "authorization_response_invalid"
    assert env.issuer.token_requests == []
    redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["redirect_uri"]
    with pytest.raises(OSError):  # noqa: PT011 - any refusal proves the listener is closed
        socket.create_connection(
            ("127.0.0.1", urllib.parse.urlsplit(redirect).port or 0), timeout=2
        )
    assert store.load(_key(env)) is None


def test_access_denied_is_a_cancelled_login(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    env.issuer.deny = True
    opener, _ = browser_opener()

    with pytest.raises(LoginCancelled):
        _login(env, store, lock_dir, opener)

    assert env.issuer.token_requests == []


def test_redemption_without_refresh_token_requires_authorization(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    env.issuer.omit_refresh_token = True
    opener, _ = browser_opener()

    with pytest.raises(AuthorizationRequired):
        _login(env, store, lock_dir, opener)

    assert store.load(_key(env)) is None


@pytest.mark.parametrize(
    "tamper",
    ["audience", "client"],
)
def test_access_token_with_foreign_audience_or_client_is_not_accepted(
    env: Environment, store: RecordStore, lock_dir: Path, tamper: str
) -> None:
    if tamper == "audience":
        env.issuer.access_audience = "http://127.0.0.1:9"
    else:
        env.issuer.access_client_id = "someone-else"
    opener, _ = browser_opener()

    with pytest.raises(AuthUnavailable) as caught:
        _login(env, store, lock_dir, opener)

    assert caught.value.code == "token_response_rejected"
    assert store.load(_key(env)) is None


def test_access_token_signed_by_an_unknown_key_is_not_accepted(
    env: Environment, store: RecordStore, lock_dir: Path
) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    env.issuer.jwks_key = ec.generate_private_key(ec.SECP256R1())
    opener, _ = browser_opener()

    with pytest.raises(AuthUnavailable) as caught:
        _login(env, store, lock_dir, opener)

    assert caught.value.code == "token_response_rejected"


def test_successful_login_persists_one_bounded_record_without_the_access_token(
    env: Environment, lock_dir: Path
) -> None:
    saved: list[str] = []

    class Spy(MemoryStore):
        def save(self, record: Any) -> None:
            saved.append(record.to_json())
            super().save(record)

    store = Spy()
    opener, _ = browser_opener()

    session = _login(env, store, lock_dir, opener)

    assert len(saved) == 1
    raw = saved[0]
    record = json.loads(raw)
    assert set(record) == {
        "v",
        "issuer",
        "resource",
        "client_id",
        "sub",
        "connection_id",
        "owner_epoch",
        "authorization_generation",
        "refresh_token",
        "state",
        "rotation_id",
    }
    assert record["state"] == "ready"
    assert len(raw.encode()) <= 1024
    access = session._core._access
    assert access is not None
    assert access not in raw
    assert "access_token" not in record


def test_store_write_failure_after_login_is_a_store_error_not_a_silent_success(
    env: Environment, lock_dir: Path
) -> None:
    class Broken(MemoryStore):
        def save(self, record: Any) -> None:
            raise StoreError

    opener, _ = browser_opener()

    with pytest.raises(SecureStoreUnavailable):
        _login(env, Broken(), lock_dir, opener)


def test_session_only_keeps_everything_in_memory_and_never_builds_a_native_store(
    env: Environment, lock_dir: Path
) -> None:
    opener, _ = browser_opener()

    def forbidden() -> RecordStore:
        raise AssertionError("native store must not be built for session-only")

    flow = LoginFlow(runtime_for(MemoryStore(), lock_dir, opener, native_store=forbidden))
    session = flow.login(session_only=True, endpoint=env.endpoint)

    assert session.binding.connection_id == "conn-1"
    assert not lock_dir.exists()


def test_native_store_unavailable_fails_before_the_browser_opens(
    env: Environment, lock_dir: Path
) -> None:
    opened: list[str] = []

    def unavailable() -> RecordStore:
        raise SecureStoreUnavailable

    flow = LoginFlow(
        runtime_for(
            MemoryStore(), lock_dir, lambda url: opened.append(url), native_store=unavailable
        )
    )
    with pytest.raises(SecureStoreUnavailable) as caught:
        flow.login(endpoint=env.endpoint)

    assert opened == []
    assert caught.value.next_action == "use_session_only"


def _callback_response(redirect: str, port_query: str) -> str:
    target = urllib.parse.urlsplit(redirect)
    with socket.create_connection((target.hostname or "", target.port or 0), timeout=5) as conn:
        conn.sendall(
            f"GET {target.path}?{port_query} HTTP/1.1\r\nHost: {target.netloc}\r\n\r\n".encode()
        )
        received = b""
        while chunk := conn.recv(4096):
            received += chunk
    return received.decode()


def test_callback_page_before_redemption_makes_no_success_claim() -> None:
    listener, redirect = bind_loopback()
    state = "s" * 43
    pages: list[str] = []
    query = urllib.parse.urlencode({"state": state, "iss": "https://i", "code": "c"})
    sender = threading.Thread(target=lambda: pages.append(_callback_response(redirect, query)))
    sender.start()

    wait_for_callback(
        listener, redirect_uri=redirect, state=state, verifier="v", issuer="https://i", timeout=5
    )
    sender.join(5)

    assert "Authorization received. Return to your terminal." in pages[0]
    assert "connected" not in pages[0].lower()


def test_stalled_local_connection_does_not_delay_the_real_callback() -> None:
    listener, redirect = bind_loopback()
    state = "s" * 43
    port = urllib.parse.urlsplit(redirect).port
    assert port is not None
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)  # sends nothing
    query = urllib.parse.urlencode({"state": state, "iss": "https://i", "code": "c"})
    sender = threading.Thread(target=lambda: _callback_response(redirect, query))
    sender.start()
    try:
        started = time.monotonic()
        result = wait_for_callback(
            listener,
            redirect_uri=redirect,
            state=state,
            verifier="v",
            issuer="https://i",
            timeout=1.5,
        )
        elapsed = time.monotonic() - started
    finally:
        stalled.close()
        sender.join(5)

    assert result.code == "c"
    assert elapsed < 1.5


class _RecordingSocket:
    """Socket double that records the order of option and bind calls."""

    calls: list[tuple[Any, ...]] = []  # noqa: RUF012

    def __init__(self, family: int, kind: int) -> None:
        pass

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        self.calls.append(("bind", address))

    def listen(self, backlog: int) -> None:
        pass

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 4242)

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("platform", "exclusive"), [("win32", True), ("linux", False), ("darwin", False)]
)
def test_the_callback_port_is_bound_exclusively_on_windows_only(
    monkeypatch: pytest.MonkeyPatch, platform: str, exclusive: bool
) -> None:
    from types import SimpleNamespace

    from zenture._auth import browser

    _RecordingSocket.calls = []
    monkeypatch.setattr(browser, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", 0x7FFFFFFC, raising=False)
    monkeypatch.setattr(socket, "socket", _RecordingSocket)

    bind_loopback()

    calls = _RecordingSocket.calls
    bind_index = next(i for i, call in enumerate(calls) if call[0] == "bind")
    options = [call for call in calls[:bind_index] if call[0] == "setsockopt"]
    if exclusive:
        assert options == [("setsockopt", socket.SOL_SOCKET, 0x7FFFFFFC, 1)]
    else:
        assert calls[0][0] == "bind"
        assert [call for call in calls if call[0] == "setsockopt"] == []


def test_stray_request_with_unread_body_still_gets_its_response_and_the_callback_survives() -> None:
    listener, redirect = bind_loopback()
    parts = urllib.parse.urlsplit(redirect)
    outcome: dict[str, Any] = {}

    def serve() -> None:
        outcome["result"] = wait_for_callback(
            listener,
            redirect_uri=redirect,
            state="state-1",
            verifier="verifier-1",
            issuer="https://issuer.example",
            timeout=20,
        )

    server = threading.Thread(target=serve, daemon=True)
    server.start()

    def exchange(request: bytes) -> bytes:
        with socket.create_connection((parts.hostname or "", parts.port or 0), timeout=10) as peer:
            peer.sendall(request)
            received = bytearray()
            while chunk := peer.recv(4096):
                received.extend(chunk)
            return bytes(received)

    body = b"x" * 12_000  # larger than one listener read: bytes stay unread at close
    stray = (
        f"POST {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode("ascii") + body
    # A reset on close would discard this response (Windows: WinError 10054).
    assert exchange(stray).startswith(b"HTTP/1.1 400")

    good = (
        f"GET {parts.path}?state=state-1&code=real-code&iss=https%3A%2F%2Fissuer.example "
        f"HTTP/1.1\r\nHost: {parts.netloc}\r\n\r\n"
    ).encode("ascii")
    assert exchange(good).startswith(b"HTTP/1.1 200")
    server.join(timeout=20)
    assert outcome["result"].code == "real-code"


def test_draining_a_trickling_peer_is_bounded_in_total_time() -> None:
    # A local process that keeps trickling bytes must not hold the single-threaded
    # listener: the drain stops after a fixed total time, not only per recv.
    from zenture._auth import browser

    ours, peer = socket.socketpair()
    stop = threading.Event()

    def trickle() -> None:
        with contextlib.suppress(OSError):
            deadline = time.monotonic() + 3.0
            while not stop.is_set() and time.monotonic() < deadline:
                peer.sendall(b"x")
                time.sleep(0.05)
            peer.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    try:
        started = time.monotonic()
        browser._drain_before_close(ours)  # pyright: ignore[reportPrivateUsage]  # white-box bound check
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        ours.close()
        peer.close()
        sender.join(timeout=2)
    assert elapsed < 1.0


def test_reading_a_trickling_request_head_is_bounded_in_total_time() -> None:
    # A local peer that never finishes its request head must not hold the
    # single-threaded listener (which also blocks cancel and the login timeout).
    from zenture._auth import browser

    ours, peer = socket.socketpair()
    stop = threading.Event()

    def trickle() -> None:
        with contextlib.suppress(OSError):
            deadline = time.monotonic() + 4.0
            peer.sendall(b"GET /oauth/callback HTTP/1.1\r\n")
            while not stop.is_set() and time.monotonic() < deadline:
                peer.sendall(b"X")
                time.sleep(0.05)
            peer.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=trickle, daemon=True)
    sender.start()
    try:
        started = time.monotonic()
        result = browser._read_request(ours)  # pyright: ignore[reportPrivateUsage]  # white-box bound check
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        ours.close()
        peer.close()
        sender.join(timeout=2)
    assert result is None
    assert elapsed < 3.0
