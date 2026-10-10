"""Synthetic scenario authority and strict current judge fact witnesses."""

import json
from copy import deepcopy
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from judge_schema_resolution import resolve_judge_schema
from pydantic import ValidationError
from test_eval_judge import harness, message, result, wire_payload
from test_eval_judge import isolated_count_replay as isolated_count_replay

import rent_navigator.eval.judge as judge_module
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    _decode_judgment,
    _scalar_pointers,
    build_judge_packet,
    build_smoke_input,
    judge_schema,
)
from rent_navigator.eval.models import GoldCase, JudgeResult
from rent_navigator.eval.runner import (
    APPROVED_FACTS_ADAPTER,
    ApprovedNoticeFacts,
    ApprovedRentFacts,
    JudgeFailure,
    JudgeInput,
    JudgeSafetyContext,
    approved_facts_from_case,
    approved_facts_from_request,
    validate_approved_facts,
)
from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.models import NoticeFacts, RentFacts


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture(scope="module")
def cases(corpus: Corpus) -> dict[str, GoldCase]:
    return {case.id: case for case in load_gold(Path("eval"), corpus).cases}


@pytest.fixture
def value(corpus: Corpus, cases: dict[str, GoldCase]) -> JudgeInput:
    return build_smoke_input(cases["R04"], corpus)


def test_smoke_packet_supplies_complete_original_approved_r04_facts(value: JudgeInput) -> None:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    assert case.request.mode == "rent"
    assert build_judge_packet(value)["approved_facts"] == {
        "kind": "rent",
        "facts": case.request.facts.model_dump(mode="json"),
    }


def test_current_packet_rejects_missing_required_approved_facts(value: JudgeInput) -> None:
    packet = build_judge_packet(value)
    packet.pop("approved_facts", None)
    with pytest.raises(ValueError):
        judge_schema(packet)


def test_judge_input_requires_explicit_null_or_approved_envelope(value: JudgeInput) -> None:
    arguments = {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != "approved_facts"
    }
    with pytest.raises(TypeError):
        JudgeInput(**arguments)


def envelope(case: GoldCase) -> dict[str, Any]:
    supplied = approved_facts_from_case(case)
    assert supplied is not None
    return supplied.model_dump(mode="json")


@pytest.mark.parametrize("identifier,model", [("N04", NoticeFacts), ("R04", RentFacts)])
def test_declared_kind_validates_complete_existing_facts(
    cases: dict[str, GoldCase], identifier: str, model: type[NoticeFacts]
) -> None:
    wire = envelope(cases[identifier])
    parsed = APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))
    assert parsed is not None and type(parsed.facts) is model
    assert parsed.model_dump(mode="json") == wire
    assert approved_facts_from_request(cases[identifier].request) == parsed


@pytest.mark.parametrize("identifier", ["Q01", "Q04"])
def test_question_premises_are_explicit_null(cases: dict[str, GoldCase], identifier: str) -> None:
    assert approved_facts_from_case(cases[identifier]) is None
    assert APPROVED_FACTS_ADAPTER.validate_json("null") is None


@pytest.mark.parametrize("identifier,wrong", [("N04", "rent"), ("R04", "notice")])
def test_wrong_declared_kind_does_not_guess_overlapping_fact_union(
    cases: dict[str, GoldCase], identifier: str, wrong: str
) -> None:
    wire = envelope(cases[identifier])
    wire["kind"] = wrong
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


