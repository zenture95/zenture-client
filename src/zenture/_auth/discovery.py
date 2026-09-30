"""Protected-resource and authorization-server discovery with exact-match checks."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from zenture._auth.errors import AuthUnavailable
from zenture._auth.http import request_json
from zenture._auth.model import Target, is_secure_origin, origin

_PRM_PATH = "/.well-known/oauth-protected-resource"
_AS_PATH = "/.well-known/oauth-authorization-server"


@dataclass(frozen=True, slots=True)
class Discovery:
    """Verified endpoints of the issuer that protects one MCP resource."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    device_authorization_endpoint: str | None


def _contract_error() -> AuthUnavailable:
    return AuthUnavailable("discovery_contract_violation", next_action="check_endpoint")


def _endpoint(metadata: dict[str, object], key: str, issuer: str, *, required: bool) -> str | None:
    value = metadata.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not is_secure_origin(value) or origin(value) != issuer:
        raise _contract_error()
    if urlsplit(value).query:
        raise _contract_error()
    return value


def _issuer_metadata_url(issuer: str) -> str:
    parsed = urlsplit(issuer)
    path = parsed.path.rstrip("/")
    return f"{origin(issuer)}{_AS_PATH}{path}"


def discover(client: httpx.Client, target: Target) -> Discovery:
    """Discover and verify the issuer; raises before any browser can open."""

    resource = target.resource
    try:
        prm = request_json(client, "GET", f"{resource}{_PRM_PATH}")
        if prm.status != 200 or prm.body is None:
            raise _contract_error()
        if prm.body.get("resource") != resource:
            raise _contract_error()
        servers = prm.body.get("authorization_servers")
        if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
            raise _contract_error()
        issuer = servers[0]
        if not is_secure_origin(issuer) or urlsplit(issuer).query or issuer.endswith("/"):
            raise _contract_error()
        meta = request_json(client, "GET", _issuer_metadata_url(issuer))
    except httpx.HTTPError as exc:
        raise AuthUnavailable("discovery_unreachable") from exc
    if meta.status != 200 or meta.body is None:
        raise _contract_error()
    metadata = meta.body
    if metadata.get("issuer") != issuer:
        raise _contract_error()
    methods = metadata.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise _contract_error()
    if metadata.get("authorization_response_iss_parameter_supported") is not True:
        raise _contract_error()
    authorization = _endpoint(metadata, "authorization_endpoint", origin(issuer), required=True)
    token = _endpoint(metadata, "token_endpoint", origin(issuer), required=True)
    jwks = _endpoint(metadata, "jwks_uri", origin(issuer), required=True)
    device = _endpoint(metadata, "device_authorization_endpoint", origin(issuer), required=False)
    if authorization is None or token is None or jwks is None:
        raise _contract_error()
    return Discovery(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=authorization,
        token_endpoint=token,
        jwks_uri=jwks,
        device_authorization_endpoint=device,
    )
