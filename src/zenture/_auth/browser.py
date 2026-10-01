"""RFC 8252 loopback browser authorization (Authorization Code + PKCE S256)."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import secrets
import select
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit

from zenture._auth.errors import AuthUnavailable, LoginCancelled
from zenture._auth.model import CLIENT_ID, SCOPE

if TYPE_CHECKING:
    import threading

    from zenture._auth.discovery import Discovery

CALLBACK_PATH = "/oauth/callback"
LOGIN_TIMEOUT_SECONDS = 600.0
_MAX_REQUEST_BYTES = 16 * 1024
_CONNECTION_READ_SECONDS = 0.5
_SLICE_SECONDS = 0.2
_PAGE = (
    b"<!doctype html><html lang=en><meta charset=utf-8><title>zenture</title>"
    b"<body><p>%s</p></body></html>"
)

BrowserOpener = Callable[[str], object]


@dataclass(frozen=True, slots=True, repr=False)
class CallbackResult:
    """The validated authorization response; ``code`` is a bearer-like secret."""

    code: str
    redirect_uri: str
    verifier: str

    def __repr__(self) -> str:
        return "CallbackResult()"


def bind_loopback() -> tuple[socket.socket, str]:
    """Listen on 127.0.0.1 (fallback ::1) on an ephemeral port; never a LAN address."""

    last_error: OSError | None = None
    for family, host, display in (
        (socket.AF_INET, "127.0.0.1", "127.0.0.1"),
        (socket.AF_INET6, "::1", "[::1]"),
    ):
        try:
            listener = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            last_error = exc
            continue
        try:
            if sys.platform == "win32":
                # Windows lets a second local process share a port unless it is claimed exclusively.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, 0))
            listener.listen(4)
        except OSError as exc:
            listener.close()
            last_error = exc
            continue
        port = listener.getsockname()[1]
        return listener, f"http://{display}:{port}{CALLBACK_PATH}"
    raise AuthUnavailable("loopback_unavailable", next_action="use_device_login") from last_error


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(
    discovery: Discovery, *, redirect_uri: str, state: str, challenge: str
) -> str:
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": discovery.resource,
    }
    encoded = "&".join(f"{key}={quote(value, safe='')}" for key, value in query.items())
    return f"{discovery.authorization_endpoint}?{encoded}"


def _read_request(connection: socket.socket) -> tuple[str, str, dict[str, str]] | None:
    connection.settimeout(_CONNECTION_READ_SECONDS)
    received = bytearray()
    try:
        while b"\r\n\r\n" not in received:
            chunk = connection.recv(4096)
            if not chunk:
                return None
            received.extend(chunk)
            if len(received) > _MAX_REQUEST_BYTES:
                return None
    except OSError:
        return None
    head = bytes(received).split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return parts[0], parts[1], headers


def _respond(connection: socket.socket, status: str, message: str) -> None:
    body = _PAGE % message.encode("ascii")
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    with contextlib.suppress(OSError):
        connection.sendall(head + body)


def _single(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values is None or len(values) != 1 or not values[0]:
        return None
    return values[0]


def _classify(
    request: tuple[str, str, dict[str, str]] | None, *, host_header: str, state: str
) -> dict[str, list[str]] | None:
    """Return the query of an acceptable callback, else ``None`` (request is ignored)."""

    if request is None:
        return None
    method, target, headers = request
    if method != "GET" or headers.get("host") != host_header:
        return None
    try:
        parsed = urlsplit(target)
        if parsed.path != CALLBACK_PATH or parsed.fragment:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        return None
    received_state = _single(query, "state")
    if (
        received_state is None
        or not received_state.isascii()
        or not hmac.compare_digest(received_state, state)
    ):
        return None
    return query


def wait_for_callback(
    listener: socket.socket,
    *,
    redirect_uri: str,
    state: str,
    verifier: str,
    issuer: str,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    cancel: threading.Event | None = None,
) -> CallbackResult:
    """Accept exactly one valid callback, then stop listening."""

    host_header = urlsplit(redirect_uri).netloc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise LoginCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthUnavailable("login_timed_out", next_action="login")
            ready, _, _ = select.select([listener], [], [], min(_SLICE_SECONDS, remaining))
            if not ready:
                continue
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            with connection:
                query = _classify(_read_request(connection), host_header=host_header, state=state)
                if query is None:
                    _respond(connection, "400 Bad Request", "Invalid request.")
                    continue
                return _finish(connection, query, issuer, redirect_uri, verifier)
    finally:
        listener.close()


def _finish(
    connection: socket.socket,
    query: dict[str, list[str]],
    issuer: str,
    redirect_uri: str,
    verifier: str,
) -> CallbackResult:
    if _single(query, "iss") != issuer:
        _respond(connection, "400 Bad Request", "Authorization response rejected.")
        raise AuthUnavailable("authorization_response_invalid")
    error = _single(query, "error")
    if error is not None:
        if error == "access_denied":
            _respond(connection, "200 OK", "Authorization was cancelled. You can close this tab.")
            raise LoginCancelled
        _respond(connection, "200 OK", "Authorization failed. You can close this tab.")
        raise AuthUnavailable("authorization_failed")
    code = _single(query, "code")
    if code is None:
        _respond(connection, "400 Bad Request", "Authorization response rejected.")
        raise AuthUnavailable("authorization_response_invalid")
    _respond(connection, "200 OK", "Authorization received. Return to your terminal.")
    return CallbackResult(code=code, redirect_uri=redirect_uri, verifier=verifier)
