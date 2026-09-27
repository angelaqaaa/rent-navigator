"""Structural calibration checks do not establish live semantic accuracy."""

import json
import socket
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic import transform_schema
from judge_calibration import (
    CALIBRATION_DIRECTORY,
    CALIBRATION_IDS,
    calibration_input,
    calibration_sizes,
    compact_json,
    load_calibration,
    stress_audit_output,
)

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.judge import (
    _decode_judgment,
    build_judge_packet,
    judge_config_hash,
    judge_schema,
)


@pytest.fixture(autouse=True)
def no_external_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Calibration preparation cannot access a provider or network")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", prohibited)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", prohibited)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def test_exact_thirteen_frozen_packets_and_originals() -> None:
    manifest = json.loads((CALIBRATION_DIRECTORY / "manifest.json").read_text())
    assert manifest["fixture_ids"] == list(CALIBRATION_IDS)
    assert set(manifest["files"]) == {f"{identifier}.json" for identifier in CALIBRATION_IDS}
    assert {path.name for path in CALIBRATION_DIRECTORY.glob("J*.json")} == set(manifest["files"])
    for name, digest in manifest["files"].items():
        assert sha256((CALIBRATION_DIRECTORY / name).read_bytes()).hexdigest() == digest
    for original in manifest["originals"].values():
        assert (
            sha256(original["original_packet_text"].encode()).hexdigest()
            == original["original_packet_text_sha256"]
        )
        assert (
            sha256(compact_json(original["original_generation_response"]).encode()).hexdigest()
            == (original["original_generation_response_sha256"])
        )
        assert (
            original["original_generation_response"]["content"][0]["text"]
            == original["original_grade_text"]
        )
        assert json.loads(original["original_grade_text"]) == original["original_grade"]
    assert sha256(Path("eval/gold.jsonl").read_bytes()).hexdigest() == (
        "575cfd2b66465633f7cb04b44eb8455293c1a9c24a4911c0482879313d9b82cb"
    )


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_public_boundary_packet_reference_binding_and_blinding(
    corpus: Corpus, identifier: str
) -> None:
    fixture = load_calibration(identifier)
    packet = fixture["provider_packet"]
    assert fixture["purpose"] == "synthetic_non_scored_judge_calibration"
    assert fixture["review_status"] == "proposed_expectations_pending_independent_review"
    assert fixture["source_provenance"]["corpus_hash"] == corpus.corpus_hash
    assert set(packet) == {
        "required_claims",
        "evidence",
        "expected_tool_result",
        "observed_tool_result",
        "statements",
    }
    assert len(packet["statements"]) <= 6
    assert build_judge_packet(calibration_input(packet, corpus)) == packet
    assert [row["id"] for row in packet["evidence"]] == sorted(
        row["id"] for row in packet["evidence"]
    )
    for number, row in enumerate(packet["evidence"], 1):
        assert row["reference"] == f"e{number}"
        assert row["text"] == corpus.chunk(row["id"]).text
        assert row["heading"] == corpus.chunk(row["id"]).heading
        assert row["cited_by"] == [
            statement["id"]
            for statement in packet["statements"]
            if row["id"] in statement["citation_ids"]
        ]
    schema = judge_schema(packet)
    assert transform_schema(deepcopy(schema)) == schema
    assert identifier not in compact_json(packet)
    assert fixture["title"] not in compact_json(packet)