def test_rent_subclass_cannot_be_serialized_as_notice_and_lose_rent_fields(
    cases: dict[str, GoldCase], value: JudgeInput
) -> None:
    rent = approved_facts_from_case(cases["R04"])
    notice = approved_facts_from_case(cases["N04"])
    assert isinstance(rent, ApprovedRentFacts) and isinstance(notice, ApprovedNoticeFacts)
    assert isinstance(rent.facts, NoticeFacts)
    with pytest.raises(ValidationError):
        ApprovedNoticeFacts(kind="notice", facts=rent.facts)
    with pytest.raises(ValidationError):
        ApprovedRentFacts(kind="rent", facts=notice.facts)  # type: ignore[arg-type]
    invalid = ApprovedNoticeFacts.model_construct(kind="notice", facts=rent.facts)
    for validate in (validate_approved_facts, APPROVED_FACTS_ADAPTER.validate_python):
        with pytest.raises(ValidationError):
            validate(invalid)
    with pytest.raises(ValidationError):
        replace(value, approved_facts=invalid)
    # Passing a request instance with this same subclass must not strip the full payload either.
    wrong_request = cases["N04"].request.model_copy(update={"facts": rent.facts})
    with pytest.raises(ValidationError):
        approved_facts_from_request(wrong_request)


@pytest.mark.parametrize(
    "mutation",
    ["missing_kind", "missing_facts", "extra", "uppercase", "question", "bool_kind", "null_facts"],
)
def test_envelope_has_no_defaults_extra_keys_or_kind_coercion(
    cases: dict[str, GoldCase], mutation: str
) -> None:
    wire = envelope(cases["N04"])
    if mutation.startswith("missing_"):
        wire.pop(mutation.removeprefix("missing_"))
    elif mutation == "extra":
        wire["case_id"] = "N04"
    elif mutation == "null_facts":
        wire["facts"] = None
    else:
        wire["kind"] = {"uppercase": "NOTICE", "question": "question", "bool_kind": True}[mutation]
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize("wire", ["none", "unknown", True, False, 0, [], {}])
def test_null_envelope_is_not_none_unknown_or_another_json_value(wire: object) -> None:
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    "field",
    ["effective_on", "served_on", "current_cents", "proposed_cents", "tenancy_start", "last_date"],
)
def test_nullable_fact_keys_are_required_on_real_json_boundary(
    cases: dict[str, GoldCase], field: str
) -> None:
    wire = envelope(cases["R04"])
    if field == "last_date":
        wire["facts"]["last_increase"].pop("date")
    else:
        wire["facts"].pop(field)
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize("field", ["current_cents", "proposed_cents"])
@pytest.mark.parametrize("bad", [True, False, "100000", 0, -1])
def test_fact_money_keeps_public_strict_positive_cents(
    cases: dict[str, GoldCase], field: str, bad: object
) -> None:
    wire = envelope(cases["R04"])
    wire["facts"][field] = bad
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-9-1", "2026-09-01T00:00:00", "unknown", True])
def test_fact_dates_keep_existing_wire_validation(cases: dict[str, GoldCase], bad: object) -> None:
    wire = envelope(cases["N04"])
    wire["facts"]["effective_on"] = bad
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


def test_unfavorable_service_chronology_is_valid_but_inconsistent_last_increase_is_not(
    cases: dict[str, GoldCase],
) -> None:
    wire = envelope(cases["R04"])
    wire["facts"]["served_on"] = "2027-01-01"
    assert APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire)) is not None
    wire["facts"]["last_increase"]["date"] = "2024-08-31"
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize("state", ["none", "unknown"])
def test_none_and_unknown_last_increase_remain_distinct_null_date_states(
    cases: dict[str, GoldCase], state: str
) -> None:
    wire = envelope(cases["R04"])
    wire["facts"]["last_increase"] = {"state": state, "date": None}
    parsed = APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))
    assert parsed is not None and isinstance(parsed.facts, RentFacts)
    assert parsed.facts.last_increase.state == state and parsed.facts.last_increase.date is None
    wire["facts"]["last_increase"]["date"] = "2025-09-01"
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


def test_known_last_increase_requires_date_and_rent_form_preserves_case(
    cases: dict[str, GoldCase],
) -> None:
    wire = envelope(cases["R04"])
    wire["facts"]["last_increase"]["date"] = None
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))
    wire = envelope(cases["R04"])
    wire["facts"]["form"] = "n1"
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


