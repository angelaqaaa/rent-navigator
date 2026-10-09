"""Structural calibration checks do not establish live semantic accuracy."""

import json
import shutil
import socket
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import anthropic
import judge_calibration
import pytest
from anthropic import transform_schema
from judge_calibration import (
    APPROVED_APPROVAL_HASH,
    APPROVED_CORPUS_HASH,
    APPROVED_GOLD_HASH,
    CALIBRATION_DIRECTORY,
    CALIBRATION_IDS,
    FIXED_SOURCE_MAPPING,
    HISTORICAL_CALIBRATION_DIRECTORY,
    HISTORICAL_FILE_HASHES,
    approved_calibration_dataset,
    calibration_input,
    calibration_sizes,
    compact_json,
    load_calibration,
    load_historical_calibration,
    stress_audit_output,
    wire5_audit_output,
    wire5_stress_audit_output,
)

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    _decode_judgment,
    build_judge_packet,
    judge_config_hash,
    judge_schema,
)
from rent_navigator.eval.schema_factor import SCHEMA_REPRESENTATION


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
    manifest = json.loads((HISTORICAL_CALIBRATION_DIRECTORY / "manifest.json").read_text())
    assert manifest["fixture_ids"] == list(CALIBRATION_IDS)
    assert set(manifest["files"]) == {f"{identifier}.json" for identifier in CALIBRATION_IDS}
    assert {path.name for path in HISTORICAL_CALIBRATION_DIRECTORY.glob("J*.json")} == set(
        manifest["files"]
    )
    for name, digest in manifest["files"].items():
        assert sha256((HISTORICAL_CALIBRATION_DIRECTORY / name).read_bytes()).hexdigest() == digest
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
        "approved_facts",
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


def test_frozen_offline_size_report_retains_historical_configuration() -> None:
    manifest = json.loads((HISTORICAL_CALIBRATION_DIRECTORY / "manifest.json").read_text())
    raw = (HISTORICAL_CALIBRATION_DIRECTORY / "sizes.json").read_bytes()
    assert sha256(raw).hexdigest() == manifest["sizing_file_sha256"]
    assert (
        sha256(raw).hexdigest()
        == "c8c4082647e0197d231d47bd291e59c07376406caddc4578b6a48d2e9a0dac8a"
    )
    sizes = json.loads(raw)
    historical = "893184b31c98f638b9b1d16be677f17f59eedce67ef9735e175d76fe3f996798"
    assert sizes["judge_config_hash"] == manifest["prepared_judge_config_hash"] == historical
    assert judge_config_hash() != historical
    assert sizes["provider_calls"] == 0
    assert set(sizes["fixtures"]) == set(CALIBRATION_IDS)


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_historical_wire3_representative_and_stress_are_not_migrated(identifier: str) -> None:
    fixture = load_historical_calibration(identifier)
    for old_wire in (fixture["representative_audit_output"], stress_audit_output(fixture)):
        for packet in (fixture["provider_packet"], load_calibration(identifier)["provider_packet"]):
            with pytest.raises(ValueError):
                _decode_judgment(compact_json(old_wire), packet)


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_absent_packet3_or_wire5_fields_never_fall_back_to_historical_contract(
    identifier: str,
) -> None:
    current = load_calibration(identifier)
    wire = wire5_audit_output(current)
    with pytest.raises(ValueError):
        _decode_judgment(
            compact_json(wire), load_historical_calibration(identifier)["provider_packet"]
        )
    for leaf in wire["statements"].values():
        for part in leaf["parts"]:
            part["factual_audit"].pop("fact_fields")
            part["citation_audit"].pop("fact_fields")
    with pytest.raises(ValueError):
        _decode_judgment(compact_json(wire), current["provider_packet"])


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_derived_wire5_structural_examples_map_only_unchanged_grades(identifier: str) -> None:
    fixture = load_calibration(identifier)
    example = wire5_audit_output(fixture)
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
    stress = wire5_stress_audit_output(fixture)
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
    manifest = json.loads((HISTORICAL_CALIBRATION_DIRECTORY / "manifest.json").read_text())
    original = manifest["originals"][original_key]
    packet = deepcopy(load_historical_calibration(identifier)["provider_packet"])
    for row in packet["evidence"]:
        row.pop("reference")
    assert packet == json.loads(original["original_packet_text"])


def test_counterexample_targets_preserve_historical_disagreement() -> None:
    manifest = json.loads((HISTORICAL_CALIBRATION_DIRECTORY / "manifest.json").read_text())
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


