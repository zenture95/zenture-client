"""Operations on the stored authorization that never start a login."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from zenture._auth.errors import (
    AuthorizationRequired,
    AuthUnavailable,
    PermissionDenied,
    SecureStoreUnavailable,
)
from zenture._auth.login import LoginFlow, Prepared, Runtime
from zenture._auth.store import StoredRecord, StoreError
from zenture.errors import ZentureMCPError

if TYPE_CHECKING:
    from zenture._auth.model import Binding
    from zenture._auth.session import AuthSession

StatusState = Literal[
    "not_logged_in", "connected", "authorization_required", "unavailable", "store_unavailable"
]


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Outcome of verifying the stored authorization; never carries a credential."""

    state: StatusState
    issuer: str | None = None
    resource: str | None = None
    binding: Binding | None = None
    code: str | None = None


def _load(prepared: Prepared) -> StoredRecord | None:
    try:
        return prepared.store.load(prepared.core.identity)
    except StoreError as exc:
        raise SecureStoreUnavailable from exc


def open_stored_session(endpoint: str | None, runtime: Runtime | None = None) -> AuthSession:
    """Return a session over the stored record; never interactive, never starts a login."""

    from zenture._auth.session import AuthSession

    prepared = LoginFlow(runtime).prepare(session_only=False, endpoint=endpoint)
    record = _load(prepared)
    if record is None:
        raise AuthorizationRequired(
            "no_stored_authorization", next_action="login", hint="run `zenture auth login`"
        )
    prepared.core.binding = record.binding
    return AuthSession(prepared.core)


def clear_stored(endpoint: str | None, runtime: Runtime | None = None) -> bool:
    """Delete only the local record; return whether one existed."""

    prepared = LoginFlow(runtime).prepare(session_only=False, endpoint=endpoint)
    identity = prepared.core.identity
    try:
        with prepared.lock(identity):
            existed = prepared.store.load(identity) is not None
            prepared.store.delete(identity)
    except StoreError as exc:
        raise SecureStoreUnavailable from exc
    return existed


def check_status(endpoint: str | None, runtime: Runtime | None = None) -> StatusReport:
    """Verify by one refresh and one authenticated, non-activity MCP probe."""

    flow = LoginFlow(runtime)
    try:
        prepared = flow.prepare(session_only=False, endpoint=endpoint)
    except SecureStoreUnavailable as exc:
        return StatusReport("store_unavailable", code=exc.code)
    except AuthUnavailable as exc:
        return StatusReport("unavailable", code=exc.code)
    identity = prepared.core.identity

    def report(
        state: StatusState, code: str | None = None, binding: Binding | None = None
    ) -> StatusReport:
        return StatusReport(
            state, issuer=identity.issuer, resource=identity.resource, binding=binding, code=code
        )

    try:
        record = _load(prepared)
        if record is None:
            return report("not_logged_in")
        prepared.core.binding = record.binding
        prepared.core.refresh()
        asyncio.run(flow.probe(prepared))
    except SecureStoreUnavailable as exc:
        return report("store_unavailable", exc.code)
    except (AuthorizationRequired, PermissionDenied) as exc:
        return report("authorization_required", exc.code)
    except AuthUnavailable as exc:
        return report("unavailable", exc.code)
    except ZentureMCPError:
        return report("unavailable", "mcp_unavailable")
    return report("connected", binding=prepared.core.binding)