@pytest.mark.parametrize("changed", ["request", "expected_args", "expected_extraction"])
def test_original_case_equalities_are_revalidated_before_using_request_facts(
    cases: dict[str, GoldCase], changed: str
) -> None:
    case = cases["R04"]
    assert case.request.mode == "rent" and case.expected_extract is not None
    extraction = case.expected_extract
    foreign = case.request.facts.model_copy(update={"proposed_cents": 105000})
    if changed == "request":
        case = case.model_copy(
            update={"request": case.request.model_copy(update={"facts": foreign})}
        )
    elif changed == "expected_args":
        case = case.model_copy(update={"expected_tool_args": foreign})
    else:
        case = case.model_copy(
            update={"expected_extract": extraction.model_copy(update={"proposed_cents": 105000})}
        )
    with pytest.raises(ValidationError):
        approved_facts_from_case(case)


@pytest.mark.parametrize("approved,confirmed", [(False, True), (True, False), (True, True)])
def test_safety_context_disagreement_including_null_is_rejected(
    value: JudgeInput, cases: dict[str, GoldCase], approved: bool, confirmed: bool
) -> None:
    facts = approved_facts_from_case(cases["R04"])
    assert facts is not None
    context_facts = facts.facts if confirmed else None
    if approved and confirmed:
        context_facts = facts.facts.model_copy(update={"proposed_cents": 105000})
    safety = JudgeSafetyContext(confirmed_facts=context_facts, expected_tool_status=None)
    with pytest.raises(ValueError):
        replace(value, approved_facts=facts if approved else None, safety_context=safety)


@pytest.mark.parametrize("identifier", ["N04", "R04", "Q04"])
def test_exact_safety_facts_agreement_is_checked_again_at_packet_boundary(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str
) -> None:
    approved = approved_facts_from_case(cases[identifier])
    safety = JudgeSafetyContext(
        confirmed_facts=approved.facts if approved is not None else None,
        expected_tool_status=None,
    )
    packet = build_judge_packet(replace(value, approved_facts=approved, safety_context=safety))
    assert packet["safety_context"]["confirmed_facts"] == (
        approved.facts.model_dump(mode="json") if approved is not None else None
    )
    packet["safety_context"]["confirmed_facts"] = (
        None if approved is not None else envelope(cases["R04"])["facts"]
    )
    with pytest.raises(ValueError):
        judge_schema(packet)


NOTICE_POINTERS = [
    "/approved_facts/facts/effective_on",
    "/approved_facts/facts/scope/ordinary",
    "/approved_facts/facts/scope/period_start",
    "/approved_facts/facts/served_on",
    "/approved_facts/facts/service_method",
]
RENT_POINTERS = [
    "/approved_facts/facts/current_cents",
    "/approved_facts/facts/effective_on",
    "/approved_facts/facts/form",
    "/approved_facts/facts/guideline_status",
    "/approved_facts/facts/last_increase/date",
    "/approved_facts/facts/last_increase/state",
    "/approved_facts/facts/proposed_cents",
    "/approved_facts/facts/scope/ordinary",
    "/approved_facts/facts/scope/period_start",
    "/approved_facts/facts/served_on",
    "/approved_facts/facts/service_method",
    "/approved_facts/facts/tenancy_start",
]


def fact_packet(
    value: JudgeInput, case: GoldCase, text: str, *, cited: bool = False
) -> dict[str, Any]:
    packet = build_judge_packet(value)
    packet["approved_facts"] = envelope(case)
    packet["expected_tool_result"] = (
        case.expected_tool_result.model_dump(mode="json") if case.expected_tool_result else None
    )
    packet["observed_tool_result"] = deepcopy(packet["expected_tool_result"])
    packet["evidence"] = [row for row in packet["evidence"] if row["canonical"]]
    for index, row in enumerate(packet["evidence"], 1):
        row["reference"], row["cited_by"] = f"e{index}", ["s1"] if cited and index == 1 else []
    packet["required_claims"] = [{"id": "c1", "text": text}]
    packet["statements"] = [
        {"id": "s1", "text": text, "citation_ids": [packet["evidence"][0]["id"]] if cited else []}
    ]
    return packet


