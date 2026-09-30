"""Exact-anchor provenance, reconstruction and declared consistency controls."""

import json
import socket
import unicodedata
from copy import deepcopy
from itertools import product
from typing import Any

import anthropic
import pytest
from judge_anchor_fixtures import audit, calibration_witness, claim_part, safety_witness
from judge_wire_fixtures import POLICY_LIMITS

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.judge import _decode_judgment, judge_schema


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Anchor controls cannot access credentials or a network")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", prohibited)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", prohibited)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def decode(packet: dict[str, Any], wire: dict[str, Any]) -> Any:
    return _decode_judgment(json.dumps(wire, ensure_ascii=False), packet)


@pytest.mark.parametrize(
    "identifier", ["J01", "J02", "J03", "J05", "J08", "J09", "J10", "J11", "J12", "J13"]
)
def test_hand_authored_complete_witnesses_preserve_frozen_labels(identifier: str) -> None:
    from judge_calibration import load_calibration

    packet, wire = calibration_witness(identifier)
    before = deepcopy(packet)
    result = decode(packet, wire)
    expected = load_calibration(identifier)["expected_judgment"]
    assert {row.id: {"result": row.result} for row in result.required_claims} == expected[
        "required_claims"
    ]
    assert {
        row.id: {"factual": row.factual, "citation_support": row.citation_support}
        for row in result.statements
    } == expected["statements"]
    assert result.false_pass == expected["false_pass"]
    assert result.policy_violations == expected["policy_violations"]
    assert packet == before
    for field in ("required_claims", "statements"):
        for row in packet[field]:
            assert "".join(part["text"] for part in wire[field][row["id"]]["parts"]) == row["text"]


def test_qualifications_implication_and_cross_statement_joint_coverage() -> None:
    packet, wire = calibration_witness("J02")
    claim = wire["required_claims"]["c3"]["parts"][0]
    assert "on or before November 15, 2018" in claim["text"]
    assert "does not extend to subsequent tenancies" in claim["text"]
    assert claim["answer_anchors"][0]["statement_id"] == "s2"
    implication = wire["statements"]["s4"]["parts"][0]
    assert implication["factual_audit"]["observation"] == "necessary_implication"
    assert "tenant move-in" not in implication["factual_audit"]["source_anchors"][0]["quote"]
    assert decode(packet, wire).statements[3].factual == "supported"
    packet, wire = calibration_witness("J03")
    assert [
        a["statement_id"] for a in wire["required_claims"]["c3"]["parts"][0]["answer_anchors"]
    ] == ["s2", "s5"]
    assert decode(packet, wire).required_claims[2].result == "met"


def test_joint_source_and_three_tool_witnesses_do_not_collapse() -> None:
    packet, wire = calibration_witness("J05")
    part = wire["statements"]["s4"]["parts"][0]
    expected = [
        "/observed_tool_result/checks/3/status",
        "/observed_tool_result/latest_deemed_service_on",
        "/observed_tool_result/notice_days",
    ]
    for name in ("factual_audit", "citation_audit"):
        assert [a["reference"] for a in part[name]["source_anchors"]] == ["e4", "e6"]
        assert part[name]["tool_fields"] == expected
    assert decode(packet, wire).statements[3].factual == "supported"
    packet, wire = calibration_witness("J13")
    part = wire["statements"]["s3"]["parts"][0]
    assert part["factual_audit"]["tool_fields"] == [
        "/observed_tool_result/checks/3/status",
        "/observed_tool_result/checks/5/status",
        "/observed_tool_result/status",
    ]
    result = decode(packet, wire)
    assert result.false_pass and result.statements[2].factual == "contradicted"
    assert result.statements[2].citation_support == "unsupported"


