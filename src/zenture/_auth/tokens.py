"""Token-endpoint requests and access-token verification against the issuer JWKS."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import jwt
from jwt.algorithms import ECAlgorithm

from zenture._auth.errors import AuthorizationRequired, AuthUnavailable
from zenture._auth.http import JsonResponse, request_json
from zenture._auth.model import CLIENT_ID, Binding

if TYPE_CHECKING:
    from collections.abc import Callable

    from zenture._auth.discovery import Discovery

_MAX_TOKEN_LENGTH = 4096
_MAX_REFRESH_TOKEN_LENGTH = 512
_LEEWAY_SECONDS = 10


class TokenRejected(Exception):
    """A token response or access token failed verification (no detail kept)."""


@dataclass(frozen=True, slots=True, repr=False)
class TokenSet:
    """One verified token response; the access token never leaves memory."""

    access_token: str
    refresh_token: str
    expires_at: float
    binding: Binding

    def __repr__(self) -> str:
        return f"TokenSet(binding={self.binding!r})"


@dataclass(slots=True)
class KeyCache:
    """JWKS of one issuer; refetched at most once per unknown key id."""

    keys: dict[str, Any] = field(default_factory=cast("Callable[[], dict[str, Any]]", dict))

    def load(self, client: httpx.Client, discovery: Discovery) -> None:
        response = request_json(client, "GET", discovery.jwks_uri)
        raw = None if response.body is None else response.body.get("keys")
        if response.status != 200 or not isinstance(raw, list):
            raise TokenRejected
        loaded: dict[str, Any] = {}
        for item in cast("list[object]", raw):
            if isinstance(item, dict):
                item = cast("dict[object, object]", item)
                if (
                    item.get("kty") == "EC"
                    and item.get("crv") == "P-256"
                    and isinstance(item.get("kid"), str)
                    and item.get("use", "sig") == "sig"
                ):
                    # kid was checked above; all JWK members are still checked by the JWT parser.
                    loaded[cast("str", item["kid"])] = ECAlgorithm.from_jwk(
                        {key: value for key, value in item.items() if isinstance(key, str)}
                    )
        self.keys = loaded


def _claim_int(claims: dict[str, Any], key: str) -> int:
    value = claims.get(key)
    if type(value) is not int or value < 0:
        raise TokenRejected
    return value


def verify_access_token(
    token: str,
    *,
    discovery: Discovery,
    keys: KeyCache,
    client: httpx.Client,
) -> tuple[Binding, float]:
    """Verify an ES256 access JWT and return its binding and expiry."""

    if not token or len(token) > _MAX_TOKEN_LENGTH:
        raise TokenRejected
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "ES256" or not isinstance(kid, str):
            raise TokenRejected
        if kid not in keys.keys:
            keys.load(client, discovery)
        key = keys.keys.get(kid)
        if key is None:
            raise TokenRejected
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=["ES256"],
            issuer=discovery.issuer,
            audience=discovery.resource,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except (jwt.PyJWTError, httpx.HTTPError, ValueError) as exc:
        raise TokenRejected from exc
    sub = claims.get("sub")
    connection_id = claims.get("connection_id")
    if claims.get("client_id") != CLIENT_ID or not isinstance(sub, str) or not sub:
        raise TokenRejected
    if not isinstance(connection_id, str) or not connection_id:
        raise TokenRejected
    binding = Binding(
        sub=sub,
        connection_id=connection_id,
        owner_epoch=_claim_int(claims, "owner_epoch"),
        authorization_generation=_claim_int(claims, "authorization_generation"),
    )
    return binding, float(claims["exp"])


def parse_token_response(
    response: JsonResponse,
    *,
    discovery: Discovery,
    keys: KeyCache,
    client: httpx.Client,
) -> TokenSet:
    """Verify a successful token response; refresh token must be present."""

    body = response.body
    if response.status != 200 or body is None:
        raise TokenRejected
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    token_type = body.get("token_type")
    if not isinstance(access, str) or not isinstance(token_type, str):
        raise TokenRejected
    if token_type.lower() != "bearer":
        raise TokenRejected
    binding, expires_at = verify_access_token(access, discovery=discovery, keys=keys, client=client)
    if (
        not isinstance(refresh, str)
        or not refresh
        or len(refresh) > _MAX_REFRESH_TOKEN_LENGTH
        or refresh != refresh.strip()
    ):
        raise AuthorizationRequired("refresh_token_missing")
    return TokenSet(access, refresh, expires_at, binding)


def redeem_authorization_code(
    client: httpx.Client,
    discovery: Discovery,
    keys: KeyCache,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> TokenSet:
    """Exchange the authorization code; nothing persists before verification."""

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "resource": discovery.resource,
    }
    try:
        response = request_json(client, "POST", discovery.token_endpoint, form=form)
    except httpx.HTTPError as exc:
        raise AuthUnavailable("token_endpoint_unreachable") from exc
    try:
        return parse_token_response(response, discovery=discovery, keys=keys, client=client)
    except TokenRejected as exc:
        raise AuthUnavailable("token_response_rejected") from exc


def seconds_until(expires_at: float, *, now: float | None = None) -> float:
    return expires_at - (time.time() if now is None else now)