def fact_audit(observation: str, pointers: list[str]) -> dict[str, Any]:
    return {
        "observation": observation,
        "source_anchors": [],
        "tool_fields": [],
        "fact_fields": pointers,
        "basis": "The independently supplied scenario premise is the conditional fact witness.",
    }


def fact_wire(packet: dict[str, Any], pointers: list[str]) -> dict[str, Any]:
    text = packet["statements"][0]["text"]
    cited = bool(packet["statements"][0]["citation_ids"])
    citation = (
        fact_audit("insufficient", [])
        if cited
        else {
            "observation": "not_applicable",
            "source_anchors": [],
            "tool_fields": [],
            "fact_fields": [],
            "basis": "No citation supplied.",
        }
    )
    return {
        "required_claims": {
            "c1": {
                "kind": "ordinary_coverage",
                "result": "met",
                "parts": [
                    {
                        "text": text,
                        "observation": "explicit",
                        "answer_anchors": [{"statement_id": "s1", "quote": text}],
                        "basis": "The statement explicitly states the full coverage target.",
                    }
                ],
            }
        },
        "statements": {
            "s1": {
                "factual": "supported",
                "citation_support": "unsupported" if cited else "not_applicable",
                "parts": [
                    {
                        "text": text,
                        "factual_audit": fact_audit("explicit", pointers),
                        "citation_audit": citation,
                    }
                ],
            }
        },
        "false_pass": False,
        "policy_violations": [],
    }


def decode(packet: dict[str, Any], wire: dict[str, Any]) -> JudgeResult:
    return _decode_judgment(json.dumps(wire, ensure_ascii=False), packet)


@pytest.mark.parametrize(
    "identifier,pointer",
    [("N04", pointer) for pointer in NOTICE_POINTERS]
    + [("R04", pointer) for pointer in RENT_POINTERS],
)
def test_every_real_scalar_or_null_fact_leaf_can_witness_its_conditional_premise(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str, pointer: str
) -> None:
    wire_facts = envelope(cases[identifier])["facts"]
    scalar: Any = wire_facts
    for key in pointer.removeprefix("/approved_facts/facts/").split("/"):
        scalar = scalar[key]
    text = f"The approved scenario's {pointer.rsplit('/', 1)[1]} is {json.dumps(scalar)}."
    packet = fact_packet(value, cases[identifier], text)
    assert decode(packet, fact_wire(packet, [pointer])).statements[0].factual == "supported"


@pytest.mark.parametrize("identifier,pointers", [("N04", NOTICE_POINTERS), ("R04", RENT_POINTERS)])
def test_schema_enum_and_bound_follow_all_actual_fact_leaves(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str, pointers: list[str]
) -> None:
    packet = fact_packet(value, cases[identifier], "The approved scenario supplies these premises.")
    schema = judge_schema(packet)
    expanded = resolve_judge_schema(schema)
    part = expanded["properties"]["statements"]["properties"]["s1"]["properties"]["parts"]["items"]
    for name in ("factual_audit", "citation_audit"):
        audit_schema = part["properties"][name]
        assert "fact_fields" in audit_schema["required"]
        array = audit_schema["properties"]["fact_fields"]
        if name == "factual_audit":
            assert array["items"]["enum"] == pointers
        else:
            assert "enum" not in array["items"]
    assert _scalar_pointers(packet["approved_facts"]["facts"], "/approved_facts/facts") == tuple(
        pointers
    )