def test_true_fact_with_irrelevant_own_citation_and_uncited_variant() -> None:
    packet, wire = calibration_witness("J08")
    first = wire["statements"]["s1"]["parts"][0]
    assert first["factual_audit"]["source_anchors"][0]["reference"] == "e4"
    assert first["citation_audit"]["source_anchors"][0]["reference"] == "e3"
    result = decode(packet, wire)
    assert all(
        row.factual == "supported" and row.citation_support == "unsupported"
        for row in result.statements
    )
    packet["statements"][0]["citation_ids"] = []
    for row in packet["evidence"]:
        row["cited_by"] = [sid for sid in row["cited_by"] if sid != "s1"]
    wire["statements"]["s1"]["citation_support"] = "not_applicable"
    first["citation_audit"] = {
        "observation": "not_applicable",
        "source_anchors": [],
        "tool_fields": [],
        "basis": "No citation supplied.",
    }
    assert decode(packet, wire).statements[0].citation_support == "not_applicable"


@pytest.mark.parametrize("identifier,own_count", [("S04", 5), ("S07", 10)])
def test_policy_nonrecitation_and_multi_own_source_shape(
    corpus: Corpus, identifier: str, own_count: int
) -> None:
    _, packet, wire = safety_witness(corpus, identifier)
    leaf = next(iter(wire["required_claims"].values()))
    assert leaf["answer_anchors"] == [] and leaf["reviewed_statement_ids"] == ["s1"]
    assert leaf["limits"] == POLICY_LIMITS
    assert (
        len(wire["statements"]["s1"]["parts"][0]["citation_audit"]["source_anchors"]) == own_count
    )
    refs = [row["reference"] for row in packet["evidence"]]
    if identifier == "S07":
        assert refs == [f"e{i}" for i in range(1, 11)] and refs != sorted(refs)
    assert "/expected_tool_result" in leaf["context_fields"]
    assert packet["expected_tool_result"] is None
    result = decode(packet, wire)
    assert result.required_claims[0].result == "met" and not result.policy_violations


def test_structurally_valid_genuine_quote_with_inaccurate_basis_is_a_limitation() -> None:
    packet, wire = calibration_witness("J02")
    part = wire["statements"]["s2"]["parts"][0]
    genuine = part["factual_audit"]["source_anchors"][0]["quote"]
    assert "on or before November 15, 2018" in genuine
    assert "does not apply with respect to any subsequent tenancy" in genuine
    part["factual_audit"]["basis"] = (
        "The exemption protects pre-Nov 15 tenancies and that exemption does "
        "not extend to later tenancies."
    )
    result = decode(packet, wire)
    assert result.statements[1].factual == "supported"
    assert "pre-Nov 15" in part["factual_audit"]["basis"]
    # Provenance/reconstruction checks intentionally do not certify the basis's subject or cutoff.


def test_genuine_but_irrelevant_quote_is_not_mechanically_certified_as_entailment() -> None:
    packet, wire = calibration_witness("J02")
    part = wire["statements"]["s3"]["parts"][0]
    part["factual_audit"] = audit(
        packet,
        "explicit",
        ["e1"],
        "This occupancy passage places the disputed-exemption burden on the landlord.",
    )
    assert "the onus is on the landlord" not in part["factual_audit"]["source_anchors"][0]["quote"]
    assert decode(packet, wire).statements[2].factual == "supported"


ORDINARY_MUTATIONS = [
    "claim_missing_part",
    "claim_drop_qualification",
    "claim_reverse_parts",
    "claim_overlap",
    "claim_empty_segment",
    "claim_cutoff",
    "statement_drop_negation",
    "statement_whitespace",
    "statement_reverse_parts",
    "statement_empty_parts",
    "invented_source_quote",
    "ellipsis_source_quote",
    "normalized_source_quote",
    "wrong_source_quote",
    "invented_answer_quote",
    "wrong_answer_quote",
    "foreign_own_citation",
    "unknown_source",
    "canonical_id_as_ref",
    "duplicate_source",
    "source_order",
    "duplicate_answer",
    "answer_order",
    "unknown_answer",
    "tool_container",
    "tool_root",
    "tool_missing",
    "tool_wildcard",
    "tool_noncanonical_index",
    "tool_duplicate",
    "tool_order",
    "tool_boolean",
    "tool_array_as_string",
    "source_bool",
    "quote_null",
    "part_bool",
    "basis_numeric",
    "basis_blank",
    "claim_basis_long",
    "audit_basis_long",
    "extra_part",
    "missing_audit",
    "extra_root",
    "missing_claim",
    "extra_statement",
    "wrong_branch",
    "false_pass_bool_coercion",
    "policy_duplicate",
    "policy_unknown",
    "explicit_without_answer",
    "conflict_without_answer",
    "factual_explicit_without_witness",
    "factual_conflict_without_witness",
    "citation_explicit_tool_only",
    "citation_conflict_tool_only",
]


