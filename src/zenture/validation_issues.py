"""Filepath: validation_issues.py
Purpose: Own strict, immutable public validation issue values and bounded parsing.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ValidationCategory = Literal[
    "required",
    "invalid_type",
    "invalid_enum",
    "invalid_format",
    "too_short",
    "too_long",
    "out_of_range",
    "invalid_combination",
    "unknown_field",
]
ValidationUnit = Literal["unicode_code_points", "items", "bytes", "seconds"]


def omit_null_optional(schema: dict[str, Any]) -> None:
    """The public optional contract advertises omission, never explicit null."""
    for field in schema.get("properties", {}).values():
        omit_null_field(field)


def omit_null_field(field: dict[str, Any]) -> None:
    variants = field.get("anyOf")
    if variants:
        non_null = [variant for variant in variants if variant.get("type") != "null"]
        if len(non_null) == 1:
            field.pop("anyOf")
            field.update(non_null[0])
            field.pop("default", None)


def constraints_schema(schema: dict[str, Any]) -> None:
    omit_null_optional(schema)
    schema["minProperties"] = 1


class ValidationConstraints(BaseModel):
    """Static public schema facts; never submitted values or validator context."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        json_schema_extra=constraints_schema,
    )
    min_length: int | None = Field(
        None, ge=0, description="Declared minimum string length or item count."
    )
    max_length: int | None = Field(
        None, ge=0, description="Declared maximum string length or item count."
    )
    minimum: float | None = Field(None, description="Declared inclusive numeric lower bound.")
    maximum: float | None = Field(None, description="Declared inclusive numeric upper bound.")
    allowed_values: tuple[Annotated[str, Field(max_length=128)], ...] | None = Field(
        None,
        min_length=1,
        max_length=32,
        description="Finite values declared by the public field schema.",
    )
    unit: ValidationUnit | None = Field(None, description="Unit of a declared public limit.")

    @field_validator("allowed_values", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(cast("list[object]", value)) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _nonempty(self) -> Self:
        if not self.model_dump(exclude_none=True):
            raise ValueError("constraints must contain a public fact")
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("optional constraints must be omitted")
        return self


class ValidationIssue(BaseModel):
    """Safe JSON Pointer and stable validation category."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, json_schema_extra=omit_null_optional
    )
    path: str = Field(
        max_length=256,
        pattern=r"^(?:/(?:[^~/]|~[01])*)*$",
        description="JSON Pointer to a public field or its nearest known parent.",
    )
    category: ValidationCategory = Field(description="Stable validation failure category.")
    constraints: ValidationConstraints | None = Field(
        None,
        description="Optional static public schema facts; no submitted values or validator context.",
    )

    @model_validator(mode="after")
    def _omitted_constraints(self) -> Self:
        if "constraints" in self.model_fields_set and self.constraints is None:
            raise ValueError("optional constraints must be omitted")
        return self


def parse_issues(value: object) -> tuple[ValidationIssue, ...]:
    """Discard invalid optional details without changing the coarse failure."""
    if not isinstance(value, list | tuple):
        return ()
    entries = cast("list[object] | tuple[object, ...]", value)
    if not 1 <= len(entries) <= 20:
        return ()
    result: list[ValidationIssue] = []
    for raw in entries:
        try:
            item = raw if isinstance(raw, ValidationIssue) else ValidationIssue.model_validate(raw)
        except ValidationError:
            continue
        if item not in result:
            result.append(item)
    return tuple(result)


def safe_issues(
    value: object, *, channel: Literal["rest", "mcp"] = "rest"
) -> tuple[ValidationIssue, ...]:
    """Resolve public schema facts lazily to keep value models independent."""
    from zenture._contract.models import (
        ArtifactUploadRequest,
        CreateRunRequest,
        PrepareKnowledgeRunRequest,
        RecordRunOutcomeRequest,
        RunHeaderSchema,
        RunParameterSchema,
    )
    from zenture.validation_projection import sanitize_issues

    models: list[type[BaseModel]] = [
        ArtifactUploadRequest,
        CreateRunRequest,
        PrepareKnowledgeRunRequest,
        RecordRunOutcomeRequest,
        RunParameterSchema,
        RunHeaderSchema,
    ]
    if channel == "mcp":
        from zenture._mcp.contracts import (
            McpArtifactRequest,
            McpGetRunRequest,
            McpListRunsRequest,
            McpOutcomeRequest,
            McpParameterSchema,
        )

        models.extend(
            [
                McpArtifactRequest,
                McpGetRunRequest,
                McpListRunsRequest,
                McpOutcomeRequest,
                McpParameterSchema,
            ]
        )
    return sanitize_issues(value, [model.model_json_schema() for model in models])
