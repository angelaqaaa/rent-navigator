"""Independent expanded-tree oracle for the supported local schema representation."""

import json
from copy import deepcopy
from typing import Any

_MAPS = frozenset({"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"})
_SINGLES = frozenset(
    {
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_FORBIDDEN = frozenset(
    {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"}
)
type _Schema = dict[str, Any] | bool
type _Path = tuple[str, ...]


def _children(node: dict[str, Any]) -> list[tuple[_Path, _Schema]]:
    children: list[tuple[_Path, _Schema]] = []
    for keyword, value in node.items():
        if keyword in _MAPS or keyword == "dependencies":
            if not isinstance(value, dict):
                raise ValueError("Schema map must be an object")
            for name, child in value.items():
                if keyword == "dependencies" and isinstance(child, list):
                    continue
                if not isinstance(name, str) or not isinstance(child, dict | bool):
                    raise ValueError("Schema map has an invalid entry")
                children.append(((keyword, name), child))
        elif keyword in _ARRAYS or keyword == "items" and isinstance(value, list):
            if not isinstance(value, list):
                raise ValueError("Schema alternatives must be an array")
            for index, child in enumerate(value):
                if not isinstance(child, dict | bool):
                    raise ValueError("Schema array has an invalid entry")
                children.append(((keyword, str(index)), child))
        elif keyword in _SINGLES:
            if not isinstance(value, dict | bool):
                raise ValueError("Schema child must be an object or boolean")
            children.append(((keyword,), value))
    return children


def _reference_path(value: Any) -> _Path:
    if not isinstance(value, str) or not value.startswith("#"):
        raise ValueError("Only local URI fragments are supported")
    fragment = value[1:]
    octets = bytearray()
    cursor = 0
    try:
        while cursor < len(fragment):
            character = fragment[cursor]
            if character == "%":
                digits = fragment[cursor + 1 : cursor + 3]
                if len(digits) != 2 or any(
                    digit not in "0123456789abcdefABCDEF" for digit in digits
                ):
                    raise ValueError("Malformed fragment percent escape")
                octets.append(int(digits, 16))
                cursor += 3
            else:
                octets.extend(character.encode("utf-8", errors="strict"))
                cursor += 1
        pointer = octets.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("Fragment must contain valid UTF-8") from None
    if not pointer.startswith("/$defs/"):
        raise ValueError("Only root definition references are supported")
    tokens: list[str] = []
    for encoded in pointer[1:].split("/"):
        cursor = 0
        decoded = ""
        while cursor < len(encoded):
            character = encoded[cursor]
            if character != "~":
                decoded += character
                cursor += 1
                continue
            if cursor + 1 >= len(encoded) or encoded[cursor + 1] not in "01":
                raise ValueError("Invalid reference escape")
            decoded += "~" if encoded[cursor + 1] == "0" else "/"
            cursor += 2
        tokens.append(decoded)
    return tuple(tokens)


def resolve_judge_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand every local reference; exclude the root definition inventory from the tree.

    Every definition, including unreachable ones, is checked before root expansion.
    Traversal follows schema positions rather than arbitrary JSON data or property names.
    No production factoring or schema validation helpers are used.
    """
    nodes: dict[_Path, _Schema] = {}

    def index(node: _Schema, path: _Path) -> None:
        nodes[path] = node
        if isinstance(node, bool):
            return
        if _FORBIDDEN.intersection(node):
            raise ValueError("Unsupported schema reference form")
        if "$ref" in node and set(node) != {"$ref"}:
            raise ValueError("Reference siblings are unsupported")
        for suffix, child in _children(node):
            index(child, path + suffix)

    index(schema, ())
    complete: dict[_Path, _Schema] = {}
    active: set[_Path] = set()

    def expand(path: _Path) -> _Schema:
        if path in complete:
            return deepcopy(complete[path])
        if path in active:
            raise ValueError("Cyclic schema references are unsupported")
        active.add(path)
        node = nodes[path]
        if isinstance(node, bool):
            result: _Schema = node
        elif "$ref" in node:
            target = _reference_path(node["$ref"])
            if target not in nodes:
                raise ValueError("Reference must target an existing schema position")
            result = expand(target)
        else:
            result = deepcopy(node)
            for suffix, _ in _children(node):
                destination: Any = result
                for token in suffix[:-1]:
                    if isinstance(destination, list):
                        destination = destination[int(token)]
                    else:
                        destination = destination[token]
                final = suffix[-1]
                if isinstance(destination, list):
                    destination[int(final)] = expand(path + suffix)
                else:
                    destination[final] = expand(path + suffix)
        active.remove(path)
        complete[path] = deepcopy(result)
        return result

    for path in nodes:
        expand(path)
    root = expand(())
    assert isinstance(root, dict)
    root.pop("$defs", None)
    return root


# Re-frozen keyed-grade limits on 2026-10-13: 1971 bytes at four c1-c4 claims,
# six s1-s6 statements, all uncited; ceil(1971 * 1.10) = 2169. All 64 mixes checked.
# Claim.id has no maximum length; this finite bound uses the frozen approved ID inventory.
# The original constant name preserves historical worksheet compatibility.
INITIAL_GRADE_LIMITS = {"depth": 5, "enum_members": 9, "bytes": 2169}


def schema_metrics(schema: dict[str, Any]) -> dict[str, int]:
    """Count stored definitions once; raw dict/list depth starts at root depth 1."""
    counts = {"unions": 0, "optional": 0, "enum_members": 0, "objects": 0, "refs": 0}

    def visit(node: _Schema) -> None:
        if isinstance(node, bool):
            return
        counts["unions"] += int("anyOf" in node) + int("oneOf" in node)
        counts["unions"] += int(isinstance(node.get("type"), list))
        counts["optional"] += len(set(node.get("properties", {})) - set(node.get("required", [])))
        counts["enum_members"] += len(node.get("enum", []))
        counts["objects"] += int(node.get("type") == "object")
        counts["refs"] += int("$ref" in node)
        for _, child in _children(node):
            visit(child)

    def depth(value: object) -> int:
        if isinstance(value, dict):
            return 1 + max((depth(child) for child in value.values()), default=0)
        if isinstance(value, list):
            return 1 + max((depth(child) for child in value), default=0)
        return 0

    visit(schema)
    return {
        **counts,
        "depth": depth(schema),
        "bytes": len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()),
    }


def lint_grade_schema(schema: dict[str, Any]) -> dict[str, int]:
    """Check provider keywords, references, closed shapes and frozen structural limits."""
    supported = {
        "$defs",
        "$ref",
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "title",
        "description",
        "allOf",
    }

    def visit(node: _Schema, path: _Path = ()) -> None:
        if isinstance(node, bool):
            return
        if set(node) - supported:
            raise ValueError("Unsupported provider schema keyword")
        if "minItems" in node and (
            type(node["minItems"]) is not int or not 0 <= node["minItems"] <= 1
        ):
            raise ValueError("Unsupported minimum item count")
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            raise ValueError("Provider objects must be closed")
        if len(node.get("enum", [])) > 4 and path not in {
            ("$defs", "claim_id"),
            ("$defs", "statement_id"),
            ("$defs", "policy_id"),
        }:
            raise ValueError("Only identifier enums may exceed four members")
        if "allOf" in node and any("$ref" in child for child in node["allOf"]):
            raise ValueError("allOf cannot contain a reference")
        for suffix, child in _children(node):
            visit(child, path + suffix)

    # Validate every reference, including unused definitions and cycles.
    resolve_judge_schema(schema)
    metrics = schema_metrics(schema)
    failed = [key for key, ceiling in INITIAL_GRADE_LIMITS.items() if metrics[key] > ceiling]
    if failed:
        raise ValueError("Schema budget exceeded: " + ", ".join(failed))
    if metrics["unions"] or metrics["optional"]:
        raise ValueError("Grade schemas require zero unions and optional properties")
    visit(schema)
    return metrics