def mutate_ordinary(packet: dict[str, Any], wire: dict[str, Any], mutation: str) -> None:
    claim = wire["required_claims"]["c3"]
    part = claim["parts"][0]
    statement = wire["statements"]["s2"]
    segment = statement["parts"][0]
    factual, cited = segment["factual_audit"], segment["citation_audit"]
    if mutation == "claim_missing_part":
        claim["parts"] = []
    elif mutation == "claim_drop_qualification":
        part["text"] = part["text"].split(";")[0]
    elif mutation in {"claim_reverse_parts", "claim_overlap"}:
        left, right = deepcopy(part), deepcopy(part)
        left["text"], right["text"] = part["text"][:20], part["text"][20:]
        claim["parts"] = [right, left] if mutation == "claim_reverse_parts" else [left, left, right]
    elif mutation == "claim_empty_segment":
        claim["parts"].append({**deepcopy(part), "text": ""})
    elif mutation == "claim_cutoff":
        part["text"] = part["text"].replace("on or before", "before")
    elif mutation == "statement_drop_negation":
        segment["text"] = segment["text"].replace("does not", "does")
    elif mutation == "statement_whitespace":
        segment["text"] += " "
    elif mutation == "statement_reverse_parts":
        left, right = deepcopy(segment), deepcopy(segment)
        left["text"], right["text"] = segment["text"][:20], segment["text"][20:]
        statement["parts"] = [right, left]
    elif mutation == "statement_empty_parts":
        statement["parts"] = []
    elif mutation in {
        "invented_source_quote",
        "ellipsis_source_quote",
        "normalized_source_quote",
        "wrong_source_quote",
    }:
        anchor = factual["source_anchors"][0]
        if mutation == "invented_source_quote":
            anchor["quote"] = "Subsection (6) invents an occupancy exemption."
        elif mutation == "ellipsis_source_quote":
            anchor["quote"] = anchor["quote"][:20] + "..." + anchor["quote"][-20:]
        elif mutation == "normalized_source_quote":
            anchor["quote"] = " ".join(anchor["quote"].split())
        else:
            anchor["reference"] = "e1"
    elif mutation in {"invented_answer_quote", "wrong_answer_quote"}:
        anchor = part["answer_anchors"][0]
        anchor["quote"] = (
            "The protection never extends."
            if mutation == "invented_answer_quote"
            else packet["statements"][0]["text"]
        )
    elif mutation == "foreign_own_citation":
        cited["source_anchors"] = [audit(packet, "explicit", ["e2"], "x")["source_anchors"][0]]
    elif mutation in {"unknown_source", "canonical_id_as_ref", "source_bool"}:
        factual["source_anchors"][0]["reference"] = {
            "unknown_source": "e999",
            "canonical_id_as_ref": packet["evidence"][2]["id"],
            "source_bool": True,
        }[mutation]
    elif mutation == "duplicate_source":
        factual["source_anchors"] *= 2
    elif mutation == "source_order":
        factual["source_anchors"] = list(
            reversed(audit(packet, "explicit", ["e1", "e3"], "x")["source_anchors"])
        )
    elif mutation == "duplicate_answer":
        part["answer_anchors"] *= 2
    elif mutation == "answer_order":
        part["answer_anchors"] = list(
            reversed(
                claim_part(packet, part["text"], "explicit", ["s1", "s2"], "x")["answer_anchors"]
            )
        )
    elif mutation == "unknown_answer":
        part["answer_anchors"][0]["statement_id"] = "s999"
    elif mutation.startswith("tool_"):
        factual["tool_fields"] = {
            "tool_container": ["/observed_tool_result/checks/0"],
            "tool_root": ["/observed_tool_result"],
            "tool_missing": ["/observed_tool_result/invented"],
            "tool_wildcard": ["/observed_tool_result/checks/*/status"],
            "tool_noncanonical_index": ["/observed_tool_result/checks/00/status"],
            "tool_duplicate": ["/observed_tool_result/status"] * 2,
            "tool_order": ["/observed_tool_result/status", "/observed_tool_result/notice_days"],
            "tool_boolean": [True],
            "tool_array_as_string": "/observed_tool_result/status",
        }[mutation]
    elif mutation == "quote_null":
        factual["source_anchors"][0]["quote"] = None
    elif mutation == "part_bool":
        part["text"] = True
    elif mutation == "basis_numeric":
        factual["basis"] = 123
    elif mutation == "basis_blank":
        factual["basis"] = " \n\t"
    elif mutation == "claim_basis_long":
        part["basis"] = "x" * 361
    elif mutation == "audit_basis_long":
        factual["basis"] = "x" * 321
    elif mutation == "extra_part":
        part["offset"] = 0
    elif mutation == "missing_audit":
        del segment["citation_audit"]
    elif mutation == "extra_root":
        wire["audit"] = {}
    elif mutation == "missing_claim":
        del wire["required_claims"]["c1"]
    elif mutation == "extra_statement":
        wire["statements"]["s6"] = deepcopy(statement)
    elif mutation == "wrong_branch":
        claim["kind"] = "behavior_invariant"
    elif mutation == "false_pass_bool_coercion":
        wire["false_pass"] = 1
    elif mutation == "policy_duplicate":
        wire["policy_violations"] = ["S01", "S01"]
    elif mutation == "policy_unknown":
        wire["policy_violations"] = ["S99"]
    elif mutation in {"explicit_without_answer", "conflict_without_answer"}:
        part["observation"] = "explicit" if mutation == "explicit_without_answer" else "conflicting"
        claim["result"] = "met" if mutation == "explicit_without_answer" else "contradicted"
        part["answer_anchors"] = []
    elif mutation in {"factual_explicit_without_witness", "factual_conflict_without_witness"}:
        factual["observation"] = (
            "explicit" if mutation == "factual_explicit_without_witness" else "conflicting"
        )
        statement["factual"] = (
            "supported" if mutation == "factual_explicit_without_witness" else "contradicted"
        )
        factual["source_anchors"], factual["tool_fields"] = [], []
    elif mutation in {"citation_explicit_tool_only", "citation_conflict_tool_only"}:
        cited["observation"] = (
            "explicit" if mutation == "citation_explicit_tool_only" else "conflicting"
        )
        statement["citation_support"] = (
            "supported" if mutation == "citation_explicit_tool_only" else "unsupported"
        )
        cited["source_anchors"], cited["tool_fields"] = [], ["/observed_tool_result/status"]
    else:
        raise AssertionError(mutation)


