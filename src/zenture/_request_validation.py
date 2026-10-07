"""Filepath: src/zenture/_request_validation.py
Purpose: Preserve local Run validation semantics with content-free SDK exceptions.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError

from zenture.models import SDKBaseModel
from zenture.validation_projection import locate


class SafeRunRequestModel(SDKBaseModel):
    """Preserve Pydantic's validation family with content-free failures."""

    @model_validator(mode="wrap")
    @classmethod
    def _safe_validation(cls, value: Any, handler: Any) -> Any:
        return cls._validate_content_free(value, handler)

    @classmethod
    def _validate_content_free(cls, value: Any, handler: Any) -> Any:
        """Run a complete validation handler with the shared safe error projection."""
        try:
            return handler(value)
        except ValidationError as error:
            schema = cls.model_json_schema()
            lines: list[InitErrorDetails] = []
            for detail in error.errors(include_input=False, include_context=False):
                path, _ = locate(schema, detail["loc"])
                lines.append(
                    {
                        "type": PydanticCustomError(
                            detail["type"],  # pyright: ignore[reportArgumentType] -- trusted Pydantic error kind
                            "Invalid request field",
                        ),
                        "loc": tuple(path.split("/")[1:]) if path else (),
                        "input": None,
                    }
                )
            raise ValidationError.from_exception_data(
                cls.__name__, lines, hide_input=True
            ) from None
