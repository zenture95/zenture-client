"""Explicit login: reuse a stored authorization or run the loopback browser flow."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from zenture._auth.browser import (
    LOGIN_TIMEOUT_SECONDS,
    BrowserOpener,
    authorization_url,
    bind_loopback,
    pkce_pair,
    wait_for_callback,
)
from zenture._auth.device import (
    Clock,
    Presenter,
    Sleep,
    run_device_login,
    terminal_presenter,
)
from zenture._auth.discovery import Discovery, discover
from zenture._auth.errors import AuthorizationRequired, AuthUnavailable, SecureStoreUnavailable
from zenture._auth.http import new_client
from zenture._auth.lock import LOCK_WAIT_SECONDS, LockFactory, binding_lock
from zenture._auth.model import CLIENT_ID, Target
from zenture._auth.session import AuthSession, SessionCore
from zenture._auth.store import (
    MemoryStore,
    RecordKey,
    RecordStore,
    StoredRecord,
    StoreError,
    native_store,
)
from zenture._auth.tokens import KeyCache, TokenSet, redeem_authorization_code
from zenture.errors import ZentureMCPError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx


def _default_opener(url: str) -> object:
    import webbrowser

    return webbrowser.open(url, new=2)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Replaceable environment of a login; production defaults, explicit in tests."""

    opener: BrowserOpener = _default_opener
    http: Callable[[], httpx.Client] = new_client
    native_store: Callable[[], RecordStore] = native_store
    lock_directory: Path | None = None
    lock_wait_seconds: float = LOCK_WAIT_SECONDS
    login_timeout: float = LOGIN_TIMEOUT_SECONDS
    probe_timeout: float = 30.0
    presenter: Presenter = terminal_presenter
    clock: Clock = time.monotonic
    sleep: Sleep = time.sleep


def default_runtime() -> Runtime:
    """Production environment of every login, status and stored-session operation."""

    return Runtime()


@dataclass(slots=True)
class Prepared:
    discovery: Discovery
    store: RecordStore
    lock: LockFactory
    runtime: Runtime
    core: SessionCore = field(init=False)

    def __post_init__(self) -> None:
        self.core = SessionCore(
            discovery=self.discovery,
            store=self.store,
            lock=self.lock,
            http=self.runtime.http,
        )


MCP_TRANSPORT_UNAVAILABLE = "mcp_transport_unavailable"


def is_mcp_outage(exc: ZentureMCPError) -> bool:
    """Only a transport outage is transient; dependency and protocol errors are not."""

    return exc.code == MCP_TRANSPORT_UNAVAILABLE


def _mcp_unavailable() -> AuthUnavailable:
    """The stored authorization stays; an MCP outage never justifies a new authorization."""

    return AuthUnavailable("mcp_unavailable", next_action="retry_later")


def _no_lock(_identity: RecordKey) -> contextlib.AbstractContextManager[None]:
    return contextlib.nullcontext()


