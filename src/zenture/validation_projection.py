"""Filepath: validation_projection.py
Purpose: Project schema-owned safe locations and facts, excluding all request content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .validation_issues import ValidationIssue


def _branches(schema: dict[str, Any], definitions: dict[str, Any]) -> list[dict[str, Any]]:
    if "$ref" in schema:
        return _branches(definitions.get(schema["$ref"].rsplit("/", 1)[1], {}), definitions)
    variants = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(variants, list):
        return [
            branch
            for variant in cast("list[dict[str, Any]]", variants)
            for branch in _branches(variant, definitions)
        ]
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        schema = {**schema, "items": _branches(schema["items"], definitions)[0]}
    return [schema]


def _branch_label(token: object, node: dict[str, Any]) -> bool:
    return isinstance(token, str) and (
        token in {node.get("title"), node.get("const")}
        or token == node.get("properties", {}).get("type", {}).get("const")
        or token in node.get("properties", {}).get("type", {}).get("enum", [])
    )


def _advance(
    nodes: list[dict[str, Any]], token: object, definitions: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    properties = {key for node in nodes for key in node.get("properties", {})}
    if isinstance(token, str) and token in properties:
        children = [
            branch
            for node in nodes
            if token in node.get("properties", {})
            for branch in _branches(node["properties"][token], definitions)
        ]
        return children, token.replace("~", "~0").replace("/", "~1")
    if type(token) is int and token >= 0:
        arrays = [
            node
            for node in nodes
            if node.get("type") == "array"
            and isinstance(node.get("maxItems"), int)
            and token < node["maxItems"]
        ]
        if arrays:
            return _branches(arrays[0]["items"], definitions), str(token)
    selected = [node for node in nodes if _branch_label(token, node)]
    return selected, None


def locate(
    schema: dict[str, Any],
    location: tuple[object, ...],
    constraints: dict[str, object] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Strip framework/union labels; unknown names stop at the known parent."""
    definitions = schema.get("$defs", {})
    nodes = _branches(schema, definitions)
    path: list[str] = []
    for token in location:
        if token in ("body", "query", "path", "header") and not path:
            continue
        children, name = _advance(nodes, token, definitions)
        if not children:
            break
        nodes = children
        if name is not None:
            path.append(name)
    candidates = [
        node
        for node in nodes
        if node.get("type") != "null"
        and (constraints is None or _matches_facts(constraints, facts(node)))
    ]
    # A discriminator field can declare one constant in each known union branch.
    enum_sets = [facts(node).get("allowed_values") for node in nodes if node.get("type") != "null"]
    if (
        constraints is not None
        and not candidates
        and len(enum_sets) > 1
        and all(isinstance(values, list) for values in enum_sets)
    ):
        values = [value for group in enum_sets for value in cast("list[str]", group)]
        combined = {"type": "string", "enum": list(dict.fromkeys(values))}
        if _matches_facts(constraints, facts(combined)):
            candidates.append(combined)
    selected: dict[str, Any] = candidates[0] if candidates else {}
    return "" if not path else "/" + "/".join(path), selected


def facts(node: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for source, target in (
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_length"),
        ("maxItems", "max_length"),
        ("minimum", "minimum"),
        ("maximum", "maximum"),
    ):
        if source in node:
            result[target] = node[source]
    if "minLength" in node or "maxLength" in node:
        result["unit"] = "unicode_code_points"
    elif "minItems" in node or "maxItems" in node:
        result["unit"] = "items"
    if node.get("x-validation-unit") in {"bytes", "seconds"}:
        result["unit"] = node["x-validation-unit"]
    values = node.get("enum", node.get("items", {}).get("enum"))
    if (
        isinstance(values, list)
        and values
        and all(isinstance(value, str) for value in cast("list[object]", values))
    ):
        result["allowed_values"] = values
    elif isinstance(node.get("const"), str):
        result["allowed_values"] = [node["const"]]
    return result


def _matches_facts(actual: dict[str, object], declared: dict[str, object]) -> bool:
    for key, value in actual.items():
        owned = declared.get(key)
        if key == "allowed_values" and isinstance(value, list) and isinstance(owned, list):
            if set(cast("list[object]", value)) != set(cast("list[object]", owned)):
                return False
        elif owned != value:
            return False
    return True


def sanitize_issues(value: object, schemas: list[dict[str, Any]]) -> tuple[ValidationIssue, ...]:
    """Keep only paths/facts owned by one of the operation's declared models."""
    from .validation_issues import parse_issues

    safe: list[ValidationIssue] = []
    for issue in parse_issues(value):
        decoded = tuple(
            part.replace("~1", "/").replace("~0", "~") for part in issue.path.split("/")[1:]
        )
        location: tuple[object, ...] = tuple(
            int(part) if part.isdecimal() else part for part in decoded
        )
        for schema in schemas:
            actual = (
                issue.constraints.model_dump(mode="json", exclude_none=True)
                if issue.constraints
                else None
            )
            path, node = locate(schema, location, actual)
            if path != issue.path:
                continue
            if actual is not None and not _matches_facts(actual, facts(node)):
                continue
            safe.append(issue)
            break
    return tuple(safe)
