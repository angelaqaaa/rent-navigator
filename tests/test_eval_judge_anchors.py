"""Grade-only coverage, strict legacy rejection and no anchor-based certification."""

import json
from itertools import product
from typing import Any

import pytest
from judge_anchor_fixtures import calibration_witness
from judge_calibration import CALIBRATION_IDS, load_calibration

from rent_navigator.eval.judge import _decode_judgment, judge_schema


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_frozen_labels_survive_without_audit_fields(identifier: str) -> None:
    packet, wire = calibration_witness(identifier)
    expected = load_calibration(identifier)["expected_judgment"]
    result = _decode_judgment(json.dumps(wire), packet)
    assert {row.id: {"result": row.result} for row in result.required_claims} == expected[
        "required_claims"
    ]
    assert {
        row.id: {"factual": row.factual, "citation_support": row.citation_support}
        for row in result.statements
    } == expected["statements"]
    assert result.false_pass == expected["false_pass"]
    assert result.policy_violations == expected["policy_violations"]
    assert all(set(row) == {"result"} for row in wire["required_claims"].values())
    assert all(set(row) == {"factual", "citation_support"} for row in wire["statements"].values())


@pytest.mark.parametrize("family", ["required_claims", "statements"])
@pytest.mark.parametrize(
    "field",
    [
        "parts",
        "basis",
        "quote",
        "answer_anchors",
        "source_anchors",
        "observation",
        "fact_fields",
        "tool_fields",
        "kind",
        "reviewed_statement_ids",
        "context_fields",
        "limits",
        "factual_audit",
        "citation_audit",
    ],
)
@pytest.mark.parametrize("value", [None, "", False, [], {}])
def test_anchor_wire_fields_are_always_rejected(family: str, field: str, value: object) -> None:
    packet, wire = calibration_witness("J01")
    wire[family][next(iter(wire[family]))][field] = value
    with pytest.raises(ValueError):
        _decode_judgment(json.dumps(wire), packet)


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_historical_representative_votes_have_no_fallback(identifier: str) -> None:
    fixture = load_calibration(identifier)
    with pytest.raises(ValueError):
        _decode_judgment(
            json.dumps(fixture["representative_audit_output"]), fixture["provider_packet"]
        )


@pytest.mark.parametrize("field", ["required_claims", "statements"])
@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "foreign", "case", "space", "object", "null"]
)
def test_arrays_require_exact_once_only_identifiers(field: str, mutation: str) -> None:
    packet, wire = calibration_witness("J01")
    first = next(iter(wire[field]))
    raw = None
    if mutation == "missing":
        wire[field].pop(first)
    elif mutation == "duplicate":
        raw = json.dumps(wire)
        original = json.dumps(first) + ": " + json.dumps(wire[field][first])
        raw = raw.replace(original, original + ", " + original, 1)
    elif mutation in {"foreign", "case", "space"}:
        name = (
            "foreign"
            if mutation == "foreign"
            else first.upper()
            if mutation == "case"
            else first + " "
        )
        wire[field][name] = wire[field].pop(first)
    elif mutation == "object":
        wire[field] = [{"id": identifier, **leaf} for identifier, leaf in wire[field].items()]
    else:
        wire[field] = None
    with pytest.raises(ValueError):
        _decode_judgment(raw if raw is not None else json.dumps(wire), packet)


@pytest.mark.parametrize(
    "factual,citation",
    list(
        product(
            ["supported", "unsupported", "contradicted"],
            ["supported", "unsupported", "not_applicable"],
        )
    ),
)
@pytest.mark.parametrize("cited", [False, True])
def test_factual_and_citation_domains_remain_independent(
    factual: str, citation: str, cited: bool
) -> None:
    packet, wire = calibration_witness("J01")
    if not cited:
        packet["statements"][0]["citation_ids"] = []
        for row in packet["evidence"]:
            row["cited_by"] = [sid for sid in row["cited_by"] if sid != "s1"]
        packet["evidence"] = [
            row for row in packet["evidence"] if row["canonical"] or row["cited_by"]
        ]
        for i, row in enumerate(packet["evidence"], 1):
            row["reference"] = f"e{i}"
    wire["statements"][next(iter(wire["statements"]))].update(
        factual=factual, citation_support=citation
    )
    if cited == (citation == "not_applicable"):
        with pytest.raises(ValueError):
            _decode_judgment(json.dumps(wire), packet)
    else:
        result = _decode_judgment(json.dumps(wire), packet)
        assert result.statements[0].factual == factual
        assert result.statements[0].citation_support == citation


@pytest.mark.parametrize(
    "field", ["c1", "result", "factual", "citation_support", "false_pass", "policy_violations"]
)
def test_equal_and_escaped_duplicate_keys_are_rejected(field: str) -> None:
    packet, wire = calibration_witness("J01")
    raw = json.dumps(wire)
    replacement = '"' + field[0] + f"\\u{ord(field[1]):04x}" + field[2:] + '"'
    value: Any = wire.get(field)
    if field == "c1":
        value = wire["required_claims"]["c1"]
    elif field == "result":
        value = wire["required_claims"]["c1"][field]
    elif field in {"factual", "citation_support"}:
        value = wire["statements"][next(iter(wire["statements"]))][field]
    original = json.dumps(field) + ": " + json.dumps(value)
    raw = raw.replace(original, original + ", " + replacement + ": " + json.dumps(value), 1)
    with pytest.raises(ValueError, match="json_invalid"):
        _decode_judgment(raw, packet)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nested_non_json_constants_are_rejected(constant: str) -> None:
    packet, wire = calibration_witness("J01")
    with pytest.raises(ValueError):
        _decode_judgment(json.dumps(wire).replace('"met"', constant, 1), packet)


def test_generic_claim_count_and_unicode_ids_bind_without_position_inference() -> None:
    packet, wire = calibration_witness("J01")
    identifiers = [f"claim/{i}~ %" for i in range(17)]
    packet["required_claims"] = [{"id": key, "text": "Synthetic target"} for key in identifiers]
    wire["required_claims"] = {key: {"result": "met"} for key in reversed(identifiers)}
    wire["statements"] = dict(reversed(list(wire["statements"].items())))
    result = _decode_judgment(json.dumps(wire), packet)
    assert [row.id for row in result.required_claims] == identifiers
    assert [row.id for row in result.statements] == [row["id"] for row in packet["statements"]]
    assert judge_schema(packet)["properties"]["required_claims"]["required"] == identifiers