@pytest.mark.parametrize("identifier,pointers", [("N04", NOTICE_POINTERS), ("R04", RENT_POINTERS)])
def test_all_real_fact_leaves_can_be_retained_together_without_an_arbitrary_cap(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str, pointers: list[str]
) -> None:
    text = (
        "The approved scenario premises are "
        + json.dumps(envelope(cases[identifier])["facts"])
        + "."
    )
    packet = fact_packet(value, cases[identifier], text)
    assert decode(packet, fact_wire(packet, pointers)).statements[0].factual == "supported"


@pytest.mark.parametrize("container", ["facts", "scope", "last_increase"])
def test_fact_model_rejects_nested_extra_fields(cases: dict[str, GoldCase], container: str) -> None:
    wire = envelope(cases["R04"])
    target = wire["facts"] if container == "facts" else wire["facts"][container]
    target["extra"] = "synthetic"
    with pytest.raises(ValidationError):
        APPROVED_FACTS_ADAPTER.validate_json(json.dumps(wire))


BAD_POINTERS: list[object] = [
    ["/approved_facts"],
    ["/approved_facts/kind"],
    ["/approved_facts/facts"],
    ["/approved_facts/facts/scope"],
    ["/approved_facts/facts/last_increase"],
    ["/approved_facts/facts/proposed_cents/missing"],
    ["/approved_facts/facts/missing"],
    ["/approved_facts/facts/*"],
    ["/approved_facts/facts/scope/~1ordinary"],
    ["/approved_facts/facts/scope/~0ordinary"],
    ["/approved_facts/facts/proposed_cents", "/approved_facts/facts/proposed_cents"],
    ["/approved_facts/facts/proposed_cents", "/approved_facts/facts/current_cents"],
    [True],
    [None],
    [100000],
    "/approved_facts/facts/proposed_cents",
    None,
]


