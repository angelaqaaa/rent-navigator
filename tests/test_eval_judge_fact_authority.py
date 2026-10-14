"""Synthetic scenario authority and strict current judge fact witnesses."""

import json
from copy import deepcopy
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from judge_wire_fixtures import grade_only_wire
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
from rent_navigator.eval.models import GoldCase
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


def fact_grade_wire(packet: dict[str, Any]) -> dict[str, Any]:
    wire = {
        "required_claims": [
            {"id": row["id"], "result": "met"} for row in packet["required_claims"]
        ],
        "statements": [
            {
                "id": row["id"],
                "factual": "supported",
                "citation_support": "supported" if row["citation_ids"] else "not_applicable",
            }
            for row in packet["statements"]
        ],
        "false_pass": False,
        "policy_violations": [],
    }

    return grade_only_wire(wire, packet)


@pytest.mark.parametrize("identifier", ["N04", "R04", "Q04"])
def test_full_approved_facts_are_preserved_but_not_echoed_in_grade_wire(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str
) -> None:
    packet = build_judge_packet(
        replace(value, approved_facts=approved_facts_from_case(cases[identifier]))
    )
    before = deepcopy(packet)
    wire = fact_grade_wire(packet)
    result = _decode_judgment(json.dumps(wire), packet)
    assert all(row.factual == "supported" for row in result.statements)
    assert packet == before
    assert packet["approved_facts"] == (
        None if identifier == "Q04" else envelope(cases[identifier])
    )
    assert "approved_facts" not in wire
    assert "fact_fields" not in json.dumps(judge_schema(packet))


@pytest.mark.parametrize("identifier", ["N04", "R04"])
def test_all_actual_fact_leaves_remain_conditioned_inputs(
    value: JudgeInput, cases: dict[str, GoldCase], identifier: str
) -> None:
    packet = fact_packet(value, cases[identifier], "Synthetic conditional scenario premise")
    expected = NOTICE_POINTERS if identifier == "N04" else RENT_POINTERS
    assert (
        list(_scalar_pointers(packet["approved_facts"]["facts"], "/approved_facts/facts"))
        == expected
    )
    assert _decode_judgment(json.dumps(fact_grade_wire(packet)), packet)
    approved = approved_facts_from_case(cases[identifier])
    assert approved is not None
    assert packet["approved_facts"]["facts"] == approved.facts.model_dump(mode="json")


@pytest.mark.parametrize(
    "field", ["basis", "fact_fields", "parts", "observation", "source_anchors", "tool_fields"]
)
def test_grade_wire_rejects_fact_witness_fields_with_known_billing(
    value: JudgeInput, field: str
) -> None:
    test = harness(value)
    wire = wire_payload(result(value), value)
    wire["statements"][next(iter(wire["statements"]))][field] = ["Synthetic forbidden field"]
    original = json.dumps(wire, indent=2)
    test.port.response = message(value, content=[{"type": "text", "text": original}])
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "invalid_generated_output"
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd == test.budget.committed_usd == Decimal("0.003")
    assert accounting.cost.reserved_cost_usd == Decimal("0.21384")
    assert test.records()[-1].value["content"][0]["text"] == original
    assert "Synthetic forbidden field" not in test.metadata.getvalue()
    test.verify(accounting)


def test_unknown_service_is_not_inferred_from_a_computed_deadline(
    value: JudgeInput, cases: dict[str, GoldCase]
) -> None:
    packet = fact_packet(value, cases["N04"], "Synthetic conditional scenario premise")
    assert packet["approved_facts"]["facts"]["served_on"] is None
    assert packet["observed_tool_result"]["latest_deemed_service_on"] is not None
    assert _decode_judgment(json.dumps(fact_grade_wire(packet)), packet)


def test_identity_binds_grade_only_wire_and_unchanged_packet_capacity() -> None:
    assert judge_module._PACKET_POLICY["version"] == 3
    assert judge_module._WIRE_POLICY["version"] == 8
    assert (
        judge_module.judge_config_hash()
        != "0ce355b473863850ffa7449643cba30ab8d8d2563fccb3e3c912fa7272a117a7"
    )
    assert "approved_facts_schema" in judge_module._PACKET_POLICY
    assert MODEL_POLICIES[JUDGE_MODEL].max_output_tokens == 16384