class LoginFlow:
    """Login orchestration over an injectable :class:`Runtime`."""

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime or default_runtime()

    # -- public entry points ---------------------------------------------------------
    def login(
        self, *, device: bool = False, session_only: bool = False, endpoint: str | None = None
    ) -> AuthSession:
        prepared = self.prepare(session_only=session_only, endpoint=endpoint)
        if not session_only and self._reuse(prepared):
            try:
                asyncio.run(self.probe(prepared))
            except AuthorizationRequired:
                pass
            except ZentureMCPError as exc:
                if not is_mcp_outage(exc):
                    raise
                raise _mcp_unavailable() from exc
            else:
                return AuthSession(prepared.core)
            prepared = self._renew(prepared)
        return self._authorize(prepared, device, threading.Event())

    async def login_async(
        self, *, device: bool = False, session_only: bool = False, endpoint: str | None = None
    ) -> AuthSession:
        prepared = await asyncio.to_thread(
            self.prepare, session_only=session_only, endpoint=endpoint
        )
        if not session_only and await asyncio.to_thread(self._reuse, prepared):
            try:
                await self.probe(prepared)
            except AuthorizationRequired:
                pass
            except ZentureMCPError as exc:
                if not is_mcp_outage(exc):
                    raise
                raise _mcp_unavailable() from exc
            else:
                return AuthSession(prepared.core)
            prepared = await asyncio.to_thread(self._renew, prepared)
        cancel = threading.Event()
        try:
            return await asyncio.to_thread(self._authorize, prepared, device, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

    # -- steps -----------------------------------------------------------------------
    def prepare(self, *, session_only: bool, endpoint: str | None) -> Prepared:
        runtime = self._runtime
        target = Target.from_endpoint(endpoint)
        with runtime.http() as client:
            discovery = discover(client, target)
        if session_only:
            store: RecordStore = MemoryStore()
            return Prepared(discovery=discovery, store=store, lock=_no_lock, runtime=runtime)
        store = runtime.native_store()
        directory = runtime.lock_directory

        def lock(identity: RecordKey) -> contextlib.AbstractContextManager[None]:
            return binding_lock(
                identity, directory=directory, wait_seconds=runtime.lock_wait_seconds
            )

        return Prepared(discovery=discovery, store=store, lock=lock, runtime=runtime)

    def _renew(self, prepared: Prepared) -> Prepared:
        """Fresh session state for a new authorization after a failed reuse."""

        return Prepared(
            discovery=prepared.discovery,
            store=prepared.store,
            lock=prepared.lock,
            runtime=prepared.runtime,
        )

    def _reuse(self, prepared: Prepared) -> bool:
        """D-35: refresh a stored ready record once; ``False`` leads to browser login."""

        identity = prepared.core.identity
        try:
            record = prepared.store.load(identity)
        except StoreError as exc:
            raise SecureStoreUnavailable from exc
        if record is None:
            return False
        try:
            prepared.core.refresh()
        except AuthorizationRequired:
            return False
        return True

    async def probe(self, prepared: Prepared) -> None:
        """Authenticated, non-activity MCP ``initialize`` + ``tools/list`` probe."""

        from zenture._mcp.client import AsyncMcpClient

        session = AuthSession(prepared.core)
        endpoint = prepared.discovery.resource + "/"
        async with AsyncMcpClient.connect(
            endpoint, session=session, timeout=prepared.runtime.probe_timeout
        ) as client:
            await client.list_tools()

    def _authorize(self, prepared: Prepared, device: bool, cancel: threading.Event) -> AuthSession:
        if device:
            return self._device(prepared, cancel)
        return self._browser(prepared, cancel)

    def _device(self, prepared: Prepared, cancel: threading.Event) -> AuthSession:
        runtime = prepared.runtime
        keys = KeyCache()
        with runtime.http() as client:
            tokens = run_device_login(
                client,
                prepared.discovery,
                keys,
                present=runtime.presenter,
                clock=runtime.clock,
                sleep=runtime.sleep,
                cancel=cancel,
            )
        return self._adopt(prepared, tokens, keys)

    def _adopt(self, prepared: Prepared, tokens: TokenSet, keys: KeyCache) -> AuthSession:
        self._persist(prepared, tokens)
        prepared.core.keys = keys
        prepared.core.adopt(tokens)
        return AuthSession(prepared.core)

    def _browser(self, prepared: Prepared, cancel: threading.Event) -> AuthSession:
        discovery = prepared.discovery
        runtime = prepared.runtime
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        listener, redirect_uri = bind_loopback()
        try:
            url = authorization_url(
                discovery, redirect_uri=redirect_uri, state=state, challenge=challenge
            )
            if runtime.opener(url) is False:
                raise AuthUnavailable("browser_unavailable", next_action="use_device_login")
        except BaseException:
            listener.close()
            raise
        callback = wait_for_callback(
            listener,
            redirect_uri=redirect_uri,
            state=state,
            verifier=verifier,
            issuer=discovery.issuer,
            timeout=runtime.login_timeout,
            cancel=cancel,
        )
        with runtime.http() as client:
            keys = KeyCache()
            tokens = redeem_authorization_code(
                client,
                discovery,
                keys,
                code=callback.code,
                verifier=callback.verifier,
                redirect_uri=callback.redirect_uri,
            )
        return self._adopt(prepared, tokens, keys)

    def _persist(self, prepared: Prepared, tokens: TokenSet) -> None:
        identity = RecordKey(prepared.discovery.issuer, prepared.discovery.resource, CLIENT_ID)
        record = StoredRecord(identity, tokens.binding, tokens.refresh_token, state="ready")
        try:
            with prepared.lock(identity):
                prepared.store.save(record)
        except StoreError as exc:
            raise SecureStoreUnavailable from exc
