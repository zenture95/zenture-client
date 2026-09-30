"""Explicit native authorization for the hosted zenture MCP endpoint.

Importing this package performs no network I/O, opens no browser and touches
neither the credential store nor any lock file; only ``login`` and
``login_async`` do, and only when called.
"""

from __future__ import annotations

from zenture._auth.errors import (
    AuthorizationRequired,
    AuthUnavailable,
    LoginCancelled,
    PermissionDenied,
    SecureStoreUnavailable,
)
from zenture._auth.login import LoginFlow
from zenture._auth.session import AuthSession


def login(
    *, device: bool = False, session_only: bool = False, endpoint: str | None = None
) -> AuthSession:
    """Authorize through the system browser and return an opaque session.

    A stored authorization is reused after one refresh and an authenticated
    probe; only when it is no longer usable does a new browser authorization
    start. ``session_only=True`` keeps every credential in memory.
    """

    if device:
        raise NotImplementedError("device login is not available yet")
    return LoginFlow().login(session_only=session_only, endpoint=endpoint)


async def login_async(
    *, device: bool = False, session_only: bool = False, endpoint: str | None = None
) -> AuthSession:
    """Asynchronous form of :func:`login`."""

    if device:
        raise NotImplementedError("device login is not available yet")
    return await LoginFlow().login_async(session_only=session_only, endpoint=endpoint)


__all__ = [
    "AuthSession",
    "AuthUnavailable",
    "AuthorizationRequired",
    "LoginCancelled",
    "PermissionDenied",
    "SecureStoreUnavailable",
    "login",
    "login_async",
]