def test_frozen_offline_size_report_matches_current_schema_and_fixtures() -> None:
    manifest = json.loads((CALIBRATION_DIRECTORY / "manifest.json").read_text())
    raw = (CALIBRATION_DIRECTORY / "sizes.json").read_bytes()
    assert sha256(raw).hexdigest() == manifest["sizing_file_sha256"]
    sizes = json.loads(raw)
    assert (
        sizes["judge_config_hash"] == manifest["prepared_judge_config_hash"] == judge_config_hash()
    )
    assert sizes["provider_calls"] == 0
    assert sizes["fixtures"] == {
        identifier: calibration_sizes(load_calibration(identifier))
        for identifier in CALIBRATION_IDS
    }


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_proposed_audit_examples_map_only_grades_without_semantic_claim(identifier: str) -> None:
    fixture = load_calibration(identifier)
    example = fixture["representative_audit_output"]
    result = _decode_judgment(compact_json(example), fixture["provider_packet"])
    expected = fixture["expected_judgment"]
    assert {item.id: {"result": item.result} for item in result.required_claims} == expected[
        "required_claims"
    ]
    assert {
        item.id: {"factual": item.factual, "citation_support": item.citation_support}
        for item in result.statements
    } == expected["statements"]
    assert result.false_pass == expected["false_pass"]
    assert result.policy_violations == expected["policy_violations"]
    stress = stress_audit_output(fixture)
    assert _decode_judgment(compact_json(stress), fixture["provider_packet"]) == result
    sizes = calibration_sizes(fixture)
    assert sizes["structural_stress_output_utf8_bytes"] > sizes["representative_output_utf8_bytes"]
    assert sizes["count_payload_without_timeout_utf8_bytes"] > sizes["packet_utf8_bytes"]
    assert all("token" not in key for key in sizes)


@pytest.mark.parametrize(
    "identifier,original_key", [("J01", "Q04"), ("J04", "N04"), ("J13", "R04")]
)
def test_retained_counterexamples_change_only_mechanical_handles(
    identifier: str, original_key: str
) -> None:
    manifest = json.loads((CALIBRATION_DIRECTORY / "manifest.json").read_text())
    original = manifest["originals"][original_key]
    packet = deepcopy(load_calibration(identifier)["provider_packet"])
    for row in packet["evidence"]:
        row.pop("reference")
    assert packet == json.loads(original["original_packet_text"])


def test_counterexample_targets_preserve_historical_disagreement() -> None:
    manifest = json.loads((CALIBRATION_DIRECTORY / "manifest.json").read_text())
    assert manifest["originals"]["Q04"]["original_grade"]["required_claims"]["c3"] == {
        "result": "met"
    }
    first = load_calibration("J01")["expected_judgment"]
    assert first["required_claims"]["c3"] == {"result": "missing"}
    assert first["statements"]["s3"] == {"factual": "supported", "citation_support": "unsupported"}
    assert (
        manifest["originals"]["N04"]["original_grade"]["statements"]["s4"]["factual"] == "supported"
    )
    assert load_calibration("J04")["expected_judgment"]["statements"]["s4"] == {
        "factual": "contradicted",
        "citation_support": "unsupported",
    }
    assert load_calibration("J13")["expected_judgment"]["false_pass"] is True


def test_control_pairs_keep_relevant_evidence_and_grade_independence() -> None:
    cross = load_calibration("J03")["representative_audit_output"]["required_claims"]["c3"]
    assert cross["answer_statement_ids"] == ["s2", "s5"] and cross["result"] == "met"
    split = load_calibration("J06")
    assert len(split["provider_packet"]["statements"]) == 6
    assert split["expected_judgment"]["statements"]["s4"]["factual"] == "contradicted"
    assert split["expected_judgment"]["statements"]["s5"]["factual"] == "supported"
    uncited = load_calibration("J07")
    assert uncited["provider_packet"]["statements"][0]["citation_ids"] == []
    assert uncited["expected_judgment"]["statements"]["s1"] == {
        "factual": "supported",
        "citation_support": "not_applicable",
    }
    wrong_own = load_calibration("J08")
    for grade in wrong_own["expected_judgment"]["statements"].values():
        assert grade == {"factual": "supported", "citation_support": "unsupported"}
    for positive, negative in (("J09", "J10"), ("J11", "J12")):
        left, right = load_calibration(positive), load_calibration(negative)
        assert left["provider_packet"]["evidence"] == right["provider_packet"]["evidence"]
        assert (
            left["provider_packet"]["required_claims"]
            == right["provider_packet"]["required_claims"]
        )
        assert left["expected_judgment"]["statements"]["s1"]["factual"] == "supported"
        assert right["expected_judgment"]["statements"]["s1"]["factual"] == "contradicted"