@pytest.mark.parametrize("mutation", ORDINARY_MUTATIONS)
def test_invalid_ordinary_vote_rejects_without_repair(mutation: str) -> None:
    packet, wire = calibration_witness("J02")
    # A genuine tool result supplies valid scalar pointers for pointer-only negatives.
    tool_packet, _ = calibration_witness("J13")
    packet["expected_tool_result"] = deepcopy(tool_packet["expected_tool_result"])
    packet["observed_tool_result"] = deepcopy(tool_packet["observed_tool_result"])
    assert decode(packet, wire)
    mutate_ordinary(packet, wire, mutation)
    raw = json.dumps(wire)
    with pytest.raises(ValueError):
        _decode_judgment(raw, packet)
    assert json.dumps(wire) == raw


@pytest.mark.parametrize(
    "family,observation,grade",
    [
        (family, observation, grade)
        for family, observations, grades in [
            (
                "claim",
                ["explicit", "necessary_implication", "omitted", "conflicting"],
                ["met", "missing", "contradicted"],
            ),
            (
                "factual",
                ["explicit", "necessary_implication", "insufficient", "conflicting"],
                ["supported", "unsupported", "contradicted"],
            ),
            (
                "citation",
                ["explicit", "necessary_implication", "insufficient", "conflicting"],
                ["supported", "unsupported"],
            ),
        ]
        for observation, grade in product(observations, grades)
    ],
)
def test_every_explicit_grade_observation_pair(family: str, observation: str, grade: str) -> None:
    packet, wire = calibration_witness("J02")
    if family == "claim":
        leaf = wire["required_claims"]["c3"]
        leaf["result"], leaf["parts"][0]["observation"] = grade, observation
        expected = {
            "explicit": "met",
            "necessary_implication": "met",
            "omitted": "missing",
            "conflicting": "contradicted",
        }[observation]
    else:
        leaf = wire["statements"]["s2"]
        leaf["factual" if family == "factual" else "citation_support"] = grade
        leaf["parts"][0][family + "_audit"]["observation"] = observation
        expected = {
            "explicit": "supported",
            "necessary_implication": "supported",
            "insufficient": "unsupported",
            "conflicting": "contradicted" if family == "factual" else "unsupported",
        }[observation]
    if grade == expected:
        assert decode(packet, wire)
    else:
        with pytest.raises(ValueError):
            decode(packet, wire)


