"""Bounded client-side MCP contracts over the canonical Run models."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from zenture._contract import PublicRunResponse
from zenture._contract.run_references import validate_run_cursor
from zenture.models import SDKBaseModel

if TYPE_CHECKING:
    from zenture._contract import ListRunEventsResponse

McpToolName = Literal[
    "run",
    "attach_artifact",
    "list_runs",
    "get_run",
    "cancel_run",
    "record_run_outcome",
]

PRODUCT_TOOL_NAMES: tuple[McpToolName, ...] = (
    "run",
    "attach_artifact",
    "list_runs",
    "get_run",
    "cancel_run",
    "record_run_outcome",
)

BearerTokenProvider = Callable[[], str]

_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{3,128}$")
_ARTIFACT_REF = re.compile(r"^art_[A-Za-z0-9_-]{3,128}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANONICAL_MCP_HOSTS = frozenset(
    {
        "mcp.zenture.app",
        "mcp-int.zenture.app",
        "localhost",
        "127.0.0.1",
        "::1",
    }
)


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """Validated hosted MCP endpoint without arbitrary-origin support."""

    url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _normalize_endpoint(self.url))

    @classmethod
    def from_value(cls, value: object) -> McpEndpoint:
        return cls(_normalize_endpoint(value))


def _normalize_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("MCP endpoint must be a non-empty URL")
    parsed = urlsplit(value.strip())
    hostname = parsed.hostname.lower() if parsed.hostname is not None else None
    if hostname not in _CANONICAL_MCP_HOSTS:
        raise ValueError("MCP endpoint host is not an approved zenture origin")
    is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (is_loopback and parsed.scheme == "http"):
        raise ValueError("MCP endpoint must use HTTPS except for loopback development")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP endpoint must not include user information")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP endpoint must not include query or fragment data")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("MCP endpoint port is invalid") from exc
    if not is_loopback and port not in {None, 443}:
        raise ValueError("hosted MCP endpoint must use the canonical HTTPS port")
    path = parsed.path or "/"
    if path != "/":
        raise ValueError("MCP endpoint must target the hosted root transport")
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


class McpArtifactRequest(SDKBaseModel):
    """Metadata passed to the MCP artifact tool; bytes stay out of JSON."""

    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=127)
    byte_size: int = Field(ge=1, le=10 * 1024 * 1024)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("content_hash", mode="before")
    @classmethod
    def _hash(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        normalized = value.strip().lower()
        if _SHA256.fullmatch(normalized) is None:
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        return normalized


class McpListRunsRequest(SDKBaseModel):
    """Bounded list filters matching the MCP tool input contract."""

    status: tuple[str, ...] = ()
    decision: tuple[str, ...] = ()
    profile: tuple[str, ...] = ()
    created_after: str | None = Field(default=None, max_length=64)
    created_before: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=5, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=512)

    @field_validator("status", "decision", "profile")
    @classmethod
    def _filters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8 or any(not item or len(item) > 64 for item in value):
            raise ValueError("MCP list filters are bounded to eight short values")
        return value

    @field_validator("cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        if value is None:
            return None
        return validate_run_cursor(value)


class McpGetRunRequest(SDKBaseModel):
    """Typed get/replay arguments for the MCP client."""

    run_id: str
    view: Literal["summary", "full"] = "summary"
    replay_cursor: str | None = Field(default=None, max_length=512)
    replay_limit: int = Field(default=50, ge=1, le=50)
    include_event_replay: bool = False

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if _RUN_ID.fullmatch(value) is None:
            raise ValueError("run_id is invalid")
        return value

    @field_validator("replay_cursor", mode="before")
    @classmethod
    def _cursor(cls, value: object) -> object:
        if value is None:
            return None
        return validate_run_cursor(value, field="replay_cursor")


class McpFindingAdjudication(SDKBaseModel):
    """Bounded typed finding feedback carried by the MCP outcome tool."""

    finding_ref: str = Field(min_length=1, max_length=128)
    outcome: Literal["confirmed", "rejected", "partially_valid", "not_sure"]


class McpOutcomeRequest(SDKBaseModel):
    """MCP outcome input mapped to the canonical outcome authority."""

    run_id: str
    outcome: Literal["used", "edited", "rejected", "escalated", "not_sure"]
    finding_adjudications: tuple[McpFindingAdjudication, ...] = Field(default=(), max_length=20)
    edited_artifact_ref: str | None = Field(default=None, min_length=7, max_length=132)

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if _RUN_ID.fullmatch(value) is None:
            raise ValueError("run_id is invalid")
        return value

    @field_validator("edited_artifact_ref")
    @classmethod
    def _artifact_ref(cls, value: str | None) -> str | None:
        if value is not None and _ARTIFACT_REF.fullmatch(value) is None:
            raise ValueError("edited_artifact_ref is invalid")
        return value

    @model_validator(mode="after")
    def _edited_requires_ref(self) -> McpOutcomeRequest:
        if (self.outcome == "edited") != (self.edited_artifact_ref is not None):
            raise ValueError("edited outcome requires edited_artifact_ref")
        return self


def validate_run_idempotency_key(value: object) -> str:
    """Validate the MCP Run identity without coercion or normalization."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError(
            "Run idempotency key must be 1-128 ASCII letters, digits, underscores or hyphens"
        )
    return value


class McpRunResponse(PublicRunResponse):
    """Run-only receipt; persist the named identity separately from model_dump."""

    idempotency_key: str = Field(exclude=True, repr=False)

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _identity(cls, value: object) -> str:
        return validate_run_idempotency_key(value)


@dataclass(frozen=True, slots=True)
class McpRunRead:
    """Canonical Run read plus optional explicit event replay."""

    run: PublicRunResponse
    event_replay: ListRunEventsResponse | None = None


__all__ = [
    "PRODUCT_TOOL_NAMES",
    "BearerTokenProvider",
    "McpArtifactRequest",
    "McpEndpoint",
    "McpFindingAdjudication",
    "McpGetRunRequest",
    "McpListRunsRequest",
    "McpOutcomeRequest",
    "McpRunRead",
    "McpToolName",
]
