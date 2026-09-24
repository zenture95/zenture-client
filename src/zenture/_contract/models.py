"""Internal contract models derived from the zenture Public API OpenAPI artifact."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from zenture._contract.run_references import (
    RUN_CURSOR_PATTERN,
    validate_run_cursor,
    validate_terminal_refs,
)
from zenture.models import SDKBaseModel

_ARTIFACT_REF_RE = re.compile(r"^art_[A-Za-z0-9_-]{3,128}$")
_SAFE_ARTIFACT_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./:-]*:[A-Za-z0-9_./:-]{1,479}$")
_UPLOAD_ID_RE = re.compile(r"^upload_[A-Za-z0-9_-]{8,128}$")


class _RunResponseModel(SDKBaseModel):
    """Strict known fields with tolerant additive response members."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class OperationStatus(StrEnum):
    """Public operation lifecycle statuses."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PublicErrorCode(StrEnum):
    """Stable public API error codes."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    VALIDATION_FAILED = "validation_failed"
    MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    INTERNAL_ERROR = "internal_error"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OPERATION_EXPIRED = "operation_expired"
    PROPOSAL_EXPIRED = "proposal_expired"
    PROPOSAL_HASH_MISMATCH = "proposal_hash_mismatch"
    ACCOUNT_REQUIRED = "account_required"
    ARTIFACT_REQUIRED = "artifact_required"
    ARTIFACT_AMBIGUOUS = "artifact_ambiguous"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    ARTIFACT_EXPIRED = "artifact_expired"
    ARTIFACT_TYPE_UNSUPPORTED = "artifact_type_unsupported"
    ARTIFACT_PROCESSING_UNAVAILABLE = "artifact_processing_unavailable"
    PREPARE_RATE_LIMITED = "prepare_rate_limited"
    PREPARE_CAPACITY_UNAVAILABLE = "prepare_capacity_unavailable"
    TOO_MANY_OUTSTANDING_RUNS = "too_many_outstanding_runs"
    CAPACITY_TEMPORARILY_UNAVAILABLE = "capacity_temporarily_unavailable"
    MAINTENANCE_ACTIVE = "maintenance_active"
    ENGINE_UNAVAILABLE_TIMEOUT = "engine_unavailable_timeout"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    RUN_NOT_FOUND = "run_not_found"
    RUN_TERMINAL = "run_terminal"
    CANCEL_CONFLICT = "cancel_conflict"
    BUDGET_CEILING_EXCEEDED = "budget_ceiling_exceeded"
    EXECUTION_FAILED = "execution_failed"


class ModelMode(StrEnum):
    """Public chat model execution modes."""

    SINGLE = "single"
    MULTI = "multi"


PublicOperationResultType = Literal["input_wizard", "chat", "evaluation", "unknown"]
ModelCostClass = Literal["economy", "standard", "premium", "frontier"]
ModelCapability = Annotated[str, Field(min_length=1, max_length=40)]
ModelId = Annotated[str, Field(min_length=1, max_length=140)]
UsageScope = Literal["api", "all"]


def _coerce_aware_datetime(value: object) -> object:
    if not isinstance(value, str):
        return value

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware.")
    return parsed


def _coerce_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(cast("list[object]", value))
    return value


class PublicError(SDKBaseModel):
    """Public API error object with a bounded stable code."""

    code: PublicErrorCode
    message: str = Field(min_length=1)

    @field_validator("code", mode="before")
    @classmethod
    def _coerce_code(cls, value: object) -> object:
        if isinstance(value, str):
            return PublicErrorCode(value)
        return value


class PublicErrorEnvelope(SDKBaseModel):
    """Public API error envelope."""

    error: PublicError
    request_id: str = Field(pattern=r"^req_[a-f0-9]{32}$")


class PublicAmountBilled(SDKBaseModel):
    """Display credit amount charged for a completed public API call."""

    amount: str = Field(pattern=r"^[0-9]+\.[0-9]{2}$")
    unit: Literal["credits"] = "credits"


class AvailableCreditAmount(_RunResponseModel):
    status: Literal["available"]
    amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$")
    unit: Literal["credits"]


class UnavailableCreditAmount(_RunResponseModel):
    status: Literal["unavailable"]
    reason_code: Literal[
        "invalid_scale", "missing_scale", "unsupported_scale",
        "legacy_unbound_scale", "billing_projection_unavailable",
    ]