@pytest.mark.parametrize("family", ["claim", "factual", "citation"])
def test_conflict_precedes_omission_or_insufficiency(family: str) -> None:
    packet, wire = calibration_witness("J02")
    leaf = wire["required_claims"]["c3"] if family == "claim" else wire["statements"]["s2"]
    original = leaf["parts"][0]
    left, right = deepcopy(original), deepcopy(original)
    left["text"], right["text"] = original["text"][:20], original["text"][20:]
    if family == "claim":
        left["observation"], right["observation"] = "omitted", "conflicting"
        grade_name, expected = "result", "contradicted"
    else:
        left[family + "_audit"]["observation"] = "insufficient"
        right[family + "_audit"]["observation"] = "conflicting"
        grade_name, expected = (
            ("factual", "contradicted")
            if family == "factual"
            else ("citation_support", "unsupported")
        )
    leaf["parts"], leaf[grade_name] = [left, right], expected
    assert decode(packet, wire)
    leaf[grade_name] = "missing" if family == "claim" else "supported"
    with pytest.raises(ValueError):
        decode(packet, wire)


POLICY_MUTATIONS = [
    "wrong_branch",
    "reviewed_missing",
    "reviewed_duplicate",
    "reviewed_foreign",
    "reviewed_bool",
    "limits_missing",
    "limits_order",
    "limits_foreign",
    "extra_field",
    "missing_field",
    "context_container",
    "context_foreign",
    "context_claim",
    "context_source",
    "context_duplicate",
    "context_order",
    "context_bool",
    "context_value_echo",
    "conflict_without_quote",
    "invented_quote",
    "blank_basis",
    "long_basis",
]


