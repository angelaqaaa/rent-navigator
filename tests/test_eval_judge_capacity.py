"""Grade-only capacity shapes retain facts and disclose synthetic measurements."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from judge_calibration import CALIBRATION_IDS, calibration_input, load_calibration
from judge_capacity_fixtures import (
    calibration_candidate,
    long_statement_candidate,
    safety_candidate,
    serialized,
    witness_metrics,
)
from judge_schema_resolution import lint_grade_schema

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.judge import _decode_judgment, build_judge_packet, judge_schema


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_all_fixed_packets_and_labels_remain_unchanged(identifier: str) -> None:
    original = load_calibration(identifier)
    packet, wire = calibration_candidate(identifier)
    assert packet == original["provider_packet"]
    assert build_judge_packet(calibration_input(packet, load_corpus())) == packet
    result = _decode_judgment(serialized(wire), packet)
    assert {row.id: {"result": row.result} for row in result.required_claims} == original[
        "expected_judgment"
    ]["required_claims"]
    assert {
        row.id: {"factual": row.factual, "citation_support": row.citation_support}
        for row in result.statements
    } == original["expected_judgment"]["statements"]
    metrics = witness_metrics(packet, wire)
    assert metrics["json_utf8_bytes"] == len(serialized(wire).encode())
    assert metrics["raw_sha256"] == sha256(serialized(wire).encode()).hexdigest()
    assert metrics["token_measurement"] is None and not metrics["remote_grammar_verified"]
    assert metrics["semantic_review_required"]
    lint_grade_schema(judge_schema(packet))


@pytest.mark.parametrize("identifier", ["J04", "J05", "J06", "J07"])
def test_unknown_service_remains_distinct_from_computed_deadline(identifier: str) -> None:
    packet, wire = calibration_candidate(identifier)
    facts = packet["approved_facts"]["facts"]
    assert facts["served_on"] is None and facts["service_method"] == "hand"
    assert facts["effective_on"] == "2027-03-01"
    assert packet["observed_tool_result"]["latest_deemed_service_on"] == "2026-12-01"
    assert _decode_judgment(serialized(wire), packet)


def test_six_full_length_statements_have_one_shared_grade_shape() -> None:
    packet, wire = long_statement_candidate()
    assert len(packet["statements"]) == 6
    assert all(len(row["text"]) == 600 for row in packet["statements"])
    assert all(row["citation_support"] == "not_applicable" for row in wire["statements"].values())
    assert len(_decode_judgment(serialized(wire), packet).statements) == 6
    schema = judge_schema(packet)
    assert all(
        ref == {"$ref": "#/$defs/uncited_statement"}
        for ref in schema["properties"]["statements"]["properties"].values()
    )
    lint_grade_schema(schema)


@pytest.mark.parametrize("identifier", ["S04", "S07"])
def test_safety_packets_keep_complete_sources_and_no_policy_recitation_requirement(
    identifier: str,
) -> None:
    value, packet, wire = safety_candidate(load_corpus(), identifier)
    assert packet["safety_context"]["policy_claims"] is True
    assert packet == build_judge_packet(value)
    result = _decode_judgment(serialized(wire), packet)
    assert all(row.result == "met" for row in result.required_claims)
    assert result.policy_violations == []
    lint_grade_schema(judge_schema(packet))
    if identifier == "S04":
        text = packet["statements"][0]["text"]
        assert "assignment" not in text
        for qualification in (
            "lawfully entitled",
            "last increase for that tenant in that unit",
            "unit was first rented to that tenant if none",
            "at least 12 months have elapsed",
            "section 123 increases do not count",
        ):
            assert qualification in text


def test_helpers_never_modify_frozen_fixture_bytes_or_returned_grades() -> None:
    directories = [
        Path("tests/fixtures/judge_calibration"),
        Path("tests/fixtures/judge_calibration_v2"),
    ]
    before = {
        str(p): p.read_bytes() for directory in directories for p in directory.glob("J*.json")
    }
    for identifier in CALIBRATION_IDS:
        packet, wire = calibration_candidate(identifier)
        invalid = deepcopy(wire)
        invalid["required_claims"][next(iter(invalid["required_claims"]))]["result"] = "repaired"
        with pytest.raises(ValueError):
            _decode_judgment(serialized(invalid), packet)
    assert before == {path: Path(path).read_bytes() for path in before}
