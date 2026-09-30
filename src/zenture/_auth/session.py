"""Authorization session: per-request bearer supply and the crash-safe refresh rotation."""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from zenture._auth.errors import AuthorizationRequired, AuthUnavailable
from zenture._auth.http import PRE_SEND_ERRORS, JsonResponse, new_client, request_json
from zenture._auth.model import CLIENT_ID, Binding, within_resource
from zenture._auth.store import RecordKey, RecordStore, StoredRecord, StoreError
from zenture._auth.tokens import KeyCache, TokenRejected, TokenSet, parse_token_response

if TYPE_CHECKING:
    from collections.abc import Callable

    from zenture._auth.discovery import Discovery
    from zenture._auth.lock import LockFactory

_EXPIRY_MARGIN_SECONDS = 30.0


@dataclass(slots=True, repr=False)
class SessionCore:
    """Serialized access-token supply over one stored refresh authority (D-51).

    Access tokens live only here, in memory. Every rotation of the refresh
    token happens under a per-binding cross-process lock and is journalled in
    the store (``rotating`` before the request is sent), so an outcome that
    cannot be proven never leads to a second use of a consumed token.
    """

    discovery: Discovery
    store: RecordStore
    lock: LockFactory
    http: Callable[[], httpx.Client] = new_client
    keys: KeyCache = field(default_factory=KeyCache)
    clock: Callable[[], float] = time.time
    binding: Binding | None = None
    closed: bool = False
    _guard: threading.Lock = field(default_factory=threading.Lock, init=False)
    _access: str | None = field(default=None, init=False)
    _expires_at: float = field(default=0.0, init=False)

    def __repr__(self) -> str:
        return f"SessionCore(binding={self.binding!r})"

    @property
    def identity(self) -> RecordKey:
        return RecordKey(self.discovery.issuer, self.discovery.resource, CLIENT_ID)

    def require_resource(self, url: str) -> None:
        """Refuse any request target outside the discovered resource (before a token exists)."""

        if not within_resource(url, self.discovery.resource):
            raise AuthUnavailable("resource_mismatch", next_action="check_endpoint")

    def adopt(self, tokens: TokenSet) -> None:
        """Take over tokens of a fresh login (record already persisted by the caller)."""

        with self._guard:
            self._access = tokens.access_token
            self._expires_at = tokens.expires_at
            self.binding = tokens.binding

    def close(self) -> None:
        with self._guard:
            self.closed = True
            self._access = None

    def access_token(self) -> str:
        """Return a current access token, refreshing once if none is usable."""

        with self._guard:
            self._require_open()
            if (
                self._access is not None
                and self._expires_at - self.clock() > _EXPIRY_MARGIN_SECONDS
            ):
                return self._access
            return self._rotate()

    def refresh_after_rejection(self, rejected: str) -> str:
        """Serialized refresh after a 401; reuse a token another caller already refreshed."""

        with self._guard:
            self._require_open()
            if self._access is not None and self._access != rejected:
                return self._access
            return self._rotate()

    def refresh(self) -> str:
        """Force one rotation (used by explicit login reuse)."""

        with self._guard:
            self._require_open()
            return self._rotate()

    def _require_open(self) -> None:
        if self.closed:
            raise AuthorizationRequired("session_closed")

    # -- D-51 rotation ---------------------------------------------------------------
    def _rotate(self) -> str:
        identity = self.identity
        with self.lock(identity):
            try:
                record = self.store.load(identity)
            except StoreError as exc:
                raise AuthUnavailable("store_read_failed") from exc
            if record is None:
                self._drop()
                raise AuthorizationRequired("no_stored_authorization")
            if record.state == "rotating":
                # A previous rotation's outcome is unknown: its token may be consumed.
                self._forget(identity)
                raise AuthorizationRequired("rotation_outcome_unknown")
            if self.binding is not None and record.binding != self.binding:
                # Another process re-logged in; keep the newer authority, refuse this session.
                self._drop()
                raise AuthorizationRequired("binding_changed")
            with self.http() as client:
                self._prefetch_keys(client)
                return self._send_rotation(client, record)

    def _prefetch_keys(self, client: httpx.Client) -> None:
        if self.keys.keys:
            return
        try:
            self.keys.load(client, self.discovery)
        except (httpx.HTTPError, TokenRejected) as exc:
            raise AuthUnavailable("jwks_unavailable") from exc

    def _send_rotation(self, client: httpx.Client, record: StoredRecord) -> str:
        identity = record.identity
        try:
            self.store.save(record.with_state("rotating", secrets.token_urlsafe(9)))
        except StoreError as exc:
            raise AuthUnavailable("store_write_failed") from exc
        form = {
            "grant_type": "refresh_token",
            "refresh_token": record.refresh_token,
            "client_id": CLIENT_ID,
            "resource": self.discovery.resource,
        }
        try:
            response = request_json(client, "POST", self.discovery.token_endpoint, form=form)
        except PRE_SEND_ERRORS as exc:
            self._restore_ready(record)
            raise AuthUnavailable("token_endpoint_unreachable") from exc
        except httpx.HTTPError as exc:
            # Possibly sent: the record stays ``rotating`` and is never reused.
            raise AuthorizationRequired("rotation_outcome_unknown") from exc
        if _is_invalid_grant(response):
            self._forget(identity)
            raise AuthorizationRequired("refresh_rejected")
        try:
            tokens = parse_token_response(
                response, discovery=self.discovery, keys=self.keys, client=client
            )
        except AuthorizationRequired:
            self._forget(identity)
            raise
        except TokenRejected as exc:
            if response.status == 200:
                self._forget(identity)
                raise AuthorizationRequired("token_response_rejected") from exc
            raise AuthorizationRequired("rotation_outcome_unknown") from exc
        if tokens.binding != record.binding:
            self._forget(identity)
            raise AuthorizationRequired("binding_changed")
        try:
            self.store.save(
                StoredRecord(identity, tokens.binding, tokens.refresh_token, state="ready")
            )
        except StoreError as exc:
            self._forget(identity)
            raise AuthorizationRequired("store_write_failed") from exc
        self._access = tokens.access_token
        self._expires_at = tokens.expires_at
        self.binding = tokens.binding
        return tokens.access_token

    def _restore_ready(self, record: StoredRecord) -> None:
        with contextlib.suppress(StoreError):
            self.store.save(record.with_state("ready"))

    def _forget(self, identity: RecordKey) -> None:
        with contextlib.suppress(StoreError):
            self.store.delete(identity)
        self._drop()

    def _drop(self) -> None:
        self._access = None
        self._expires_at = 0.0


def _is_invalid_grant(response: JsonResponse) -> bool:
    return (
        response.status == 400
        and response.body is not None
        and response.body.get("error") == "invalid_grant"
    )


class AuthSession:
    """Opaque authorization handle; safe to print, never exposes a credential."""

    __slots__ = ("_core",)

    def __init__(self, core: SessionCore) -> None:
        self._core = core

    @property
    def binding(self) -> Binding:
        binding = self._core.binding
        if binding is None:
            raise AuthorizationRequired("session_unbound")
        return binding

    def close(self) -> None:
        """Release in-memory credentials; the stored record is left untouched."""

        self._core.close()

    def __enter__(self) -> AuthSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        binding = self._core.binding
        if binding is None:
            return "AuthSession(unbound)"
        return (
            f"AuthSession(sub={binding.sub!r}, connection_id={binding.connection_id!r}, "
            f"owner_epoch={binding.owner_epoch}, "
            f"authorization_generation={binding.authorization_generation})"
        )

    def __reduce__(self) -> tuple[()]:
        raise TypeError("AuthSession cannot be pickled")