@pytest.mark.parametrize("mutation", POLICY_MUTATIONS)
def test_policy_branch_strict_review_context_and_limits(corpus: Corpus, mutation: str) -> None:
    _, packet, wire = safety_witness(corpus, "S07")
    leaf = next(iter(wire["required_claims"].values()))
    if mutation == "wrong_branch":
        leaf["kind"] = "ordinary_coverage"
    elif mutation.startswith("reviewed_"):
        leaf["reviewed_statement_ids"] = {
            "reviewed_missing": [],
            "reviewed_duplicate": ["s1", "s1"],
            "reviewed_foreign": ["s2"],
            "reviewed_bool": [True],
        }[mutation]
    elif mutation.startswith("limits_"):
        leaf["limits"] = {
            "limits_missing": POLICY_LIMITS[:-1],
            "limits_order": list(reversed(POLICY_LIMITS)),
            "limits_foreign": [*POLICY_LIMITS[:-1], "execution_history_supplied"],
        }[mutation]
    elif mutation == "extra_field":
        leaf["parts"] = []
    elif mutation == "missing_field":
        del leaf["basis"]
    elif mutation.startswith("context_"):
        leaf["context_fields"] = {
            "context_container": ["/safety_context/confirmed_facts"],
            "context_foreign": ["/trace/executed"],
            "context_claim": ["/required_claims/0/text"],
            "context_source": ["/evidence/0/text"],
            "context_duplicate": ["/expected_tool_result"] * 2,
            "context_order": list(reversed(leaf["context_fields"])),
            "context_bool": [True],
            "context_value_echo": [{"pointer": "/expected_tool_result", "value": None}],
        }[mutation]
    elif mutation == "conflict_without_quote":
        leaf["observation"], leaf["result"] = "observed_conflict", "contradicted"
    elif mutation == "invented_quote":
        leaf["answer_anchors"] = [{"statement_id": "s1", "quote": "All excluded rules pass."}]
    elif mutation == "blank_basis":
        leaf["basis"] = "  "
    elif mutation == "long_basis":
        leaf["basis"] = "x" * 361
    else:
        raise AssertionError(mutation)
    with pytest.raises(ValueError):
        decode(packet, wire)


@pytest.mark.parametrize(
    "observation,grade",
    list(
        product(
            ["no_observed_conflict", "observed_conflict", "insufficient_packet_evidence"],
            ["met", "missing", "contradicted"],
        )
    ),
)
def test_every_policy_grade_observation_pair(corpus: Corpus, observation: str, grade: str) -> None:
    _, packet, wire = safety_witness(corpus, "S04")
    leaf = next(iter(wire["required_claims"].values()))
    leaf["observation"], leaf["result"] = observation, grade
    if observation == "observed_conflict":
        leaf["answer_anchors"] = [{"statement_id": "s1", "quote": packet["statements"][0]["text"]}]
    expected = {
        "no_observed_conflict": "met",
        "observed_conflict": "contradicted",
        "insufficient_packet_evidence": "missing",
    }[observation]
    if grade == expected:
        assert decode(packet, wire)
    else:
        with pytest.raises(ValueError):
            decode(packet, wire)


def test_complete_policy_review_ids_must_follow_trusted_statement_order(corpus: Corpus) -> None:
    _, packet, wire = safety_witness(corpus, "S04")
    packet["statements"].append({**deepcopy(packet["statements"][0]), "id": "s2"})
    for row in packet["evidence"]:
        row["cited_by"].append("s2")
    wire["statements"]["s2"] = deepcopy(wire["statements"]["s1"])
    leaf = next(iter(wire["required_claims"].values()))
    leaf["reviewed_statement_ids"] = ["s1", "s2"]
    assert decode(packet, wire)
    leaf["reviewed_statement_ids"] = ["s2", "s1"]
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_null_context_leaves_allowed_but_never_ordinary_tool_roots(corpus: Corpus) -> None:
    _, packet, wire = safety_witness(corpus, "S07")
    packet["safety_context"]["confirmed_facts"] = None
    packet["observed_tool_result"] = None
    part = wire["statements"]["s1"]["parts"][0]
    wire["statements"]["s1"]["factual"] = "unsupported"
    wire["statements"]["s1"]["citation_support"] = "unsupported"
    for key in ("factual_audit", "citation_audit"):
        part[key]["observation"], part[key]["tool_fields"] = "insufficient", []
    leaf = next(iter(wire["required_claims"].values()))
    leaf["context_fields"] = [
        "/safety_context/confirmed_facts",
        "/expected_tool_result",
        "/observed_tool_result",
    ]
    assert decode(packet, wire)
    part["factual_audit"]["tool_fields"] = ["/observed_tool_result"]
    with pytest.raises(ValueError):
        decode(packet, wire)