CreditAmountProjectionV1 = Annotated[
    AvailableCreditAmount | UnavailableCreditAmount,
    Field(discriminator="status"),
]


class PrepareCreditsProjectionV1(_RunResponseModel):
    schema_version: Literal["run.prepare_credits_projection.v1"]
    estimated_credits: CreditAmountProjectionV1
    maximum_credits: CreditAmountProjectionV1


class TerminalBillingProjectionV1(_RunResponseModel):
    schema_version: Literal["run.terminal_billing_projection.v1"] = "run.terminal_billing_projection.v1"
    status: Literal["pending", "settled", "released", "unavailable"]
    final_credits: CreditAmountProjectionV1 | None

    @model_validator(mode="after")
    def _status_matches_amount(self) -> TerminalBillingProjectionV1:
        if self.status == "pending" and self.final_credits is not None:
            raise ValueError("pending billing must not expose final Credits")
        if self.status != "pending" and self.final_credits is None:
            raise ValueError("terminal billing requires final Credits")
        return self


class PublicOperationResult(SDKBaseModel):
    """Bounded safe-reference operation result.

    Chat operations expose ids only, not prompt or answer bodies. Use
    ``chat_id`` for follow-up turns, ``turn_id`` to locate the turn, and
    ``model_response_id``/``model_response_ids`` as AI-answer ids for internal
    zenture chat evaluation.
    """

    result_type: PublicOperationResultType
    amount_billed: PublicAmountBilled | None = None
    chat_id: str | None = Field(default=None, pattern=r"^chat_[A-Za-z0-9_-]{3,128}$")
    completed_at: AwareDatetime | None = None
    evaluation_id: str | None = Field(default=None, pattern=r"^eval_[A-Za-z0-9_-]{3,128}$")
    input_wizard_id: str | None = None
    model_response_id: str | None = Field(default=None, max_length=140)
    model_response_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=3)
    optimized_prompt: str | None = None
    resource_id: str | None = None
    score: float | None = Field(default=None, ge=0, le=100)
    status: OperationStatus | str | None = None
    target_kind: Literal["chat_model_response", "api_external_chat_message"] | None = None
    turn_id: str | None = Field(default=None, max_length=140)
    wizard_session_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_nested_result_projection(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = cast("dict[str, Any]", value)
        for key in ("chat", "evaluation", "input_wizard"):
            nested = result.get(key)
            if isinstance(nested, dict):
                projection = dict(cast("dict[str, Any]", nested))
                projection.setdefault("result_type", key)
                return projection
        return result

    @field_validator("model_response_ids", mode="before")
    @classmethod
    def _coerce_model_response_ids(cls, value: object) -> object:
        return _coerce_tuple(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _coerce_completed_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return OperationStatus(value)
            except ValueError:
                return value
        return value


class PublicOperationError(SDKBaseModel):
    """Terminal operation error with domain-specific operation code."""

    code: str
    message: str


class PublicOperationResponse(SDKBaseModel):
    """Operation response returned by mutating async routes."""

    operation_id: str = Field(pattern=r"^op_[A-Za-z0-9_-]{3,128}$")
    status: OperationStatus
    result: PublicOperationResult | None = None
    error: PublicOperationError | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> object:
        if isinstance(value, str):
            return OperationStatus(value)
        return value


class PublicChatSummary(SDKBaseModel):
    """Public chat summary returned by chat read routes."""

    chat_id: str = Field(pattern=r"^chat_[A-Za-z0-9_-]{3,128}$")
    created_at: AwareDatetime | None
    title: str | None = None
    updated_at: AwareDatetime | None = None

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _coerce_datetimes(cls, value: object) -> object:
        return _coerce_aware_datetime(value)


class PublicChatTurn(SDKBaseModel):
    """Public chat turn returned by message routes.

    ``model_response_id`` is the first/single AI-answer id for the turn.
    ``model_response_ids`` contains all answer ids for multi-model turns. Pass
    the relevant id to ``client.evaluations`` when evaluating a zenture chat
    answer.
    """

    turn_id: str
    created_at: AwareDatetime | None
    user_message: str
    model_answer: str
    model_response_id: str | None = Field(default=None, max_length=140)
    model_response_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=3)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("model_response_ids", mode="before")
    @classmethod
    def _coerce_model_response_ids(cls, value: object) -> object:
        return _coerce_tuple(value)


def _empty_chat_turns() -> tuple[PublicChatTurn, ...]:
    return ()


class PublicChatCollectionResponse(SDKBaseModel):
    """Public chat list response."""

    chats: tuple[PublicChatSummary, ...]
    next_cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("chats", mode="before")
    @classmethod
    def _coerce_chats(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicChatResponse(SDKBaseModel):
    """Public chat detail response."""

    chat: PublicChatSummary
    latest_turns: tuple[PublicChatTurn, ...] = Field(default_factory=_empty_chat_turns)

    @field_validator("latest_turns", mode="before")
    @classmethod
    def _coerce_latest_turns(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicChatMessagesResponse(SDKBaseModel):
    """Public chat messages response."""

    chat_id: str = Field(pattern=r"^chat_[A-Za-z0-9_-]{3,128}$")
    turns: tuple[PublicChatTurn, ...]
    next_cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("turns", mode="before")
    @classmethod
    def _coerce_turns(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicEvaluationKpiResult(SDKBaseModel):
    """Bounded KPI result row returned by evaluation details."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, protected_namespaces=())

    model_id: str = Field(min_length=1, max_length=140)
    kpi_key: str = Field(min_length=1, max_length=120)
    value: int | float | None = None
    analysis: str | None = Field(default=None, max_length=4000)


class PublicEvaluationSourceResult(SDKBaseModel):
    """Bounded source verification row returned by evaluation details."""

    status: Literal["available", "limited", "unavailable", "unverified"]
    retrievalStatus: Literal["available", "limited", "unavailable", "unverified"] | None = None
    url: str | None = Field(default=None, max_length=2048)
    originalUrl: str | None = Field(default=None, max_length=2048)
    normalizedUrl: str | None = Field(default=None, max_length=2048)
    resolvedUrl: str | None = Field(default=None, max_length=2048)
    hostname: str | None = Field(default=None, max_length=255)
    securityLabel: str | None = Field(default=None, max_length=40)
    accessibilityScore: int | None = Field(default=None, ge=0, le=100)
    httpStatus: int | None = Field(default=None, ge=100, le=599)
    responseTimeMs: int | None = Field(default=None, ge=0)
    author: str | None = Field(default=None, max_length=200)
    publishedAt: str | None = Field(default=None, max_length=80)
    verdict: str | None = Field(default=None, max_length=80)


class PublicEvaluationResponse(SDKBaseModel):
    """Public evaluation detail response."""

    evaluation_id: str = Field(pattern=r"^eval_[A-Za-z0-9_-]{3,128}$")
    status: str
    score: float | None = None
    created_at: AwareDatetime | None = None
    amount_billed: PublicAmountBilled | None = None
    zenture_summary: dict[str, str] | None = None
    zenture_suggestion: dict[str, str] | None = None
    zenture_kpi_details: dict[str, Any] | None = None
    results: tuple[PublicEvaluationKpiResult, ...] | None = None
    sources: dict[str, tuple[PublicEvaluationSourceResult, ...]] | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("results", mode="before")
    @classmethod
    def _coerce_results(cls, value: object) -> object:
        return _coerce_tuple(value)

    @field_validator("sources", mode="before")
    @classmethod
    def _coerce_sources(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            str(key): _coerce_tuple(items)
            for key, items in cast("dict[str, object]", value).items()
        }


class PublicEvaluationCollectionResponse(SDKBaseModel):
    """Public evaluation list response."""

    evaluations: tuple[PublicEvaluationResponse, ...]
    next_cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("evaluations", mode="before")
    @classmethod
    def _coerce_evaluations(cls, value: object) -> object:
        return _coerce_tuple(value)


class RunProfile(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    DETAILED = "detailed"


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_DEPENDENCY = "waiting_for_dependency"
    PARTIALLY_COMPLETE = "partially_complete"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    BUDGET_EXHAUSTED = "budget_exhausted"


class PublicDecision(StrEnum):
    READY = "ready"
    REVISE = "revise"
    HUMAN_REVIEW = "human_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RunTextArtifact(SDKBaseModel):
    type: Literal["text"]
    value: str = Field(min_length=1, max_length=50_000)

    @field_validator("value")
    @classmethod
    def _text(cls, value: str) -> str:
        if value.casefold().startswith(("http://", "https://", "data:", "file:")):
            raise ValueError("text artifact must contain selected text, not a URL or encoded blob")
        return value


class RunReferenceArtifact(SDKBaseModel):
    type: Literal["zenture_ref"]
    value: str = Field(min_length=7, max_length=132, pattern=r"^art_[A-Za-z0-9_-]{3,128}$")


RunArtifact = Annotated[RunTextArtifact | RunReferenceArtifact, Field(discriminator="type")]


class ArtifactUploadRequest(SDKBaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=127)
    byte_size: int = Field(ge=1, le=10 * 1024 * 1024)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    upload_id: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=r"^upload_[A-Za-z0-9_-]{8,128}$"
    )


class ArtifactUploadResponse(SDKBaseModel):
    upload_id: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=r"^upload_[A-Za-z0-9_-]{8,128}$"
    )
    artifact_ref: str | None = Field(
        default=None,
        min_length=7,
        max_length=132,
        pattern=r"^art_[A-Za-z0-9_-]{3,128}$",
    )
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: int = Field(ge=1, le=10 * 1024 * 1024)
    content_type: str = Field(min_length=1, max_length=127)
    expires_at: AwareDatetime | None = None

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)


class SignedUploadResponse(SDKBaseModel):
    upload_id: str = Field(min_length=8, max_length=128, pattern=r"^upload_[A-Za-z0-9_-]{8,128}$")
    expires_at: AwareDatetime
    upload_url: str | None = Field(default=None, max_length=2048)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)


class PrepareKnowledgeRunRequest(SDKBaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    artifact: RunArtifact
    profile: RunProfile = RunProfile.STANDARD

    @field_validator("task")
    @classmethod
    def _task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must not be blank")
        return value

    @field_validator("profile", mode="before")
    @classmethod
    def _profile(cls, value: object) -> object:
        return RunProfile(value) if isinstance(value, str) else value


class CreateRunRequest(SDKBaseModel):
    proposal_id: UUID
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("proposal_id", mode="before")
    @classmethod
    def _proposal_id(cls, value: object) -> object:
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


class FindingAdjudication(SDKBaseModel):
    finding_ref: str = Field(min_length=1, max_length=128)
    outcome: Literal["confirmed", "rejected", "partially_valid", "not_sure"]


class RecordRunOutcomeRequest(SDKBaseModel):
    outcome: Literal["used", "edited", "rejected", "escalated", "not_sure"]
    finding_adjudications: list[FindingAdjudication] = Field(
        default_factory=lambda: list[FindingAdjudication](), max_length=20
    )
    edited_artifact_ref: str | None = Field(
        default=None,
        min_length=7,
        max_length=132,
        pattern=r"^art_[A-Za-z0-9_-]{3,128}$",
    )

    @model_validator(mode="after")
    def _edited_artifact_matches_outcome(self) -> Self:
        if (self.outcome == "edited") != (self.edited_artifact_ref is not None):
            raise ValueError("edited outcome requires an edited_artifact_ref")
        return self


class PublicTaskContractSummary(_RunResponseModel):
    work_type: str = Field(min_length=1, max_length=64)
    summary_ref: str = Field(min_length=1, max_length=256)
    requirement_count: int = Field(ge=0, le=128)


class PrepareKnowledgeRunResponse(SDKBaseModel):
    proposal_id: UUID
    proposal_version: int = Field(ge=1)
    expires_at: AwareDatetime
    inferred_work_type: str = Field(min_length=1, max_length=64)
    task_contract_summary: PublicTaskContractSummary
    planned_checks: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    unavailable_checks: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    expected_duration_seconds: int = Field(ge=1, le=86_400)
    estimated_credits: CreditAmountProjectionV1 | str | None = Field(default=None, description="Deprecated legacy scalar; prefer billing_projection. ")
    maximum_credits: CreditAmountProjectionV1 | str | None = Field(default=None, description="Deprecated legacy scalar; prefer billing_projection. ")
    billing_projection: PrepareCreditsProjectionV1 | None = None
    guest_slot_cost: int | None = Field(default=None, ge=0, le=1)
    start_admissible: bool
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("proposal_id", mode="before")
    @classmethod
    def _proposal_id(cls, value: object) -> object:
        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("planned_checks", "unavailable_checks", mode="before")
    @classmethod
    def _checks(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicCapabilityCoverage(_RunResponseModel):
    available_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    unavailable_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    limitation_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @field_validator("available_refs", "unavailable_refs", "limitation_refs", mode="before")
    @classmethod
    def _refs(cls, value: object) -> object:
        return _coerce_tuple(value)


_SAFE_RESULT_FORBIDDEN = re.compile(
    r"(?:https?://|www\.|```|`|system\s+prompt|developer\s+prompt|chain[- ]of[- ]thought|"
    r"prompt\s+injection|api[_ -]?key|access[_ -]?token|bearer\s+|secret|password|private\s+key)",
    re.IGNORECASE,
)


def _safe_result_text(value: object, *, maximum: int, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} bound invalid")
    if _SAFE_RESULT_FORBIDDEN.search(normalized) or re.search(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", normalized) or "<" in normalized or ">" in normalized:
        raise ValueError(f"{field_name} contains unsafe content")
    return normalized


class SafeResultFinding(_RunResponseModel):
    finding_ref: str = Field(max_length=256, pattern=r"^finding:[A-Za-z0-9_./:-]{1,247}$")
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2000)
    explanation: str = Field(min_length=1, max_length=4000)
    confidence: Literal["low", "medium", "high"]
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _refs(cls, value: object) -> object:
        return _coerce_tuple(value)

    @field_validator("title", "summary", "explanation")
    @classmethod
    def _safe_text(cls, value: object, info: Any) -> str:
        return _safe_result_text(value, maximum={"title": 256, "summary": 2000, "explanation": 4000}[info.field_name], field_name=info.field_name)


class SafeResultEvidenceSummary(_RunResponseModel):
    evidence_ref: str = Field(max_length=256, pattern=r"^evidence:[A-Za-z0-9_./:-]{1,246}$")
    summary: str = Field(min_length=1, max_length=1000)
    assessment: Literal["supported", "contradicted", "uncertain"]
    source_label: str | None = Field(default=None, max_length=256)

    @field_validator("summary", "source_label")
    @classmethod
    def _safe_text(cls, value: object, info: Any) -> str | None:
        if value is None:
            return None
        return _safe_result_text(value, maximum=1000 if info.field_name == "summary" else 256, field_name=info.field_name)


class SafeResultLimitation(_RunResponseModel):
    limitation_ref: str = Field(max_length=256, pattern=r"^limitation:[A-Za-z0-9_./:-]{1,244}$")
    summary: str = Field(min_length=1, max_length=512)

    @field_validator("summary")
    @classmethod
    def _safe_text(cls, value: object) -> str:
        return _safe_result_text(value, maximum=512, field_name="summary")


class SafeResultDecision(_RunResponseModel):
    outcome: Literal["ready", "revise", "human_review", "insufficient_evidence"]
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    next_action: str = Field(min_length=1, max_length=256)

    @field_validator("next_action")
    @classmethod
    def _safe_action(cls, value: object) -> str:
        return _safe_result_text(value, maximum=256, field_name="next_action")


class SafeResultCoverageItem(_RunResponseModel):
    check_intent_ref: str | None = Field(default=None, max_length=256, pattern=r"^check-intent:[A-Za-z0-9_./:-]{1,243}$")
    requirement_ref: str | None = Field(default=None, max_length=256, pattern=r"^(?:review-requirement|requirement):[A-Za-z0-9_./:-]{1,237}$")
    status: Literal["performed", "skipped_optional", "unavailable", "failed", "degraded"]
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    blocks_ready: bool
    artifact_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _artifact_refs(cls, value: object) -> object:
        values = _coerce_tuple(value)
        if isinstance(values, tuple) and any(not isinstance(item, str) or _SAFE_ARTIFACT_REF_RE.fullmatch(item) is None for item in values):
            raise ValueError("artifact_refs must contain compiler-safe references")
        return values

    @model_validator(mode="after")
    def _one_subject(self) -> SafeResultCoverageItem:
        if (self.check_intent_ref is None) == (self.requirement_ref is None):
            raise ValueError("coverage item must identify exactly one subject")
        return self


class SafeResultCoverage(_RunResponseModel):
    coverage_ref: str = Field(max_length=256, pattern=r"^coverage:[A-Za-z0-9_./:-]{1,246}$")
    items: tuple[SafeResultCoverageItem, ...] = Field(min_length=1, max_length=256)

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, value: object) -> object:
        return _coerce_tuple(value)


class ComposedSafeResultContentV1(_RunResponseModel):
    schema_version: Literal["run.composed_safe_result_content.v1"]
    summary: str = Field(min_length=1, max_length=4000)
    findings: tuple[SafeResultFinding, ...] = Field(default_factory=tuple, max_length=64)
    evidence_summaries: tuple[SafeResultEvidenceSummary, ...] = Field(default_factory=tuple, max_length=64)
    limitations: tuple[SafeResultLimitation, ...] = Field(default_factory=tuple, max_length=64)
    decision: SafeResultDecision
    coverage: SafeResultCoverage
    decision_ref: str = Field(max_length=256, pattern=r"^decision:[A-Za-z0-9_./:-]{1,246}$")
    coverage_ref: str = Field(max_length=256, pattern=r"^coverage:[A-Za-z0-9_./:-]{1,246}$")
    insight_ref: str = Field(max_length=256, pattern=r"^insight:[A-Za-z0-9_./:-]{1,247}$")

    @field_validator("findings", "evidence_summaries", "limitations", mode="before")
    @classmethod
    def _collections(cls, value: object) -> object:
        return _coerce_tuple(value)

    @field_validator("summary")
    @classmethod
    def _safe_summary(cls, value: object) -> str:
        return _safe_result_text(value, maximum=4000, field_name="summary")


class SafeResultContentAvailableV1(_RunResponseModel):
    status: Literal["available"]
    content: ComposedSafeResultContentV1


class SafeResultContentUnavailableV1(_RunResponseModel):
    status: Literal["unavailable"]
    reason_code: Literal["legacy_result", "projection_incomplete", "result_content_unavailable"]


SafeResultContentProjectionV1 = Annotated[
    SafeResultContentAvailableV1 | SafeResultContentUnavailableV1,
    Field(discriminator="status"),
]


class PublicQueueProjection(_RunResponseModel):
    queue_reason: str = Field(min_length=1, max_length=128)
    jobs_ahead: int = Field(ge=0, le=50)
    estimated_start_seconds: dict[str, int] | None = None
    estimated_completion_seconds: dict[str, int] | None = None
    estimate_as_of: AwareDatetime | None = None

    @field_validator("estimate_as_of", mode="before")
    @classmethod
    def _estimate_timestamp(cls, value: object) -> object:
        return _coerce_aware_datetime(value)


class PublicRunResponse(_RunResponseModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{3,128}$")
    generation: int = Field(ge=1)
    family: Literal["knowledge"] = "knowledge"
    work_type: str = Field(min_length=1, max_length=64)
    profile: RunProfile
    status: RunStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    deadline_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    queue: PublicQueueProjection | None = None
    task_contract_summary: PublicTaskContractSummary | None = None
    capability_coverage: PublicCapabilityCoverage | None = None
    acceptance_decision: PublicDecision | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    next_action: str | None = Field(default=None, max_length=128)
    run_insight_ref: str | None = Field(default=None, max_length=256)
    artifact_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    usage_summary: dict[str, int] = Field(default_factory=dict, max_length=8, description="Deprecated additive compatibility field; use billing_projection.")
    billing_summary: dict[str, str] = Field(default_factory=dict, max_length=8, description="Deprecated additive compatibility field; use billing_projection.")
    billing_projection: TerminalBillingProjectionV1 | None = None
    limitations: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    safe_result_content: SafeResultContentProjectionV1 | None = None
    cancellation_requested: bool = False
    event_cursor: str | None = Field(
        default=None, max_length=512, pattern=r"^[A-Za-z0-9._~-]{1,512}$"
    )

    @field_validator("event_cursor", mode="before")
    @classmethod
    def _event_cursor(cls, value: object) -> object:
        if value is None:
            return None
        return validate_run_cursor(value, field="event_cursor")

    @field_validator(
        "created_at", "updated_at", "started_at", "completed_at", "deadline_at", mode="before"
    )
    @classmethod
    def _timestamps(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("profile", mode="before")
    @classmethod
    def _profile(cls, value: object) -> object:
        return RunProfile(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: object) -> object:
        return RunStatus(value) if isinstance(value, str) else value

    @field_validator("acceptance_decision", mode="before")
    @classmethod
    def _acceptance_decision(cls, value: object) -> object:
        return PublicDecision(value) if isinstance(value, str) else value

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _artifact_refs(cls, value: object) -> object:
        values = _coerce_tuple(value)
        if not isinstance(values, tuple):
            return values
        refs = cast("tuple[object, ...]", values)
        if any(not isinstance(ref, str) or _ARTIFACT_REF_RE.fullmatch(ref) is None for ref in refs):
            raise ValueError("artifact reference is invalid")
        return refs

    @field_validator("limitations", mode="before")
    @classmethod
    def _limitations(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicRunListItem(_RunResponseModel):
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{3,128}$")
    status: RunStatus
    decision: PublicDecision | None = None
    task_summary_ref: str | None = None
    profile: RunProfile
    created_at: AwareDatetime
    updated_at: AwareDatetime
    deadline_at: AwareDatetime | None = None

    @field_validator("created_at", "updated_at", "deadline_at", mode="before")
    @classmethod
    def _timestamps(cls, value: object) -> object:
        return _coerce_aware_datetime(value)

    @field_validator("profile", mode="before")
    @classmethod
    def _profile(cls, value: object) -> object:
        return RunProfile(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value: object) -> object:
        return RunStatus(value) if isinstance(value, str) else value

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, value: object) -> object:
        return PublicDecision(value) if isinstance(value, str) else value


class ListRunsResponse(_RunResponseModel):
    runs: tuple[PublicRunListItem, ...]
    has_more: bool
    next_cursor: str | None = Field(
        default=None, max_length=512, pattern=r"^[A-Za-z0-9._~-]{1,512}$"
    )

    @field_validator("next_cursor", mode="before")
    @classmethod
    def _next_cursor(cls, value: object) -> object:
        if value is None:
            return None
        return validate_run_cursor(value, field="next_cursor")

    @field_validator("runs", mode="before")
    @classmethod
    def _runs(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicRunEvent(_RunResponseModel):
    type: Literal["run.event"]
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{3,128}$")
    sequence: int = Field(ge=0)
    status: Literal[
        "queued",
        "preparing",
        "running",
        "completed",
        "degraded",
        "failed",
        "cancelled",
        "expired",
        "cancel_requested",
        "budget_exhausted",
    ]
    message_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    jobs_ahead: int | None = Field(default=None, ge=0, le=50)
    estimated_start_seconds: dict[str, int] | None = None
    estimated_completion_seconds: dict[str, int] | None = None
    terminal_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    event_cursor: str = Field(min_length=1, max_length=512, pattern=RUN_CURSOR_PATTERN.pattern)

    @field_validator("event_cursor", mode="before")
    @classmethod
    def _event_cursor(cls, value: object) -> object:
        return validate_run_cursor(value, field="event_cursor")

    @field_validator("terminal_refs", mode="before")
    @classmethod
    def _terminal_refs(cls, value: object) -> object:
        return validate_terminal_refs(value)


class PublicRunHeartbeat(_RunResponseModel):
    type: Literal["run.heartbeat"]
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{3,128}$")
    sequence: int = Field(ge=0)


class PublicRunStreamError(_RunResponseModel):
    type: Literal["run.error"]
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{3,128}$")
    sequence: int = Field(ge=0)
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_.:-]+$")
    message: str = Field(min_length=1, max_length=512)
    retryable: bool
    terminal: bool


PublicRunStreamMessage = PublicRunEvent | PublicRunHeartbeat | PublicRunStreamError


class ListRunEventsResponse(_RunResponseModel):
    events: tuple[PublicRunEvent, ...]
    has_more: bool
    next_cursor: str | None = Field(
        default=None, max_length=512, pattern=r"^[A-Za-z0-9._~-]{1,512}$"
    )

    @field_validator("next_cursor", mode="before")
    @classmethod
    def _next_cursor(cls, value: object) -> object:
        if value is None:
            return None
        return validate_run_cursor(value, field="next_cursor")

    @field_validator("events", mode="before")
    @classmethod
    def _events(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicWalletResponse(SDKBaseModel):
    """Public wallet status response."""

    plan: str
    status: str
    credits_available: PublicAmountBilled
    current_period_end: AwareDatetime | None = None

    @field_validator("current_period_end", mode="before")
    @classmethod
    def _coerce_current_period_end(cls, value: object) -> object:
        return _coerce_aware_datetime(value)


class PublicUsageResponse(SDKBaseModel):
    """Public usage response."""

    scope: UsageScope
    operation_count: int = Field(ge=0)


class RouteLimitProjection(SDKBaseModel):
    """Public per-route limits projection."""

    auth_mode: str
    scopes: tuple[str, ...]
    cost_class: str
    idempotency_required: bool
    cors_policy: str
    max_body_bytes: int
    rate_limit_per_minute: int

    @field_validator("scopes", mode="before")
    @classmethod
    def _coerce_scopes(cls, value: object) -> object:
        return _coerce_tuple(value)


class LimitsResponse(SDKBaseModel):
    """Public limits response."""

    routes: dict[str, RouteLimitProjection]
    operation_statuses: tuple[str, ...]

    @field_validator("operation_statuses", mode="before")
    @classmethod
    def _coerce_operation_statuses(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicModel(SDKBaseModel):
    """Public model descriptor returned by model discovery."""

    id: ModelId
    display_name: str = Field(min_length=1, max_length=120)
    modes: tuple[ModelMode, ...] = Field(min_length=1, max_length=2)
    capabilities: tuple[ModelCapability, ...] = Field(max_length=12)
    is_default: bool
    is_available: bool
    cost_class: ModelCostClass
    provider_display_name: str | None = Field(default=None, min_length=1, max_length=80)
    max_input_tokens: int | None = Field(default=None, ge=1)

    @field_validator("modes", mode="before")
    @classmethod
    def _coerce_modes(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(ModelMode(item) for item in cast("list[str]", value))
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_capabilities(cls, value: object) -> object:
        return _coerce_tuple(value)


class PublicModelListResponse(SDKBaseModel):
    """Public model discovery response."""

    models: tuple[PublicModel, ...]

    @field_validator("models", mode="before")
    @classmethod
    def _coerce_models(cls, value: object) -> object:
        return _coerce_tuple(value)


class ChatRequest(SDKBaseModel):
    """Request body for `POST /v1/chat`.

    Omit ``chat_id`` to start a new chat. Pass a previous ``chat_id`` to append
    a follow-up turn.
    """

    message: str = Field(min_length=1, max_length=20000)
    chat_id: str | None = Field(default=None, min_length=8, max_length=140)
    mode: ModelMode = ModelMode.SINGLE
    model: ModelId | None = None
    models: tuple[ModelId, ...] | None = Field(default=None, min_length=1, max_length=3)

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return ModelMode(value)
        return value

    @field_validator("models", mode="before")
    @classmethod
    def _coerce_models(cls, value: object) -> object:
        return _coerce_tuple(value)

    @model_validator(mode="after")
    def _validate_model_mode(self) -> Self:
        if self.models is not None and len(set(self.models)) != len(self.models):
            raise ValueError("models must be unique.")
        if self.mode is ModelMode.SINGLE:
            if self.models is not None:
                raise ValueError("single mode does not accept models.")
            return self
        if self.model is not None:
            raise ValueError("multi mode does not accept model.")
        if self.models is None:
            raise ValueError("multi mode requires models.")
        return self


class OperationRunResult(SDKBaseModel):
    """SDK operation run wrapper returned by high-level polling helpers."""

    operation_id: str = Field(pattern=r"^op_[A-Za-z0-9_-]{3,128}$")
    status: OperationStatus
    result: PublicOperationResult | None = None
    error: PublicOperationError | None = None
    chat_turn: PublicChatTurn | None = None
    evaluation: PublicEvaluationResponse | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)
    last_request_id: str | None = None


class EvaluateRequest(SDKBaseModel):
    """Request body for `POST /v1/evaluate`.

    Always send ``user_message`` and ``ai_answer``. For an existing zenture chat
    answer, include the answer's ``model_response_id`` and optionally
    ``chat_id``/``turn_id``. If ``chat_id`` or ``turn_id`` is supplied,
    ``model_response_id`` is required. For an external answer, omit zenture chat
    ids and optionally include caller-owned ``external_id``/``metadata`` for
    correlation.
    """

    user_message: str = Field(min_length=1, max_length=40000)
    ai_answer: str = Field(min_length=1, max_length=40000)
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, object] = Field(default_factory=dict, max_length=50)
    chat_id: str | None = Field(default=None, min_length=8, max_length=140)
    model_response_id: str | None = Field(default=None, min_length=1, max_length=140)
    turn_id: str | None = Field(default=None, min_length=1, max_length=140)

    @model_validator(mode="after")
    def _validate_evaluation_target(self) -> Self:
        if self.model_response_id is not None and not _raw_optional_id(
            self.model_response_id, prefix="response_"
        ):
            raise ValueError("model_response_id must reference a zenture chat response")
        if (self.chat_id or self.turn_id) and not self.model_response_id:
            raise ValueError("model_response_id is required when chat_id or turn_id is provided")
        return self


def _raw_optional_id(value: str | None, *, prefix: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return raw.removeprefix(prefix)


class InputWizardRequest(SDKBaseModel):
    """Request body for `POST /v1/input-wizard`."""

    mode: Literal["prompt_improvement"]
    prompt: str = Field(min_length=1, max_length=20000)
