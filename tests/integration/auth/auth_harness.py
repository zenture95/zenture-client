"""Loopback fake issuer, fake hosted MCP server and store doubles for auth tests.

The issuer mirrors the contract behaviors the client depends on: S256 PKCE,
``iss`` in authorization responses, ES256 access JWTs, rotating refresh tokens
and revocation of the whole grant when a consumed refresh token is presented.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec
from mcp.server.mcpserver import MCPServer

from zenture._auth.login import Runtime
from zenture._auth.model import CLIENT_ID

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from zenture._auth.store import RecordStore

HANG = 599  # forced status: the request is recorded, then never answered in time
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REDIRECT_PATTERN = re.compile(r"^http://(127\.0\.0\.1|\[::1\]):\d+/oauth/callback$")


@dataclass(slots=True)
class IssuedGrant:
    grant_id: str
    sub: str
    connection_id: str
    owner_epoch: int
    generation: int
    revoked: bool = False


@dataclass(slots=True)
class FakeIssuer:
    """Threaded loopback authorization server with inspectable state."""

    port: int = 0
    resource: str = ""
    kid: str = "test-key"
    access_ttl: int = 300
    metadata_overrides: dict[str, Any] = field(default_factory=dict)
    prm_overrides: dict[str, Any] = field(default_factory=dict)
    response_iss: str | None = None  # None -> correct issuer
    omit_iss: bool = False
    deny: bool = False
    omit_refresh_token: bool = False
    access_audience: str | None = None
    access_client_id: str = CLIENT_ID
    refresh_behaviors: list[str] = field(default_factory=list)
    refresh_hold: threading.Event = field(default_factory=threading.Event)
    refresh_seen: threading.Event = field(default_factory=threading.Event)
    auth_requests: list[dict[str, str]] = field(default_factory=list)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    refresh_requests: list[str] = field(default_factory=list)
    reuse_events: int = 0
    concurrent_refreshes: int = 0
    max_concurrent_refreshes: int = 0
    _codes: dict[str, dict[str, str]] = field(default_factory=dict)
    _refresh: dict[str, tuple[IssuedGrant, bool]] = field(default_factory=dict)
    _grants: dict[str, IssuedGrant] = field(default_factory=dict)
    _access: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _key: Any = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    jwks_key: Any = None
    refresh_delay: float = 0.0
    device_script: list[str] = field(default_factory=list)
    device_requests: list[dict[str, str]] = field(default_factory=list)
    device_polls: list[dict[str, str]] = field(default_factory=list)
    device_interval: int = 5
    device_expires_in: int = 600
    user_code: str = "WDJBMJHT"
    _device_codes: dict[str, str] = field(default_factory=dict)

    @property
    def issuer(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FakeIssuer:
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                issuer._get(self)

            def do_POST(self) -> None:
                issuer._post(self)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.refresh_hold.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- helpers -----------------------------------------------------------------------
    def metadata(self) -> dict[str, Any]:
        base = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/auth",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "device_authorization_endpoint": f"{self.issuer}/device/auth",
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            "token_endpoint_auth_methods_supported": ["none"],
        }
        return {**base, **self.metadata_overrides}

    def prm(self) -> dict[str, Any]:
        base = {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": ["mcp:run"],
            "bearer_methods_supported": ["header"],
        }
        return {**base, **self.prm_overrides}

    def jwks(self) -> dict[str, Any]:
        public = jwt.algorithms.ECAlgorithm.to_jwk(
            (self.jwks_key or self._key).public_key(), as_dict=True
        )
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "ES256"}]}

    def accepts(self, token: str) -> bool:
        with self._lock:
            expiry = self._access.get(token)
        return expiry is not None and expiry > time.time()

    def grant_revoked(self) -> bool:
        return any(grant.revoked for grant in self._grants.values())

    def expire_access_tokens(self) -> None:
        with self._lock:
            self._access = dict.fromkeys(self._access, 0.0)

    def _access_jwt(self, grant: IssuedGrant) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.access_audience or self.resource,
            "sub": grant.sub,
            "client_id": self.access_client_id,
            "scope": "openid mcp:run offline_access",
            "principal_type": "user",
            "authorization_generation": grant.generation,
            "connection_id": grant.connection_id,
            "owner_epoch": grant.owner_epoch,
            "iat": now,
            "exp": now + self.access_ttl,
            "jti": secrets.token_hex(8),
        }
        token = jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": self.kid})
        with self._lock:
            self._access[token] = float(now + self.access_ttl)
        return token

    def _new_refresh(self, grant: IssuedGrant) -> str:
        token = "rt_" + secrets.token_urlsafe(24)
        with self._lock:
            self._refresh[token] = (grant, False)
        return token

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    # -- routes ------------------------------------------------------------------------
    def _get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        if parsed.path == "/.well-known/oauth-authorization-server":
            self._json(handler, 200, self.metadata())
        elif parsed.path == "/.well-known/oauth-protected-resource":
            self._json(handler, 200, self.prm())
        elif parsed.path == "/jwks":
            self._json(handler, 200, self.jwks())
        elif parsed.path == "/auth":
            self._authorize(handler, dict(urllib.parse.parse_qsl(parsed.query)))
        else:
            self._json(handler, 404, {"error": "not_found"})

    def _authorize(self, handler: BaseHTTPRequestHandler, query: dict[str, str]) -> None:
        self.auth_requests.append(query)
        redirect = query.get("redirect_uri", "")
        valid = (
            query.get("response_type") == "code"
            and query.get("client_id") == CLIENT_ID
            and query.get("code_challenge_method") == "S256"
            and query.get("scope") == "openid mcp:run offline_access"
            and query.get("resource") == self.resource
            and REDIRECT_PATTERN.fullmatch(redirect) is not None
            and len(query.get("state", "")) >= 43
            and len(query.get("code_challenge", "")) == 43
        )
        if not valid:
            self._json(handler, 400, {"error": "invalid_request"})
            return
        params: dict[str, str] = {"state": query["state"]}
        if self.deny:
            params["error"] = "access_denied"
        else:
            code = "code_" + secrets.token_urlsafe(16)
            with self._lock:
                self._codes[code] = {
                    "challenge": query["code_challenge"],
                    "redirect_uri": redirect,
                }
            params["code"] = code
        if not self.omit_iss:
            params["iss"] = self.response_iss or self.issuer
        handler.send_response(302)
        handler.send_header("Location", f"{redirect}?{urllib.parse.urlencode(params)}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _post(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        form = dict(urllib.parse.parse_qsl(handler.rfile.read(length).decode()))
        path = urllib.parse.urlsplit(handler.path).path
        if path == "/device/auth":
            self._device_authorization(handler, form)
            return
        if path != "/token":
            self._json(handler, 404, {"error": "not_found"})
            return
        self.token_requests.append(dict(form.items()))
        if form.get("grant_type") == "authorization_code":
            self._redeem(handler, form)
        elif form.get("grant_type") == "refresh_token":
            self._rotate(handler, form)
        elif form.get("grant_type") == DEVICE_GRANT:
            self._device_poll(handler, form)
        else:
            self._json(handler, 400, {"error": "unsupported_grant_type"})

    def _device_authorization(self, handler: BaseHTTPRequestHandler, form: dict[str, str]) -> None:
        self.device_requests.append(dict(form.items()))
        if (
            form.get("client_id") != CLIENT_ID
            or form.get("scope") != "openid mcp:run offline_access"
            or form.get("resource") != self.resource
        ):
            self._json(handler, 400, {"error": "invalid_request"})
            return
        device_code = "dc_" + secrets.token_urlsafe(24)
        with self._lock:
            self._device_codes[device_code] = "open"
        self._json(
            handler,
            200,
            {
                "device_code": device_code,
                "user_code": self.user_code,
                "verification_uri": f"{self.issuer}/device",
                "verification_uri_complete": f"{self.issuer}/device?user_code={self.user_code}",
                "expires_in": self.device_expires_in,
                "interval": self.device_interval,
            },
        )

    def _device_poll(self, handler: BaseHTTPRequestHandler, form: dict[str, str]) -> None:
        self.device_polls.append(dict(form.items()))
        with self._lock:
            state = self._device_codes.get(form.get("device_code", ""))
            step = self.device_script.pop(0) if self.device_script else "authorization_pending"
        if state != "open" or form.get("client_id") != CLIENT_ID:
            self._json(handler, 400, {"error": "invalid_grant"})
            return
        if step == "drop":
            with self._lock:
                self._device_codes[form["device_code"]] = "consumed"
            handler.connection.close()
            return
        if step == "server_error":
            self._json(handler, 503, {"error": "temporarily_unavailable"})
            return
        if step == "approve":
            with self._lock:
                self._device_codes[form["device_code"]] = "consumed"
            self._issue_first_tokens(handler)
            return
        self._json(handler, 400, {"error": step})

    def _redeem(self, handler: BaseHTTPRequestHandler, form: dict[str, str]) -> None:
        import base64
        import hashlib

        with self._lock:
            entry = self._codes.pop(form.get("code", ""), None)
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if (
            entry is None
            or entry["challenge"] != challenge
            or entry["redirect_uri"] != form.get("redirect_uri")
            or form.get("client_id") != CLIENT_ID
            or form.get("resource") != self.resource
        ):
            self._json(handler, 400, {"error": "invalid_grant"})
            return
        self._issue_first_tokens(handler)

    def _issue_first_tokens(self, handler: BaseHTTPRequestHandler) -> None:
        grant = IssuedGrant(
            grant_id=secrets.token_hex(6),
            sub="user-1",
            connection_id="conn-1",
            owner_epoch=3,
            generation=7,
        )
        self._grants[grant.grant_id] = grant
        body: dict[str, Any] = {
            "token_type": "Bearer",
            "access_token": self._access_jwt(grant),
            "expires_in": self.access_ttl,
        }
        if not self.omit_refresh_token:
            body["refresh_token"] = self._new_refresh(grant)
        self._json(handler, 200, body)

    def _rotate(self, handler: BaseHTTPRequestHandler, form: dict[str, str]) -> None:
        presented = form.get("refresh_token", "")
        with self._lock:
            self.refresh_requests.append(presented)
            self.concurrent_refreshes += 1
            self.max_concurrent_refreshes = max(
                self.max_concurrent_refreshes, self.concurrent_refreshes
            )
            entry = self._refresh.get(presented)
            behavior = self.refresh_behaviors.pop(0) if self.refresh_behaviors else "ok"
            if entry is not None:
                grant, consumed = entry
                if consumed or grant.revoked:
                    self.reuse_events += 1
                    grant.revoked = True
                    entry = None
                else:
                    self._refresh[presented] = (grant, True)
        self.refresh_seen.set()
        try:
            self._rotate_response(handler, form, entry, behavior)
        finally:
            with self._lock:
                self.concurrent_refreshes -= 1

    def _rotate_response(
        self,
        handler: BaseHTTPRequestHandler,
        form: dict[str, str],
        entry: tuple[IssuedGrant, bool] | None,
        behavior: str,
    ) -> None:
        if behavior == "hang":
            self.refresh_hold.wait(30)
            handler.connection.close()
            return
        if behavior == "drop":
            handler.connection.close()
            return
        if entry is None or form.get("client_id") != CLIENT_ID:
            self._json(handler, 400, {"error": "invalid_grant"})
            return
        if behavior == "server_error":
            self._json(handler, 503, {"error": "temporarily_unavailable"})
            return
        if behavior == "invalid_grant":
            self._json(handler, 400, {"error": "invalid_grant"})
            return
        grant = entry[0]
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        if behavior == "changed_generation":
            grant.generation += 1
        body = {
            "token_type": "Bearer",
            "access_token": self._access_jwt(grant),
            "refresh_token": self._new_refresh(grant),
            "expires_in": self.access_ttl,
        }
        self._json(handler, 200, body)


class _Guard:
    """ASGI wrapper: bearer check plus scripted 401/403 answers in front of MCPServer."""

    def __init__(self, app: Any, issuer: FakeIssuer, resource_metadata: str) -> None:
        self._app = app
        self._issuer = issuer
        self._metadata = resource_metadata
        self.forced: list[int] = []
        self.forced_tool_calls: list[int] = []
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope["path"] == "/.well-known/oauth-protected-resource":
            body = json.dumps(self._issuer.prm()).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        delivered = False

        async def replay() -> Any:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        try:
            rpc = json.loads(body).get("method", "")
        except (ValueError, AttributeError):
            rpc = ""
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        authorization = headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        status = self.forced.pop(0) if self.forced else 0
        if rpc == "tools/call" and self.forced_tool_calls:
            status = self.forced_tool_calls.pop(0)
        if status == 0 and not self._issuer.accepts(token):
            status = 401
        self.requests.append(
            {
                "method": scope["method"],
                "bearer": bool(token),
                "status": status or 200,
                "rpc": rpc,
            }
        )
        if status == HANG:
            await asyncio.sleep(3)
            status = 504
        if status:
            challenge = (
                f'Bearer error="invalid_token", resource_metadata="{self._metadata}", '
                'scope="mcp:run"'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", challenge.encode()),
                        (b"content-length", b"2"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})
            return
        await self._app(scope, replay, send)


class FakeMcp:
    """Real ``mcp`` Streamable HTTP server app behind a bearer guard, on a loopback port."""

    def __init__(self, issuer: FakeIssuer) -> None:
        self.issuer = issuer
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self.resource = f"http://127.0.0.1:{self.port}"
        issuer.resource = self.resource
        server = MCPServer("fake-zenture")

        @server.tool()
        def ping() -> str:
            return "pong"

        @server.tool()
        def echo() -> dict[str, str]:
            return {"state": "done"}

        self.guard = _Guard(
            server.streamable_http_app(
                streamable_http_path="/", host="127.0.0.1", stateless_http=True, json_response=True
            ),
            issuer,
            f"{self.resource}/.well-known/oauth-protected-resource",
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> FakeMcp:
        config = uvicorn.Config(self.guard, log_level="critical", lifespan="on")
        self._server = uvicorn.Server(config)
        server = self._server
        listener = self._listener
        self._thread = threading.Thread(
            target=lambda: __import__("asyncio").run(server.serve([listener])), daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


class Environment:
    """Issuer + MCP resource pair; serves PRM and AS metadata on their own origins."""

    def __init__(self) -> None:
        self.issuer = FakeIssuer()
        self.mcp = FakeMcp(self.issuer)

    def start(self) -> Environment:
        self.issuer.start()
        self.mcp.start()
        return self

    def stop(self) -> None:
        self.mcp.stop()
        self.issuer.stop()

    @property
    def endpoint(self) -> str:
        return self.mcp.resource + "/"


def browser_opener(
    *, tamper: Callable[[str], str] | None = None
) -> tuple[Callable[[str], object], list[str]]:
    """A browser stand-in that follows the authorization URL and hits the loopback callback."""

    opened: list[str] = []

    def drive(url: str) -> None:
        with httpx.Client(follow_redirects=False, timeout=10) as client:
            response = client.get(url)
            location = response.headers.get("location")
            if location is None:
                return
            callback = tamper(location) if tamper else location
            with contextlib.suppress(httpx.HTTPError):
                client.get(callback)

    def opener(url: str) -> object:
        opened.append(url)
        threading.Thread(target=drive, args=(url,), daemon=True).start()
        return True

    return opener, opened


def runtime_for(store: RecordStore, lock_dir: Path, opener: Any, **extra: Any) -> Runtime:
    options: dict[str, Any] = {"native_store": lambda: store, "lock_directory": lock_dir, **extra}
    return Runtime(opener=opener, **options)


class FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances it instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