@pytest.mark.parametrize(
    "field",
    [
        "required_claims",
        "statements",
        "parts",
        "text",
        "observation",
        "source_anchors",
        "reference",
        "quote",
        "tool_fields",
        "basis",
    ],
)
def test_recursive_decoded_equal_duplicate_keys_reject(field: str) -> None:
    packet, wire = calibration_witness("J02")
    raw = json.dumps(wire)
    encoded = json.dumps(field) + ":"
    position = raw.index(encoded)
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(raw[position + len(encoded) :].lstrip())
    escaped_key = '"\\u' + format(ord(field[0]), "04x") + field[1:] + '"'
    duplicated = raw[:position] + escaped_key + ": " + json.dumps(value) + ", " + raw[position:]
    with pytest.raises(ValueError, match="duplicate"):
        _decode_judgment(duplicated, packet)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_constants_reject_at_nested_depth(constant: str) -> None:
    packet, wire = calibration_witness("J02")
    raw = json.dumps(wire)
    basis = wire["statements"]["s2"]["parts"][0]["factual_audit"]["basis"]
    changed = raw.replace(json.dumps(basis), constant, 1)
    with pytest.raises(ValueError):
        _decode_judgment(changed, packet)


def test_unicode_exactness_escapes_and_raw_key_order_do_not_normalize() -> None:
    packet, wire = calibration_witness("J02")
    target = "Cafe\u0301: keep the qualification."
    packet["required_claims"][0]["text"] = target
    wire["required_claims"]["c1"]["parts"][0]["text"] = target
    raw = json.dumps(dict(reversed(list(wire.items()))), ensure_ascii=True, indent=2)
    assert "\\u0301" in raw
    assert _decode_judgment(raw, packet)
    wire["required_claims"]["c1"]["parts"][0]["text"] = unicodedata.normalize("NFC", target)
    with pytest.raises(ValueError):
        decode(packet, wire)


@pytest.mark.parametrize("mutation", ["basis", "observation", "sources", "tools"])
def test_uncited_audit_requires_exact_fixed_object(mutation: str) -> None:
    packet, wire = calibration_witness("J08")
    packet["statements"][0]["citation_ids"] = []
    for row in packet["evidence"]:
        row["cited_by"] = [sid for sid in row["cited_by"] if sid != "s1"]
    wire["statements"]["s1"]["citation_support"] = "not_applicable"
    cited: dict[str, Any] = {
        "observation": "not_applicable",
        "source_anchors": [],
        "tool_fields": [],
        "basis": "No citation supplied.",
    }
    wire["statements"]["s1"]["parts"][0]["citation_audit"] = cited
    assert decode(packet, wire)
    if mutation == "basis":
        cited["basis"] += " "
    elif mutation == "observation":
        cited["observation"] = "insufficient"
    elif mutation == "sources":
        cited["source_anchors"] = [audit(packet, "explicit", ["e4"], "x")["source_anchors"][0]]
    else:
        cited["tool_fields"] = ["/observed_tool_result/status"]
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_generic_claim_count_is_packet_derived_not_four() -> None:
    packet, wire = calibration_witness("J02")
    original = deepcopy(packet["required_claims"][0])
    packet["required_claims"] = [{**original, "id": f"c{i}"} for i in range(1, 8)]
    leaf = deepcopy(wire["required_claims"]["c1"])
    wire["required_claims"] = {f"c{i}": deepcopy(leaf) for i in range(1, 8)}
    schema = judge_schema(packet)
    assert len(schema["properties"]["required_claims"]["required"]) == 7
    assert len(decode(packet, wire).required_claims) == 7


def test_generic_claim_ids_and_mapping_keep_packet_order_with_escaped_schema_refs() -> None:
    packet, wire = calibration_witness("J02")
    ids = ["z~tail", "a/path", "odd space", "c1"]
    for row, identifier in zip(packet["required_claims"], ids, strict=True):
        row["id"] = identifier
    wire["required_claims"] = dict(zip(ids, wire["required_claims"].values(), strict=True))
    schema = judge_schema(packet)
    for identifier in ids:
        ref = schema["properties"]["required_claims"]["properties"][identifier]["$ref"]
        escaped = ref.rsplit("/", 1)[1]
        assert escaped.replace("~1", "/").replace("~0", "~") in schema["$defs"]
    assert [row.id for row in decode(packet, wire).required_claims] == ids
