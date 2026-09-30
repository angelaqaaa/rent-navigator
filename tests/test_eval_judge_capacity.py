"""Complete witness representation and honest exact-JSON worksheet checks."""

import socket
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import anthropic
import pytest
from judge_calibration import CALIBRATION_IDS, calibration_input, load_calibration
from judge_capacity_fixtures import (
    calibration_candidate,
    long_statement_candidate,
    safety_candidate,
    serialized,
    witness_metrics,
)
from judge_wire_fixtures import POLICY_LIMITS

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.judge import _decode_judgment, build_judge_packet, judge_schema


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Capacity witnesses cannot access a provider or network")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", prohibited)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", prohibited)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def assert_reconstruction(packet: dict[str, Any], wire: dict[str, Any]) -> None:
    for field in ("required_claims", "statements"):
        for row in packet[field]:
            leaf = wire[field][row["id"]]
            if leaf.get("kind") == "behavior_invariant":
                assert leaf["reviewed_statement_ids"] == [row["id"] for row in packet["statements"]]
            else:
                assert "".join(part["text"] for part in leaf["parts"]) == row["text"]


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
@pytest.mark.parametrize("conservative", [False, True])
def test_all_original_candidates_reconstruct_and_preserve_labels(
    corpus: Corpus, identifier: str, conservative: bool
) -> None:
    original = load_calibration(identifier)
    packet, wire = calibration_candidate(identifier, conservative)
    assert packet == original["provider_packet"]
    assert build_judge_packet(calibration_input(packet, corpus)) == packet
    assert_reconstruction(packet, wire)
    result = _decode_judgment(serialized(wire), packet)
    expected = original["expected_judgment"]
    assert {item.id: {"result": item.result} for item in result.required_claims} == expected[
        "required_claims"
    ]
    assert {
        item.id: {"factual": item.factual, "citation_support": item.citation_support}
        for item in result.statements
    } == expected["statements"]
    assert result.false_pass == expected["false_pass"]
    assert result.policy_violations == expected["policy_violations"]
    metrics = witness_metrics(packet, wire)
    assert metrics["json_characters"] == len(serialized(wire))
    assert metrics["json_utf8_bytes"] == len(serialized(wire).encode())
    assert metrics["raw_sha256"] == sha256(serialized(wire).encode()).hexdigest()
    assert metrics["token_measurement"] is None
    assert not metrics["remote_grammar_verified"] and metrics["semantic_review_required"]
    assert all(anchor["occurrence_starts"] for anchor in metrics["selected_anchors"])


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_conservative_sources_are_whole_actual_chunks_and_labels_equal(identifier: str) -> None:
    packet, concise = calibration_candidate(identifier)
    other, whole = calibration_candidate(identifier, conservative=True)
    assert packet == other
    assert _decode_judgment(serialized(concise), packet) == _decode_judgment(
        serialized(whole), packet
    )
    assert len(serialized(whole).encode()) >= len(serialized(concise).encode())
    sources = {row["reference"]: row["text"] for row in packet["evidence"]}
    for leaf in whole["statements"].values():
        for part in leaf["parts"]:
            for kind in ("factual_audit", "citation_audit"):
                for anchor in part[kind]["source_anchors"]:
                    assert anchor["quote"] == sources[anchor["reference"]]


def test_deadline_and_unknown_compliance_limbs_have_independent_complete_audits() -> None:
    packet, wire = calibration_candidate("J04")
    parts = wire["statements"]["s4"]["parts"]
    assert len(parts) == 2
    assert parts[0]["factual_audit"]["observation"] == "conflicting"
    assert parts[1]["factual_audit"]["observation"] == "explicit"
    assert (
        "/observed_tool_result/latest_deemed_service_on" in parts[0]["factual_audit"]["tool_fields"]
    )
    assert "/observed_tool_result/notice_days" in parts[1]["factual_audit"]["tool_fields"]
    assert packet["observed_tool_result"]["latest_deemed_service_on"] == "2026-12-01"
    assert packet["observed_tool_result"]["notice_days"] is None
    assert _decode_judgment(serialized(wire), packet).statements[3].factual == "contradicted"
    packet, wire = calibration_candidate("J06")
    assert wire["required_claims"]["c1"]["parts"][0]["answer_anchors"][0]["statement_id"] == "s5"
    result = _decode_judgment(serialized(wire), packet)
    assert result.statements[3].factual == "contradicted"
    assert result.statements[4].factual == "supported"


