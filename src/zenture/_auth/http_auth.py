"""``httpx2.Auth`` that feeds per-request bearers from an authorization session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2

from zenture._auth.errors import AuthorizationRequired, PermissionDenied

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from zenture._auth.session import AuthSession, SessionCore


def _core(session: AuthSession) -> SessionCore:
    # Internal auth adapter shares the session core without exposing credential access publicly.
    return session._core  # pyright: ignore[reportPrivateUsage]


class SessionAuth(httpx2.Auth):
    """Inject a bearer per request; one serialized refresh and one retry on 401.

    A second 401 means the authorization is unusable (``AuthorizationRequired``);
    403 is final (``PermissionDenied``). This class never starts a browser or
    device login and never requests additional scope.
    """

    def __init__(self, session: AuthSession) -> None:
        self._core = _core(session)

    @staticmethod
    def _bearer(request: httpx2.Request, token: str) -> None:
        request.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _check_final(response: httpx2.Response) -> None:
        if response.status_code == 403:
            raise PermissionDenied
        if response.status_code == 401:
            raise AuthorizationRequired("unauthorized_after_refresh")

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        self._core.require_resource(str(request.url))
        token = await asyncio.to_thread(self._core.access_token)
        self._bearer(request, token)
        response = yield request
        if response.status_code == 403:
            raise PermissionDenied
        if response.status_code != 401:
            return
        token = await asyncio.to_thread(self._core.refresh_after_rejection, token)
        self._bearer(request, token)
        response = yield request
        self._check_final(response)

    def sync_auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        self._core.require_resource(str(request.url))
        token = self._core.access_token()
        self._bearer(request, token)
        response = yield request
        if response.status_code == 403:
            raise PermissionDenied
        if response.status_code != 401:
            return
        token = self._core.refresh_after_rejection(token)
        self._bearer(request, token)
        response = yield request
        self._check_final(response)
