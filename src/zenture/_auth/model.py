"""Shared immutable values of the native authorization core."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from zenture._mcp.contracts import McpEndpoint

CLIENT_ID = "zenture-client"
SCOPE = "openid mcp:run offline_access"
DEFAULT_ENDPOINT = "https://mcp.zenture.app/"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class Binding:
    """Non-secret identity facts an access token is bound to."""

    sub: str
    connection_id: str
    owner_epoch: int
    authorization_generation: int


@dataclass(frozen=True, slots=True)
class Target:
    """The MCP resource a login is scoped to."""

    resource: str

    @classmethod
    def from_endpoint(cls, endpoint: str | McpEndpoint | None) -> Target:
        value = DEFAULT_ENDPOINT if endpoint is None else endpoint
        url = value.url if isinstance(value, McpEndpoint) else McpEndpoint.from_value(value).url
        parsed = urlsplit(url)
        return cls(resource=f"{parsed.scheme}://{parsed.netloc}")


def is_secure_origin(url: str) -> bool:
    """Return whether the URL is https, or http to a loopback host."""

    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return parsed.hostname is not None
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


def origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _authority(url: str) -> tuple[str, str, int] | None:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port or _DEFAULT_PORTS[scheme]
    except ValueError:
        return None
    return scheme, parsed.hostname.lower(), port


def within_resource(url: str, resource: str) -> bool:
    """Whether ``url`` lies inside the resource: same origin and under the resource path."""

    wanted = _authority(resource)
    if wanted is None or _authority(url) != wanted:
        return False
    base = urlsplit(resource).path.rstrip("/")
    path = urlsplit(url).path
    return not base or path == base or path.startswith(base + "/")