@pytest.mark.parametrize("pointers", BAD_POINTERS)
def test_invalid_fact_pointers_are_rejected_without_repair(
    value: JudgeInput, cases: dict[str, GoldCase], pointers: object
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario supplies CAD 1024.00 as proposed rent.")
    wire = fact_wire(packet, ["/approved_facts/facts/proposed_cents"])
    wire["statements"]["s1"]["parts"][0]["factual_audit"]["fact_fields"] = pointers
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_scalar_enumeration_escapes_rfc6901_keys_without_relaxing_fact_schema() -> None:
    assert _scalar_pointers({"a~b": None, "a/b": ["x"]}, "/approved_facts/facts") == (
        "/approved_facts/facts/a~1b/0",
        "/approved_facts/facts/a~0b",
    )


@pytest.mark.parametrize("observation", ["explicit", "necessary_implication", "conflicting"])
def test_noninsufficient_factual_audit_rejects_three_empty_witness_arrays(
    value: JudgeInput, cases: dict[str, GoldCase], observation: str
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario proposes CAD 1024.00.")
    wire = fact_wire(packet, [])
    leaf = wire["statements"]["s1"]
    leaf["factual"] = "contradicted" if observation == "conflicting" else "supported"
    leaf["parts"][0]["factual_audit"]["observation"] = observation
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_insufficient_factual_audit_can_retain_empty_or_inadequate_evidence(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario may have an unknown qualification.")
    wire = fact_wire(packet, [])
    wire["statements"]["s1"]["factual"] = "unsupported"
    audit = wire["statements"]["s1"]["parts"][0]["factual_audit"]
    audit["observation"] = "insufficient"
    assert decode(packet, wire).statements[0].factual == "unsupported"
    audit["fact_fields"] = ["/approved_facts/facts/scope/ordinary"]
    assert decode(packet, wire).statements[0].factual == "unsupported"


@pytest.mark.parametrize("observation", ["explicit", "necessary_implication", "conflicting"])
@pytest.mark.parametrize("witness", ["fact_only", "tool_only"])
def test_cited_audit_still_requires_its_own_source_anchor(
    value: JudgeInput, cases: dict[str, GoldCase], observation: str, witness: str
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario proposes CAD 1024.00.", cited=True)
    wire = fact_wire(packet, ["/approved_facts/facts/proposed_cents"])
    leaf = wire["statements"]["s1"]
    leaf["citation_support"] = "unsupported" if observation == "conflicting" else "supported"
    audit = leaf["parts"][0]["citation_audit"]
    audit["observation"] = observation
    audit["fact_fields"] = (
        ["/approved_facts/facts/proposed_cents"] if witness == "fact_only" else []
    )
    audit["tool_fields"] = (
        ["/expected_tool_result/cap_cents_exact"] if witness == "tool_only" else []
    )
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_fact_pointer_never_broadens_own_citation_membership(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(
        value, cases["R04"], "The supplied scenario proposes CAD 1024.00.", cited=True
    )
    wire = fact_wire(packet, ["/approved_facts/facts/proposed_cents"])
    leaf = wire["statements"]["s1"]
    leaf["citation_support"] = "supported"
    audit = leaf["parts"][0]["citation_audit"]
    audit["observation"] = "explicit"
    audit["fact_fields"] = ["/approved_facts/facts/proposed_cents"]
    foreign = packet["evidence"][1]
    audit["source_anchors"] = [{"reference": foreign["reference"], "quote": foreign["text"]}]
    with pytest.raises(ValueError):
        decode(packet, wire)


@pytest.mark.parametrize("audit_name", ["factual_audit", "citation_audit"])
def test_current_wire_requires_fact_fields_in_every_audit(
    value: JudgeInput, cases: dict[str, GoldCase], audit_name: str
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario proposes CAD 1024.00.")
    wire = fact_wire(packet, ["/approved_facts/facts/proposed_cents"])
    wire["statements"]["s1"]["parts"][0][audit_name].pop("fact_fields")
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_uncited_citation_audit_cannot_carry_even_a_valid_fact_pointer(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario proposes CAD 1024.00.")
    wire = fact_wire(packet, ["/approved_facts/facts/proposed_cents"])
    wire["statements"]["s1"]["parts"][0]["citation_audit"]["fact_fields"] = [
        "/approved_facts/facts/proposed_cents"
    ]
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_null_envelope_has_no_fact_pointers_or_inferred_zero(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(
        value, cases["R04"], "The provided packet does not establish a scenario amount."
    )
    packet["approved_facts"] = None
    wire = fact_wire(packet, [])
    wire["statements"]["s1"]["factual"] = "unsupported"
    audit = wire["statements"]["s1"]["parts"][0]["factual_audit"]
    audit["observation"] = "insufficient"
    assert decode(packet, wire).statements[0].factual == "unsupported"
    audit["fact_fields"] = ["/approved_facts/facts/proposed_cents"]
    with pytest.raises(ValueError):
        decode(packet, wire)


def test_duplicate_decoded_fact_fields_key_is_rejected(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(value, cases["R04"], "The scenario proposes CAD 1024.00.")
    raw = json.dumps(fact_wire(packet, ["/approved_facts/facts/proposed_cents"]))
    raw = raw.replace('"fact_fields":', '"\\u0066act_fields": [], "fact_fields":', 1)
    with pytest.raises(ValueError):
        _decode_judgment(raw, packet)


@pytest.mark.parametrize(
    "identifier,wrong_part,correct_part,fact_pointer,tool_pointer",
    [
        (
            "R04",
            "The supplied scenario proposes CAD 1050.00, ",
            "and its correct calculated cap is CAD 1021.00.",
            "/approved_facts/facts/proposed_cents",
            "/expected_tool_result/cap_cents_exact",
        ),
        (
            "N04",
            "The supplied effective date is 2027-02-01, ",
            "and the correct latest hand-service date is 2026-12-01.",
            "/approved_facts/facts/effective_on",
            "/expected_tool_result/latest_deemed_service_on",
        ),
    ],
)
def test_handwritten_wrong_input_does_not_become_true_from_a_correct_calculated_value(
    value: JudgeInput,
    cases: dict[str, GoldCase],
    identifier: str,
    wrong_part: str,
    correct_part: str,
    fact_pointer: str,
    tool_pointer: str,
) -> None:
    # Grades are handwritten synthetic expectations, never a model accuracy measurement.
    packet = fact_packet(value, cases[identifier], wrong_part + correct_part)
    wire = fact_wire(packet, [fact_pointer])
    wrong = wire["statements"]["s1"]["parts"][0]
    wrong["text"] = wrong_part
    wrong["factual_audit"] = fact_audit("conflicting", [fact_pointer])
    correct = deepcopy(wrong)
    correct["text"] = correct_part
    correct["factual_audit"] = fact_audit("explicit", [])
    correct["factual_audit"]["tool_fields"] = [tool_pointer]
    correct["factual_audit"]["basis"] = (
        "This separate clause matches the supplied correct tool result."
    )
    wire["statements"]["s1"]["parts"] = [wrong, correct]
    wire["statements"]["s1"]["factual"] = "contradicted"
    judged = decode(packet, wire)
    assert judged.statements[0].factual == "contradicted"
    assert packet["approved_facts"] == envelope(cases[identifier])
    assert packet["expected_tool_result"] == packet["observed_tool_result"]


def test_unknown_actual_service_is_distinct_from_computed_latest_service_date(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(
        value, cases["N04"], "The approved scenario does not supply an actual service date."
    )
    assert packet["approved_facts"]["facts"]["served_on"] is None
    assert packet["approved_facts"]["facts"]["service_method"] == "hand"
    assert packet["expected_tool_result"]["latest_deemed_service_on"] == "2026-12-01"
    assert packet["expected_tool_result"]["deemed_served_on"] is None
    assert packet["expected_tool_result"]["notice_days"] is None
    assert (
        decode(packet, fact_wire(packet, ["/approved_facts/facts/served_on"])).statements[0].factual
        == "supported"
    )


def test_current_identity_changes_packet_wire_and_facts_schema_without_changing_capacity() -> None:
    assert judge_module._PACKET_POLICY["version"] == 3
    assert judge_module._WIRE_POLICY["version"] == 6
    identity = judge_module.judge_config_hash()
    assert identity == judge_module.judge_config_hash()
    assert identity != "0ce355b473863850ffa7449643cba30ab8d8d2563fccb3e3c912fa7272a117a7"
    packet_fields = judge_module._PACKET_POLICY["fields"]
    assert isinstance(packet_fields, list) and "approved_facts" in packet_fields
    assert "approved_facts_schema" in judge_module._PACKET_POLICY
    assert MODEL_POLICIES[JUDGE_MODEL].max_output_tokens == 8192


@pytest.mark.parametrize("mutation", ["missing", "forged"])
def test_invalid_fact_witness_keeps_known_generation_cost_and_original_raw_vote(
    value: JudgeInput, mutation: str
) -> None:
    test = harness(value)
    wire = wire_payload(result(value), value)
    audit = wire["statements"]["s1"]["parts"][0]["factual_audit"]
    if mutation == "missing":
        audit.pop("fact_fields")
    else:
        audit["fact_fields"] = ["/approved_facts/facts/synthetic-forged-field"]
    raw_vote = "\n " + json.dumps(wire, indent=2) + " \n"
    test.port.response = message(value, content=[{"type": "text", "text": raw_vote}])
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "invalid_generated_output"
    assert not hasattr(caught.value, "judgment")
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd == test.budget.committed_usd == Decimal("0.003")
    assert accounting.cost.reserved_cost_usd == Decimal("0.13192")
    assert accounting.cost.usage_complete
    assert len([record for record in accounting.records if record.record_kind == "endpoint"]) == 1
    assert test.records()[-1].value["content"][0]["text"] == raw_vote
    assert "synthetic-forged-field" not in test.metadata.getvalue()
    test.verify(accounting)
