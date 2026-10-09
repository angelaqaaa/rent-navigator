"""Literal representation invariants and independent expanded-tree comparisons."""

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from judge_schema_resolution import resolve_judge_schema

from rent_navigator.eval.schema_factor import SCHEMA_REPRESENTATION, factor_judge_schema

AUDIT_FIELDS = ["basis", "fact_fields", "observation", "source_anchors", "tool_fields"]
STATEMENT_FIELDS = ["citation_support", "factual", "parts"]


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def audit(description: str = "literal guidance " * 90) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "basis": {"type": "string", "description": description, "maxLength": 360},
            "fact_fields": {"type": "array", "items": {"type": "string", "enum": ["/f/a", "/f/b"]}},
            "observation": {"type": "string", "enum": ["explicit", "conflicting"]},
            "source_anchors": {
                "type": "array",
                "items": {"type": "string", "enum": ["e1", "e2"]},
                "uniqueItems": True,
            },
            "tool_fields": {"type": "array", "items": {"type": "string", "enum": ["/t/a"]}},
        },
        "required": list(AUDIT_FIELDS),
        "additionalProperties": False,
        "title": "Exact audit title",
    }


def closed(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def two_audits(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    return closed({"left": deepcopy(first), "right": deepcopy(second)})


def statement(*, nested_audit: bool = False) -> dict[str, Any]:
    item = (
        closed({"factual_audit": audit(), "citation_audit": audit()})
        if nested_audit
        else {"type": "string", "description": "Preserve every literal sentence. " * 90}
    )
    return {
        **closed(
            {
                "citation_support": {"type": "string", "enum": ["supported", "unsupported"]},
                "factual": {"type": "string", "enum": ["supported", "contradicted"]},
                "parts": {"type": "array", "items": item, "minItems": 1, "maxItems": 10},
            }
        ),
        "title": "Full statement title",
    }


def statement_root(definition: dict[str, Any]) -> dict[str, Any]:
    return {
        **closed({f"s{i}": {"$ref": f"#/$defs/JudgeStatement_s{i}"} for i in range(1, 4)}),
        "$defs": {f"JudgeStatement_s{i}": deepcopy(definition) for i in range(1, 4)},
    }


def shared_name(prefix: str, node: dict[str, Any]) -> str:
    return prefix + hashlib.sha256(canonical(node)).hexdigest()


def assert_equivalent(original: dict[str, Any], factored: dict[str, Any]) -> None:
    assert canonical(resolve_judge_schema(factored)) == canonical(resolve_judge_schema(original))
    assert len(canonical(factored)) <= len(canonical(original))


def test_representation_descriptor_is_exact() -> None:
    assert SCHEMA_REPRESENTATION == {
        "name": "exact_subtree_factoring",
        "version": 1,
        "stage": "post_sdk_transform",
        "classes": ["audit_object", "statement_definition"],
    }


def test_profitable_audits_have_full_hash_and_sole_key_references() -> None:
    node = audit()
    original = two_audits(node, node)
    factored = factor_judge_schema(original)
    name = shared_name("SharedAudit_v1_", node)
    assert factored["$defs"] == {name: node}
    assert factored["properties"] == {
        "left": {"$ref": f"#/$defs/{name}"},
        "right": {"$ref": f"#/$defs/{name}"},
    }
    assert factored["required"] == ["left", "right"]
    assert factored["additionalProperties"] is False
    assert len(canonical(factored)) < len(canonical(original))
    assert_equivalent(original, factored)


def test_unprofitable_minimal_audits_remain_literal() -> None:
    node = closed({name: {} for name in AUDIT_FIELDS})
    original = two_audits(node, node)
    assert factor_judge_schema(original) == original


def test_one_occurrence_remains_literal_even_if_large() -> None:
    original = closed({"only": audit()})
    assert factor_judge_schema(original) == original


def test_whole_statement_definitions_remain_aliases_and_preserve_closed_maps() -> None:
    definition = statement()
    original = statement_root(definition)
    factored = factor_judge_schema(original)
    name = shared_name("SharedStatement_v1_", definition)
    assert factored["$defs"][name] == definition
    for index in range(1, 4):
        assert factored["$defs"][f"JudgeStatement_s{index}"] == {"$ref": f"#/$defs/{name}"}
    assert factored["properties"] == original["properties"]
    expanded = resolve_judge_schema(factored)
    assert expanded["required"] == ["s1", "s2", "s3"]
    assert expanded["additionalProperties"] is False
    for definition in expanded["properties"].values():
        assert definition["required"] == STATEMENT_FIELDS
        assert definition["additionalProperties"] is False
        assert definition["properties"]["parts"]["maxItems"] == 10
    assert_equivalent(original, factored)


def test_audit_pass_precedes_statement_pass_without_factoring_claims() -> None:
    original = statement_root(statement(nested_audit=True))
    original["$defs"]["JudgeClaim_c1"] = deepcopy(statement(nested_audit=True))
    original["$defs"]["JudgeClaim_c2"] = deepcopy(statement(nested_audit=True))
    factored = factor_judge_schema(original)
    audits = [name for name in factored["$defs"] if name.startswith("SharedAudit_v1_")]
    statements = [name for name in factored["$defs"] if name.startswith("SharedStatement_v1_")]
    assert len(audits) == len(statements) == 1
    shared = factored["$defs"][statements[0]]
    part = shared["properties"]["parts"]["items"]["properties"]
    assert part["factual_audit"] == part["citation_audit"] == {"$ref": f"#/$defs/{audits[0]}"}
    assert "JudgeClaim_c1" in factored["$defs"]
    assert "JudgeClaim_c2" in factored["$defs"]
    assert set(factored["$defs"]["JudgeClaim_c1"]) != {"$ref"}
    assert_equivalent(original, factored)


@pytest.mark.parametrize(
    "difference",
    ["title", "description", "enum_order", "bound", "citation_domain", "required_order"],
)
def test_literal_differences_never_merge(difference: str) -> None:
    first = audit()
    second = deepcopy(first)
    if difference == "title":
        second["title"] = "Different audit title"
    elif difference == "description":
        second["properties"]["basis"]["description"] += " additional guidance"
    elif difference == "enum_order":
        second["properties"]["observation"]["enum"].reverse()
    elif difference == "bound":
        second["properties"]["basis"]["maxLength"] = 359
    elif difference == "citation_domain":
        second["properties"]["source_anchors"]["items"]["enum"] = ["e1", "e3"]
    else:
        second["required"].reverse()
    original = two_audits(first, second)
    assert factor_judge_schema(original) == original
    assert canonical(first) != canonical(second)


@pytest.mark.parametrize("difference", ["title", "grade_order", "part_bound", "required_order"])
def test_differing_statement_definitions_never_merge(difference: str) -> None:
    first = statement()
    second = deepcopy(first)
    if difference == "title":
        second["title"] += " changed"
    elif difference == "grade_order":
        second["properties"]["factual"]["enum"].reverse()
    elif difference == "part_bound":
        second["properties"]["parts"]["maxItems"] = 9
    else:
        second["required"].reverse()
    original = {
        **closed({"s1": {"$ref": "#/$defs/JudgeStatement_s1"}}),
        "$defs": {"JudgeStatement_s1": first, "JudgeStatement_s2": second},
    }
    assert factor_judge_schema(original) == original


@pytest.mark.parametrize(
    "difference", ["open", "missing_required", "duplicate_required", "extra_property", "not_object"]
)
def test_incomplete_or_ineligible_audit_objects_are_not_interned(difference: str) -> None:
    node = audit()
    if difference == "open":
        node["additionalProperties"] = True
    elif difference == "missing_required":
        node["required"].pop()
    elif difference == "duplicate_required":
        node["required"][-1] = node["required"][0]
    elif difference == "extra_property":
        node["properties"]["extra"] = {"type": "string"}
    else:
        node["type"] = "string"
    original = two_audits(node, node)
    assert factor_judge_schema(original) == original


def test_determinism_input_immutability_and_idempotence() -> None:
    original = statement_root(statement(nested_audit=True))
    before = deepcopy(original)
    first = factor_judge_schema(original)
    second = factor_judge_schema(original)
    assert original == before
    assert first is not original
    assert canonical(first) == canonical(second)
    assert canonical(factor_judge_schema(first)) == canonical(first)
    assert_equivalent(original, first)
    first["properties"]["s1"]["$ref"] = "changed only in the returned object"
    assert original == before


def test_nested_audit_candidates_remain_acyclic_and_idempotent() -> None:
    inner = audit()
    outer = audit()
    outer["properties"]["basis"] = inner
    original = closed({"a": deepcopy(outer), "b": deepcopy(outer), "c": deepcopy(inner)})
    factored = factor_judge_schema(original)
    assert len(canonical(factored)) < len(canonical(original))
    assert factor_judge_schema(factored) == factored
    assert_equivalent(original, factored)


@pytest.mark.parametrize("kind", ["audit", "statement"])
def test_equal_generated_name_is_reused_without_self_reference(kind: str) -> None:
    node = audit() if kind == "audit" else statement()
    prefix = "SharedAudit_v1_" if kind == "audit" else "SharedStatement_v1_"
    name = shared_name(prefix, node)
    original = two_audits(node, node) if kind == "audit" else statement_root(node)
    original.setdefault("$defs", {})[name] = deepcopy(node)
    factored = factor_judge_schema(original)
    assert factored["$defs"][name] == node
    assert factor_judge_schema(factored) == factored
    assert_equivalent(original, factored)


@pytest.mark.parametrize("kind", ["audit", "statement"])
def test_unequal_generated_name_collision_is_rejected(kind: str) -> None:
    node = audit() if kind == "audit" else statement()
    prefix = "SharedAudit_v1_" if kind == "audit" else "SharedStatement_v1_"
    name = shared_name(prefix, node)
    original = two_audits(node, node) if kind == "audit" else statement_root(node)
    original.setdefault("$defs", {})[name] = {"type": "string", "title": "Different literal node"}
    before = deepcopy(original)
    with pytest.raises(ValueError):
        factor_judge_schema(original)
    assert original == before


def test_existing_shared_bodies_are_not_replacement_sites() -> None:
    node = audit()
    name = shared_name("SharedAudit_v1_", node)
    original = {
        **closed({"only": {"$ref": f"#/$defs/{name}"}}),
        "$defs": {name: node, "SharedStatement_v1_existing": statement(nested_audit=True)},
    }
    assert factor_judge_schema(original) == original
    assert_equivalent(original, original)


def test_schema_data_and_keyword_like_property_names_are_preserved() -> None:
    repeated = audit()
    original = {
        **closed({name: {"type": "string"} for name in ("$ref", "$id", "$defs", "anyOf")}),
        "enum": [repeated, deepcopy(repeated)],
        "const": {"$ref": "https://invalid.example/schema", "audit": repeated},
        "default": {"$dynamicRef": "not a schema node", "audit": repeated},
        "examples": [repeated, deepcopy(repeated)],
    }
    assert factor_judge_schema(original) == original
    assert resolve_judge_schema(original) == original


def test_audits_in_schema_array_and_conditional_positions_are_eligible() -> None:
    node = audit()
    original = {
        "anyOf": [deepcopy(node), deepcopy(node)],
        "if": {"type": "string"},
        "then": deepcopy(node),
        "else": deepcopy(node),
    }
    factored = factor_judge_schema(original)
    name = shared_name("SharedAudit_v1_", node)
    assert factored["$defs"][name] == node
    assert factored["anyOf"] == [{"$ref": f"#/$defs/{name}"}] * 2
    assert factored["then"] == factored["else"] == {"$ref": f"#/$defs/{name}"}
    assert_equivalent(original, factored)


def test_multibyte_profitability_uses_utf8_bytes() -> None:
    # Identical character counts have different byte savings and hashed identities.
    ascii_node = audit("a" * 300)
    unicode_node = audit("租" * 300)
    assert len(canonical(unicode_node)) - len(canonical(ascii_node)) == 600
    for node in (ascii_node, unicode_node):
        original = two_audits(node, node)
        factored = factor_judge_schema(original)
        name = shared_name("SharedAudit_v1_", node)
        assert name in factored["$defs"]
        assert len(canonical(factored)) < len(canonical(original))
        assert_equivalent(original, factored)


def test_independent_resolver_has_hand_checkable_alias_and_escape_expansion() -> None:
    leaf = {"type": "string", "title": "Keep title", "enum": ["b", "a"]}
    original = {
        **closed({"value": {"$ref": "#/$defs/Alias"}}),
        "$defs": {
            "a/b~c": {"type": "object", "properties": {"x/y": leaf}},
            "Alias": {"$ref": "#/$defs/a~1b~0c/properties/x~1y"},
            "unused": {"type": "integer"},
        },
    }
    before = deepcopy(original)
    assert resolve_judge_schema(original) == closed({"value": leaf})
    assert original == before
    assert factor_judge_schema(original) == original


def test_independent_resolver_preserves_boolean_schema_and_data() -> None:
    original = {
        "properties": {"yes": {"$ref": "#/$defs/Yes"}, "no": {"$ref": "#/$defs/No"}},
        "required": ["no", "yes"],
        "additionalProperties": False,
        "$defs": {"Yes": True, "No": False},
        "examples": [{"$ref": "opaque data"}],
    }
    assert resolve_judge_schema(original) == {
        "properties": {"yes": True, "no": False},
        "required": ["no", "yes"],
        "additionalProperties": False,
        "examples": [{"$ref": "opaque data"}],
    }
    assert factor_judge_schema(original) == original


@pytest.mark.parametrize(
    "bad_reference",
    [
        "https://invalid.example/schema",
        "other.json#/$defs/A",
        "#/properties/a",
        "#/$defs/missing",
        "#/$defs/A~2",
        "#/$defs/A~",
        "#/$defs/A/properties",
        "#/$defs/A/enum/0",
    ],
)
def test_invalid_or_non_schema_reference_targets_are_rejected(bad_reference: str) -> None:
    original = {
        **closed({"a": {"$ref": bad_reference}}),
        "$defs": {"A": {"type": "string", "properties": {}, "enum": [{}]}},
    }
    for operation in (resolve_judge_schema, factor_judge_schema):
        with pytest.raises(ValueError):
            operation(original)


@pytest.mark.parametrize(
    "forbidden",
    ["$id", "$anchor", "$dynamicAnchor", "$dynamicRef", "$recursiveAnchor", "$recursiveRef"],
)
def test_forbidden_forms_even_in_unused_definitions_are_rejected(forbidden: str) -> None:
    original = {"type": "string", "$defs": {"Unused": {"type": "string", forbidden: "x"}}}
    for operation in (resolve_judge_schema, factor_judge_schema):
        with pytest.raises(ValueError):
            operation(original)


@pytest.mark.parametrize("unused", [False, True])
def test_cycles_even_in_unused_definitions_are_rejected(unused: bool) -> None:
    original = {
        "type": "string",
        "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
    }
    if not unused:
        original = {**closed({"a": {"$ref": "#/$defs/A"}}), "$defs": original["$defs"]}
    for operation in (resolve_judge_schema, factor_judge_schema):
        with pytest.raises(ValueError):
            operation(original)


def test_reference_siblings_are_rejected_instead_of_dropping_annotations() -> None:
    original = {
        **closed({"a": {"$ref": "#/$defs/A", "description": "Must not vanish"}}),
        "$defs": {"A": {"type": "string"}},
    }
    for operation in (resolve_judge_schema, factor_judge_schema):
        with pytest.raises(ValueError):
            operation(original)


@pytest.mark.parametrize(
    ("reference", "names", "expected_name"),
    [
        pytest.param("#/$defs/A%20B", ["A B"], "A B", id="reviewer-space-name"),
        pytest.param("#/$defs/A%2520B", ["A%20B", "A B"], "A%20B", id="percent-decoded-once"),
        pytest.param("#/$defs/A%252FB", ["A%2FB", "A/B"], "A%2FB", id="literal-percent-slash"),
        pytest.param("#/$defs/%25", ["%"], "%", id="literal-percent-name"),
        pytest.param("#/$defs/%E7%A7%9F%E5%AE%A2", ["租客"], "租客", id="utf8-upper-hex"),
        pytest.param("#/$defs/%e7%a7%9f%e5%ae%a2", ["租客"], "租客", id="utf8-lower-hex"),
        pytest.param("#/$defs/租%25客", ["租%客"], "租%客", id="literal-and-encoded-utf8"),
        pytest.param("#/$defs/%C3%A9", ["é", "e\u0301"], "é", id="no-composed-normalization"),
        pytest.param(
            "#/$defs/e%CC%81", ["é", "e\u0301"], "e\u0301", id="no-decomposed-normalization"
        ),
        pytest.param("#/$defs/A+B", ["A+B", "A B"], "A+B", id="literal-plus"),
        pytest.param("#/$defs/A%2BB", ["A+B", "A B"], "A+B", id="encoded-plus"),
        pytest.param("#/$defs/A%20B", ["A+B", "A B"], "A B", id="space-is-not-plus"),
        pytest.param("#%2F%24defs%2FA%20B", ["A B"], "A B", id="encoded-namespace"),
        pytest.param("#%2f%24defs%2fA%20B", ["A B"], "A B", id="encoded-separators-lower"),
        pytest.param("#/$defs/A%7E1B", ["A/B"], "A/B", id="encoded-tilde-slash-token"),
        pytest.param("#/$defs/A%7E0B", ["A~B"], "A~B", id="encoded-tilde-token"),
        pytest.param("#/$defs/A~01B", ["A~1B", "A/B"], "A~1B", id="tilde-decoded-once"),
        pytest.param("#/$defs/A%7E01B", ["A~1B", "A/B"], "A~1B", id="percent-before-tilde-order"),
    ],
)
def test_uri_fragment_targets_are_observable_and_input_spelling_is_preserved(
    reference: str, names: list[str], expected_name: str
) -> None:
    original = {
        **closed({"p": {"$ref": reference}}),
        "$defs": {name: {"type": "string", "title": name, "enum": [name]} for name in names},
    }
    before = deepcopy(original)
    expected = closed({"p": {"type": "string", "title": expected_name, "enum": [expected_name]}})
    factored = factor_judge_schema(original)
    assert factored == before
    assert factored["properties"]["p"]["$ref"] == reference
    assert resolve_judge_schema(original) == expected
    assert resolve_judge_schema(factored) == expected
    assert original == before


@pytest.mark.parametrize(
    "reference",
    ["#/$defs/A%2Fproperties%2Fx", "#%2F%24defs%2FA%2fproperties%2fx"],
)
def test_percent_encoded_slashes_are_pointer_separators_before_token_decoding(
    reference: str,
) -> None:
    expected_leaf = {"type": "string", "title": "Nested target", "enum": ["nested"]}
    original = {
        **closed({"p": {"$ref": reference}}),
        "$defs": {
            "A": {"type": "object", "properties": {"x": expected_leaf}},
            "A/properties/x": {"type": "integer", "title": "Literal slash-name decoy"},
        },
    }
    before = deepcopy(original)
    factored = factor_judge_schema(original)
    assert factored == before
    assert resolve_judge_schema(original) == closed({"p": expected_leaf})
    assert resolve_judge_schema(factored) == closed({"p": expected_leaf})
    assert original == before


@pytest.mark.parametrize(
    ("reference", "names"),
    [
        pytest.param("#/$defs/A%20B", ["A%20B"], id="reviewer-dangling-space-target"),
        pytest.param("#/$defs/A%2520B", ["A B"], id="no-double-percent-decoding"),
        pytest.param("#/$defs/A%2FB", ["A/B"], id="encoded-slash-is-not-literal-token"),
        pytest.param("#/$defs/Missing%20Name", ["Existing"], id="dangling-encoded-target"),
        pytest.param("#/$defs/A%", ["A%"], id="malformed-percent"),
        pytest.param("#/$defs/A%2", ["A%2"], id="malformed-short-percent"),
        pytest.param("#/$defs/A%GG", ["A%GG"], id="malformed-nonhex-percent"),
        pytest.param("#/$defs/A%2Gtail", ["A%2Gtail"], id="malformed-embedded-percent"),
        pytest.param("#/$defs/%FF", ["%FF"], id="invalid-utf8-byte"),
        pytest.param("#/$defs/%C0%AF", ["%C0%AF"], id="invalid-utf8-overlong"),
        pytest.param("#/$defs/%E7%A7", ["%E7%A7"], id="invalid-utf8-truncated"),
        pytest.param("#/$defs/%ED%A0%80", ["%ED%A0%80"], id="invalid-utf8-surrogate"),
        pytest.param("#/$defs/\ud800", ["Existing"], id="invalid-literal-surrogate"),
        pytest.param("#/$defs/A%7E2B", ["A%7E2B"], id="encoded-invalid-tilde-escape"),
        pytest.param("#/$defs/A%7E", ["A%7E"], id="encoded-trailing-tilde"),
        pytest.param("#%2Fproperties%2Fa", ["a"], id="encoded-wrong-root-namespace"),
    ],
)
def test_uri_fragment_failures_reject_both_operations_without_mutation(
    reference: str, names: list[str]
) -> None:
    original = {
        **closed({"p": {"$ref": reference}}),
        "$defs": {name: {"type": "string"} for name in names},
    }
    before = deepcopy(original)
    for operation in (factor_judge_schema, resolve_judge_schema):
        with pytest.raises(ValueError):
            operation(original)
        assert original == before


def test_encoded_pointer_to_property_map_is_not_a_schema_target() -> None:
    original = {
        **closed({"p": {"$ref": "#/$defs/A%2Fproperties"}}),
        "$defs": {"A": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    before = deepcopy(original)
    for operation in (factor_judge_schema, resolve_judge_schema):
        with pytest.raises(ValueError):
            operation(original)
        assert original == before


@pytest.mark.parametrize("unused", [False, True])
def test_percent_encoded_cycles_are_rejected_including_unused_definitions(unused: bool) -> None:
    original = {
        **closed({"p": {"type": "string"} if unused else {"$ref": "#/$defs/A%20B"}}),
        "$defs": {
            "A B": {"$ref": "#/$defs/B%20C"},
            "B C": {"$ref": "#%2F%24defs%2FA%20B"},
        },
    }
    before = deepcopy(original)
    for operation in (factor_judge_schema, resolve_judge_schema):
        with pytest.raises(ValueError):
            operation(original)
        assert original == before