@pytest.mark.parametrize("identifier", CALIBRATION_IDS)
def test_current_fixtures_preserve_entire_history_except_independently_approved_facts(
    corpus: Corpus, identifier: str
) -> None:
    historical = load_historical_calibration(identifier)
    current = load_calibration(identifier, corpus=corpus)
    binding = current.pop("fact_authority_derivation")
    supplied = current["provider_packet"].pop("approved_facts")
    assert current == historical
    dataset = load_gold(Path("eval"), corpus)
    assert dataset.approval.status == "approved"
    assert dataset.gold_hash == APPROVED_GOLD_HASH
    assert dataset.approval_hash == APPROVED_APPROVAL_HASH
    assert corpus.corpus_hash == APPROVED_CORPUS_HASH
    source_id = FIXED_SOURCE_MAPPING[identifier]
    source = next((case for case in dataset.cases if case.id == source_id), None)
    expected = (
        {"kind": source.kind, "facts": source.request.facts.model_dump(mode="json")}
        if source is not None
        and source.kind in {"notice", "rent"}
        and source.request.mode != "question"
        else None
    )
    assert supplied == expected
    assert binding["approved_case_id"] == source_id
    assert binding["historical_fixture_sha256"] == HISTORICAL_FILE_HASHES[f"{identifier}.json"]
    assert binding["gold_file_sha256"] == dataset.gold_hash
    assert binding["approval_file_sha256"] == dataset.approval_hash
    assert binding["packet_policy_version"] == 3 and binding["wire_policy_version"] == 5
    assert binding["historical_amount_gap"] is (identifier == "J13")


def test_retained_v2_manifest_and_sizes_bind_original_preparation(corpus: Corpus) -> None:
    manifest_raw = (CALIBRATION_DIRECTORY / "manifest.json").read_bytes()
    assert len(manifest_raw) == 3851
    assert sha256(manifest_raw).hexdigest() == (
        "383184ce7c37ce605d335e2041b2b70e04c400dbb430d6662930738d62c10a81"
    )
    manifest = json.loads(manifest_raw)
    assert manifest["fixture_ids"] == list(CALIBRATION_IDS)
    assert manifest["historical_files_sha256"] == HISTORICAL_FILE_HASHES
    assert manifest["fixed_source_mapping"] == FIXED_SOURCE_MAPPING
    assert manifest["approved_source"] == {
        "gold_file_sha256": APPROVED_GOLD_HASH,
        "approval_file_sha256": APPROVED_APPROVAL_HASH,
        "corpus_hash": APPROVED_CORPUS_HASH,
    }
    assert manifest["packet_policy_version"] == 3 and manifest["wire_policy_version"] == 5
    for name, digest in manifest["files"].items():
        assert sha256((CALIBRATION_DIRECTORY / name).read_bytes()).hexdigest() == digest
    raw = (CALIBRATION_DIRECTORY / "sizes.json").read_bytes()
    assert len(raw) == 8457
    assert sha256(raw).hexdigest() == (
        "4fbcd0b998a203789d8df26a0ab050575f028cd2ba422ed5437d52ec2104198b"
    )
    assert sha256(raw).hexdigest() == manifest["sizing_file_sha256"]
    sizes = json.loads(raw)
    historical = "0746c08afcab0cba0018051f2ea1fe259d3e9cc92e6801b5b9d07d2af1198634"
    assert sizes["judge_config_hash"] == manifest["prepared_judge_config_hash"] == historical
    assert judge_config_hash() != historical
    assert sizes["provider_calls"] == 0 and sizes["count_calls"] == 0
    assert sizes["token_measurement"] is None
    for identifier in CALIBRATION_IDS:
        fresh = calibration_sizes(load_calibration(identifier, corpus=corpus))
        for field in (
            "packet_characters",
            "packet_utf8_bytes",
            "representative_output_characters",
            "representative_output_utf8_bytes",
            "structural_stress_output_characters",
            "structural_stress_output_utf8_bytes",
        ):
            assert sizes["fixtures"][identifier][field] == fresh[field]


