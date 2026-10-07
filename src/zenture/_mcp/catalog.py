"""SDK-owned tool definitions and bounded, detached MCP catalog parsing."""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import cast

from pydantic import BaseModel, JsonValue

from zenture.errors import ZentureMCPProtocolError

_MAX_TOOLS = 128
_MAX_PAGES = 128
_MAX_JSON_BYTES = 256 * 1024
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class McpToolDefinition:
    """One registered tool snapshot; JSON fields are detached caller-owned values."""

    name: str
    input_schema: dict[str, JsonValue]
    title: str | None = None
    description: str | None = None
    output_schema: dict[str, JsonValue] | None = None
    annotations: dict[str, JsonValue] | None = None


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", by_alias=True, exclude_none=True)
    if not isinstance(value, Mapping):
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    return cast("Mapping[str, object]", value)


def _json_value(value: object, depth: int = 0) -> JsonValue:
    if depth > 64:
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, depth + 1) for item in cast("list[object]", value)]
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        if all(isinstance(key, str) for key in mapping):
            return {cast("str", key): _json_value(item, depth + 1) for key, item in mapping.items()}
    raise ZentureMCPProtocolError("invalid_tool_catalog")


def _json_object(value: object) -> dict[str, JsonValue]:
    result = _json_value(value)
    if not isinstance(result, dict):
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    return result


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    return value


def _definition(value: object) -> McpToolDefinition:
    item = _mapping(value)
    name = item.get("name")
    if not isinstance(name, str) or _TOOL_NAME.fullmatch(name) is None:
        raise ZentureMCPProtocolError("invalid_tool_catalog")
    output = item.get("outputSchema")
    annotations = item.get("annotations")
    return McpToolDefinition(
        name=name,
        title=_optional_text(item.get("title")),
        description=_optional_text(item.get("description")),
        input_schema=_json_object(item.get("inputSchema")),
        output_schema=None if output is None else _json_object(output),
        annotations=None if annotations is None else _json_object(annotations),
    )


@dataclass
class CatalogSnapshot:
    """Accumulate complete pages or fail without exposing partial definitions."""

    definitions: list[McpToolDefinition] = field(default_factory=list[McpToolDefinition])
    names: set[str] = field(default_factory=set[str])
    cursors: set[str] = field(default_factory=set[str])
    pages: int = 0
    json_bytes: int = 0

    def add_page(self, result: object) -> str | None:
        page = _mapping(result)
        tools = page.get("tools")
        if not isinstance(tools, list | tuple):
            raise ZentureMCPProtocolError("invalid_tool_catalog")
        self.pages += 1
        for value in cast("list[object] | tuple[object, ...]", tools):
            definition = _definition(value)
            if definition.name in self.names:
                raise ZentureMCPProtocolError("duplicate_tool_name")
            self.names.add(definition.name)
            self.definitions.append(definition)
            if len(self.definitions) > _MAX_TOOLS:
                raise ZentureMCPProtocolError("tool_catalog_too_large")
            self.json_bytes += len(json.dumps(asdict(definition), allow_nan=False).encode("utf-8"))
            if self.json_bytes > _MAX_JSON_BYTES:
                raise ZentureMCPProtocolError("tool_catalog_too_large")
        cursor = page.get("nextCursor")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ZentureMCPProtocolError("invalid_tool_catalog")
        if cursor in self.cursors:
            raise ZentureMCPProtocolError("tool_catalog_cursor_loop")
        if self.pages >= _MAX_PAGES:
            raise ZentureMCPProtocolError("tool_catalog_too_many_pages")
        self.cursors.add(cursor)
        return cursor


def continuation(method: Callable[..., object], cursor: str) -> object:
    """Use an additive cursor capability without retrying a failed transport call."""

    try:
        inspect.signature(method).bind(cursor=cursor)
    except (TypeError, ValueError):
        raise ZentureMCPProtocolError("tool_catalog_pagination_unsupported") from None
    return method(cursor=cursor)