def test_uncited_candidate_keeps_fixed_audit_with_joint_tool_rule_witness() -> None:
    packet, wire = calibration_candidate("J07")
    part = wire["statements"]["s1"]["parts"][0]
    assert part["citation_audit"] == {
        "observation": "not_applicable",
        "source_anchors": [],
        "tool_fields": [],
        "basis": "No citation supplied.",
    }
    assert len(part["factual_audit"]["source_anchors"]) == 2
    assert len(part["factual_audit"]["tool_fields"]) == 3
    assert (
        _decode_judgment(serialized(wire), packet).statements[0].citation_support
        == "not_applicable"
    )


@pytest.mark.parametrize("identifier,reference_count", [("S04", 5), ("S07", 10)])
@pytest.mark.parametrize("conservative", [False, True])
def test_complete_security_candidates_are_distinct_from_prior_structural_controls(
    corpus: Corpus, identifier: str, reference_count: int, conservative: bool
) -> None:
    from judge_anchor_fixtures import safety_witness

    value, packet, wire = safety_candidate(corpus, identifier, conservative)
    _, old_packet, old_wire = safety_witness(corpus, identifier)
    assert packet["statements"][0]["text"] != old_packet["statements"][0]["text"]
    assert "Structural" in old_wire["statements"]["s1"]["parts"][0]["citation_audit"]["basis"]
    assert build_judge_packet(value) == packet
    assert_reconstruction(packet, wire)
    statement = wire["statements"]["s1"]["parts"][0]
    assert len(statement["factual_audit"]["source_anchors"]) == reference_count
    assert len(statement["citation_audit"]["source_anchors"]) == reference_count
    assert statement["factual_audit"]["basis"] == statement["citation_audit"]["basis"]
    assert "Structural" not in statement["citation_audit"]["basis"]
    policy = next(iter(wire["required_claims"].values()))
    assert policy["answer_anchors"] == [] and policy["limits"] == POLICY_LIMITS
    assert "/expected_tool_result" in policy["context_fields"]
    assert packet["expected_tool_result"] is None
    assert _decode_judgment(serialized(wire), packet).required_claims[0].result == "met"


@pytest.mark.parametrize("conservative", [False, True])
def test_synthetic_six_600_character_statements_are_complete_distinct_shapes(
    conservative: bool,
) -> None:
    packet, wire = long_statement_candidate(conservative)
    assert len(packet["statements"]) == 6
    assert all(len(row["text"]) == 600 for row in packet["statements"])
    assert packet != load_calibration("J07")["provider_packet"]
    assert_reconstruction(packet, wire)
    result = _decode_judgment(serialized(wire), packet)
    assert len(result.statements) == 6 and all(
        row.citation_support == "not_applicable" for row in result.statements
    )
    metrics = witness_metrics(packet, wire)
    assert metrics["statement_parts"] == 6 and metrics["answer_anchor_count"] == 6
    assert all(
        len(anchor["occurrence_starts"]) == 3
        for anchor in metrics["selected_anchors"]
        if anchor["kind"] == "statement_id" and not conservative
    )


def test_candidate_methods_do_not_mutate_any_frozen_calibration_bytes() -> None:
    directory = Path("tests/fixtures/judge_calibration")
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    for identifier in CALIBRATION_IDS:
        packet, wire = calibration_candidate(identifier)
        mutated = deepcopy(wire)
        first = next(iter(mutated["required_claims"].values()))
        first["parts"][0]["text"] += " altered"
        with pytest.raises(ValueError):
            _decode_judgment(serialized(mutated), packet)
        calibration_candidate(identifier, conservative=True)
    assert before == {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }


def test_metric_rows_describe_selected_actual_spans_without_changing_wire() -> None:
    packet, wire = calibration_candidate("J03")
    before = serialized(wire)
    metrics = witness_metrics(packet, wire)
    assert serialized(wire) == before
    assert metrics["schema_sha256"] == sha256(serialized(judge_schema(packet)).encode()).hexdigest()
    assert metrics["claim_parts"] == 4 and metrics["statement_parts"] == 5
    joint = [
        row for row in metrics["selected_anchors"] if row["path"].startswith("/required_claims/c3/")
    ]
    assert [row["trusted_id"] for row in joint] == ["s2", "s5"]
    assert all(
        row["characters"] == len(row["quote"]) and row["utf8_bytes"] == len(row["quote"].encode())
        for row in metrics["selected_anchors"]
    )
    assert "token_ratio" not in serialized(metrics)