def test_current_schema_representation_metadata(corpus: Corpus) -> None:
    directory = Path("tests/fixtures/judge_schema_representation_v1")
    manifest_raw = (directory / "manifest.json").read_bytes()
    sizes_raw = (directory / "sizes.json").read_bytes()
    legacy = json.loads((CALIBRATION_DIRECTORY / "manifest.json").read_text())
    config = judge_config_hash()
    assert json.loads(manifest_raw) == {
        "schema_version": 1,
        "purpose": "offline_judge_schema_representation",
        "representation": SCHEMA_REPRESENTATION,
        "packet_policy_version": 3,
        "wire_policy_version": 5,
        "fixture_directory": "tests/fixtures/judge_calibration_v2",
        "fixture_ids": list(CALIBRATION_IDS),
        "files": legacy["files"],
        "approved_source": legacy["approved_source"],
        "legacy_manifest_sha256": (
            "383184ce7c37ce605d335e2041b2b70e04c400dbb430d6662930738d62c10a81"
        ),
        "legacy_sizes_sha256": ("4fbcd0b998a203789d8df26a0ab050575f028cd2ba422ed5437d52ec2104198b"),
        "prepared_judge_config_hash": config,
        "sizing_file_sha256": sha256(sizes_raw).hexdigest(),
    }
    assert json.loads(sizes_raw) == {
        "schema_version": 1,
        "purpose": "offline_factored_structural_calibration_sizes",
        "representation": SCHEMA_REPRESENTATION,
        "packet_policy_version": 3,
        "wire_policy_version": 5,
        "judge_config_hash": config,
        "provider_calls": 0,
        "count_calls": 0,
        "token_measurement": None,
        "fixtures": {
            identifier: calibration_sizes(load_calibration(identifier, corpus=corpus))
            for identifier in CALIBRATION_IDS
        },
        "limitations": (
            "Exact Unicode and UTF-8 sizes only; retained packets and outputs unchanged. "
            "Structural controls do not establish semantics, token fit or provider acceptance."
        ),
    }
    for raw in (manifest_raw, sizes_raw):
        assert (
            raw
            == (
                json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode()
        )


@pytest.mark.parametrize(
    "identifier,key,value", [("J13", "proposed_cents", 102500), ("J04", "served_on", "2026-12-01")]
)
def test_jointly_rehashed_derived_facts_still_fail_external_source_binding(
    corpus: Corpus, tmp_path: Path, identifier: str, key: str, value: object
) -> None:
    target = tmp_path / "derived"
    shutil.copytree(CALIBRATION_DIRECTORY, target)
    path = target / f"{identifier}.json"
    fixture = json.loads(path.read_text())
    fixture["provider_packet"]["approved_facts"]["facts"][key] = value
    path.write_text(compact_json(fixture))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][path.name] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(compact_json(manifest))
    assert sha256(path.read_bytes()).hexdigest() == manifest["files"][path.name]
    with pytest.raises(ValueError, match="independently approved derivation"):
        load_calibration(identifier, corpus=corpus, directory=target)


@pytest.mark.parametrize(
    "case_id,key,value", [("R04", "proposed_cents", 102500), ("N04", "served_on", "2026-12-01")]
)
def test_foreign_self_signed_approved_source_cannot_replace_fixed_authority(
    corpus: Corpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    key: str,
    value: object,
) -> None:
    target = tmp_path / "foreign"
    target.mkdir()
    for name in ("gold.jsonl", "gold-approval.json", "activation.json"):
        shutil.copyfile(Path("eval") / name, target / name)
    rows = [json.loads(line) for line in (target / "gold.jsonl").read_text().splitlines()]
    row = next(row for row in rows if row["id"] == case_id)
    row["request"]["facts"][key] = value
    row["expected_tool_args"][key] = value
    if row["expected_extract"] is not None:
        row["expected_extract"][key] = value
    (target / "gold.jsonl").write_text("\n".join(compact_json(row) for row in rows) + "\n")
    approval = json.loads((target / "gold-approval.json").read_text())
    approval["gold_sha256"] = sha256((target / "gold.jsonl").read_bytes()).hexdigest()
    (target / "gold-approval.json").write_text(compact_json(approval))
    assert load_gold(target, corpus).approval.status == "approved"
    monkeypatch.setattr(judge_calibration, "APPROVED_DATA_DIRECTORY", target)
    with pytest.raises(ValueError, match="fixed external approval"):
        approved_calibration_dataset(corpus)


def test_new_derived_manifest_cannot_redefine_historical_byte_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "old"
    shutil.copytree(HISTORICAL_CALIBRATION_DIRECTORY, target)
    path = target / "J13.json"
    path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr(judge_calibration, "HISTORICAL_CALIBRATION_DIRECTORY", target)
    with pytest.raises(ValueError, match="antecedents changed"):
        load_historical_calibration("J13")
