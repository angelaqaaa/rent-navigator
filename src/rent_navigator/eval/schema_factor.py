"""Factor exact repeated judge-schema nodes without changing their expanded form."""

import copy
import json
import re
from collections.abc import Iterator
from hashlib import sha256
from typing import Any, cast
from urllib.parse import unquote_to_bytes

SCHEMA_REPRESENTATION: dict[str, Any] = {
    "name": "exact_subtree_factoring",
    "version": 1,
    "stage": "post_sdk_transform",
    "classes": ["audit_object", "statement_definition"],
}

_AUDIT_PREFIX = "SharedAudit_v1_"
_STATEMENT_PREFIX = "SharedStatement_v1_"
_SHARED_PREFIXES = (_AUDIT_PREFIX, _STATEMENT_PREFIX)
_AUDIT_FIELDS = frozenset({"basis", "fact_fields", "observation", "source_anchors", "tool_fields"})
_STATEMENT_FIELDS = frozenset({"citation_support", "factual", "parts"})
_FORBIDDEN = frozenset(
    {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"}
)
_SCHEMA_MAPS = frozenset(
    {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
)
_SCHEMA_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUES = frozenset(
    {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "contains",
        "additionalItems",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_INVALID_ESCAPE = re.compile(r"~(?![01])")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
type _Path = tuple[str | int, ...]
type _Node = dict[str, Any] | bool


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("Schema must be finite canonical JSON.") from None


def _children(node: dict[str, Any], path: _Path) -> Iterator[tuple[_Path, _Node]]:
    """Visit only schema positions; annotation and instance data remain opaque."""
    for key in sorted(node):
        value = node[key]
        if key in _SCHEMA_MAPS:
            if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
                raise ValueError("Schema map must have string names.")
            for name in sorted(value):
                yield path + (key, name), _as_node(value[name])
        elif key in _SCHEMA_ARRAYS:
            if not isinstance(value, list):
                raise ValueError("Schema alternatives must be a list.")
            for index, child in enumerate(value):
                yield path + (key, index), _as_node(child)
        elif key == "items" and isinstance(value, list):
            for index, child in enumerate(value):
                yield path + (key, index), _as_node(child)
        elif key in _SCHEMA_VALUES or key == "items":
            yield path + (key,), _as_node(value)
        elif key == "dependencies":
            if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
                raise ValueError("Schema dependencies must have string names.")
            for name in sorted(value):
                if not isinstance(value[name], list):
                    yield path + (key, name), _as_node(value[name])


def _as_node(value: Any) -> _Node:
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Schema position must contain an object or boolean.")
    return cast(dict[str, Any], value)


def _reference_path(reference: Any) -> tuple[str, ...]:
    if not isinstance(reference, str) or not reference.startswith("#"):
        raise ValueError("Only local definition references are supported.")
    fragment = reference[1:]
    if _INVALID_PERCENT_ESCAPE.search(fragment):
        raise ValueError("Definition reference has an invalid percent escape.")
    try:
        pointer = unquote_to_bytes(fragment).decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("Definition reference must use valid UTF-8.") from None
    if not pointer.startswith("/$defs/"):
        raise ValueError("Only local definition references are supported.")
    parts = pointer[1:].split("/")
    if any(_INVALID_ESCAPE.search(part) for part in parts):
        raise ValueError("Definition reference has an invalid pointer escape.")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def _validate(schema: dict[str, Any]) -> dict[_Path, _Node]:
    nodes: dict[_Path, _Node] = {}
    children: dict[_Path, list[_Path]] = {}
    pending: list[tuple[_Path, _Node]] = [((), _as_node(schema))]
    while pending:
        path, node = pending.pop()
        nodes[path] = node
        if isinstance(node, bool):
            children[path] = []
            continue
        if _FORBIDDEN.intersection(node):
            raise ValueError("Schema identifiers and dynamic references are unsupported.")
        if "$ref" in node and len(node) != 1:
            raise ValueError("Definition references must be sole-key schema nodes.")
        descendants = list(_children(node, path))
        children[path] = [child_path for child_path, _ in descendants]
        pending.extend(reversed(descendants))

    pointer_positions = {tuple(map(str, path)): path for path in nodes}
    references: dict[_Path, _Path] = {}
    for path, node in nodes.items():
        if isinstance(node, dict) and "$ref" in node:
            pointer = _reference_path(node["$ref"])
            # JSON pointer indices are strings, while indexed schema positions use ints.
            target = pointer_positions.get(pointer)
            if target is None:
                raise ValueError("Definition reference does not name a schema position.")
            references[path] = target

    active: set[_Path] = set()
    complete: set[_Path] = set()

    def visit(path: _Path) -> None:
        if path in active:
            raise ValueError("Definition references must be acyclic.")
        if path in complete:
            return
        active.add(path)
        for child_path in children[path]:
            visit(child_path)
        if path in references:
            visit(references[path])
        active.remove(path)
        complete.add(path)

    # Include unused definitions, rather than validating only the root's reference closure.
    for path in nodes:
        visit(path)
    return nodes


def _is_closed_object(node: _Node, fields: frozenset[str]) -> bool:
    if not isinstance(node, dict):
        return False
    properties = node.get("properties")
    required = node.get("required")
    return (
        node.get("type") == "object"
        and isinstance(properties, dict)
        and set(properties) == fields
        and node.get("additionalProperties") is False
        and isinstance(required, list)
        and len(required) == len(fields)
        and all(isinstance(name, str) for name in required)
        and set(required) == fields
    )


def _is_shared_body(path: _Path) -> bool:
    return any(
        part == "$defs" and isinstance(name, str) and name.startswith(_SHARED_PREFIXES)
        for part, name in zip(path, path[1:], strict=False)
    )


def _node_at(schema: dict[str, Any], path: _Path) -> Any:
    value: Any = schema
    for part in path:
        value = value[part]
    return value


def _replace_at(schema: dict[str, Any], path: _Path, reference: str) -> None:
    if not path:
        raise ValueError("Root schema cannot be an interned replacement site.")
    parent = _node_at(schema, path[:-1])
    parent[path[-1]] = {"$ref": reference}


def _groups(schema: dict[str, Any], *, statements: bool) -> list[tuple[bytes, list[_Path]]]:
    groups: dict[bytes, list[_Path]] = {}
    for path, node in _validate(schema).items():
        if _is_shared_body(path):
            continue
        if statements:
            eligible = (
                len(path) == 2
                and path[0] == "$defs"
                and isinstance(path[1], str)
                and path[1].startswith("JudgeStatement_")
                and _is_closed_object(node, _STATEMENT_FIELDS)
            )
        else:
            eligible = _is_closed_object(node, _AUDIT_FIELDS)
        if eligible:
            groups.setdefault(_canonical(node), []).append(path)
    repeated = [(encoded, paths) for encoded, paths in groups.items() if len(paths) >= 2]
    return sorted(repeated, key=lambda group: (sha256(group[0]).hexdigest(), group[0]))


def _intern_pass(schema: dict[str, Any], *, statements: bool) -> dict[str, Any]:
    prefix = _STATEMENT_PREFIX if statements else _AUDIT_PREFIX
    while True:
        changed = False
        for encoded, paths in _groups(schema, statements=statements):
            # A previously processed nested group may have changed a candidate literally.
            current_nodes = _validate(schema)
            sites = [
                path
                for path in paths
                if path in current_nodes and _canonical(current_nodes[path]) == encoded
            ]
            if len(sites) < 2:
                continue
            name = prefix + sha256(encoded).hexdigest()
            definitions = schema.get("$defs", {})
            if name in definitions and _canonical(definitions[name]) != encoded:
                raise ValueError("Generated shared-definition name has unequal existing content.")
            trial = copy.deepcopy(schema)
            trial_definitions = trial.setdefault("$defs", {})
            if name not in trial_definitions:
                trial_definitions[name] = copy.deepcopy(_node_at(schema, sites[0]))
            for path in sites:
                _replace_at(trial, path, f"#/$defs/{name}")
            if len(_canonical(trial)) < len(_canonical(schema)):
                _validate(trial)
                schema = trial
                changed = True
        if not changed:
            return schema


def factor_judge_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Share only profitable literal repeats, preserving the complete expanded schema."""
    if not isinstance(schema, dict):
        raise ValueError("Root schema must be an object.")
    original = _canonical(schema)
    result = copy.deepcopy(schema)
    _validate(result)
    result = _intern_pass(result, statements=False)
    result = _intern_pass(result, statements=True)
    _validate(result)
    if len(_canonical(result)) > len(original):
        raise ValueError("Factored schema must not grow.")
    return result
