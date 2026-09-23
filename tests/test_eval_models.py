"""Synthetic wire-boundary tests for gold, judgments and result records."""

import json
from copy import deepcopy
from typing import Any

import pytest
from eval_fixtures import (
    TOY_ATTEMPT_ID,
    TOY_HASH,
    TOY_RUN_ID,
    TOY_SOURCE_SHA,
    TOY_TRACE_ID,
    toy_gold_rows,
)
from pydantic import ValidationError

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.models import (
    Activation,
    DeterministicAssertion,
    GoldApproval,
    GoldCase,
    JudgeResult,
    ResultRow,
)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def rows(corpus: Corpus) -> list[dict[str, Any]]:
    return toy_gold_rows(corpus)


def judgment() -> dict[str, Any]:
    return {
        "required_claims": [{"id": "c1", "result": "met"}],
        "statements": [{"id": "s1", "factual": "supported", "citation_support": "not_applicable"}],
        "false_pass": False,
        "policy_violations": [],
    }


def result_row() -> dict[str, Any]:
    return {
        "run_id": TOY_RUN_ID,
        "case_id": "Q01",
        "arm": "production",
        "repeat": 0,
        "source_sha": TOY_SOURCE_SHA,
        "config_hash": TOY_HASH,
        "corpus_hash": TOY_HASH,
        "gold_hash": TOY_HASH,
        "pricing_hash": TOY_HASH,
        "attempt_id": TOY_ATTEMPT_ID,
        "trace_ids": [TOY_TRACE_ID],
        "retrieved_ids": [],
        "response": None,
        "actual_extract": None,
        "actual_tool_args": None,
        "actual_tool_result": None,
        "deterministic_assertions": [{"id": "synthetic", "passed": False}],
        "judge": None,
        "classification": "error",
        "latency_ms": 0,
        "input_tokens": None,
        "output_tokens": None,
        "serving_cost_usd": None,
        "judge_cost_usd": None,
        "usage_complete": False,
    }


def approval() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "draft",
        "gold_sha256": TOY_HASH,
        "corpus_hash": TOY_HASH,
        "approved_by": None,
        "approved_at_utc": None,
    }


def test_gold_rows_round_trip_without_changing_json(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        parsed = GoldCase.model_validate_json(json.dumps(row))
        assert parsed.model_dump(mode="json") == row
        assert GoldCase.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("field", list(GoldCase.model_fields))
def test_every_gold_field_is_required(rows: list[dict[str, Any]], field: str) -> None:
    del rows[0][field]
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[0]))


@pytest.mark.parametrize("field", ["letter", "expected_extract", "expected_tool_args"])
@pytest.mark.parametrize("value", ["null", "none", "unknown", False])
def test_nullable_gold_objects_are_not_sentinels(
    rows: list[dict[str, Any]], field: str, value: object
) -> None:
    rows[10][field] = value
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[10]))


def test_gold_rejects_extra_fields(rows: list[dict[str, Any]]) -> None:
    rows[0]["extra"] = True
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[0]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Q01"),
        ("kind", "notice"),
        ("expected_tool", "notice_deadline_check"),
        ("expected_status", "passes_checked_rules"),
        ("expected_tool_args", None),
        ("expected_tool_result", None),
    ],
)
def test_fact_gold_requires_consistent_expectations(
    rows: list[dict[str, Any]], field: str, value: object
) -> None:
    rows[0][field] = value
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[0]))


def test_expected_args_must_equal_request_facts(rows: list[dict[str, Any]]) -> None:
    row = deepcopy(rows[0])
    row["expected_tool_args"]["current_cents"] = 123
    with pytest.raises(ValidationError, match="arguments"):
        GoldCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("field", ["expected_tool", "expected_tool_args", "expected_tool_result"])
def test_question_requires_null_tool_fields(rows: list[dict[str, Any]], field: str) -> None:
    rows[10][field] = rows[0][field]
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[10]))


@pytest.mark.parametrize("field", ["letter", "expected_extract"])
def test_letter_fields_required_together(rows: list[dict[str, Any]], field: str) -> None:
    rows[1][field] = None
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[1]))


@pytest.mark.parametrize("field", ["current_cents", "proposed_cents", "effective_on"])
def test_letter_extraction_matches_request(rows: list[dict[str, Any]], field: str) -> None:
    rows[1]["expected_extract"][field] = "2026-10-01" if field == "effective_on" else 123
    with pytest.raises(ValidationError, match="extraction"):
        GoldCase.model_validate_json(json.dumps(rows[1]))


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, -1, 3, None])
def test_relevance_grades_are_strict(rows: list[dict[str, Any]], value: object) -> None:
    row = rows[10]
    row["relevance"][next(iter(row["relevance"]))] = value
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(row))


def test_question_requires_grade_two_and_facts_have_no_relevance(
    rows: list[dict[str, Any]],
) -> None:
    rows[10]["relevance"] = {identifier: 1 for identifier in rows[10]["relevance"]}
    rows[0]["relevance"] = {TOY_HASH: 2}
    for row in (rows[0], rows[10]):
        with pytest.raises(ValidationError):
            GoldCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [{"id": "", "text": "text"}],
        [{"id": "id", "text": "  "}],
        [{"id": "id", "text": "text", "extra": True}],
        [{"id": "id", "text": "one"}, {"id": "id", "text": "two"}],
        [{"id": "one", "text": "same"}, {"id": "two", "text": "same"}],
        [{"id": str(index), "text": str(index)} for index in range(5)],
    ],
)
def test_claim_bounds_and_uniqueness(
    rows: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> None:
    rows[0]["required_claims"] = claims
    with pytest.raises(ValidationError):
        GoldCase.model_validate_json(json.dumps(rows[0]))


@pytest.mark.parametrize("field", list(JudgeResult.model_fields))
def test_judge_fields_are_required(field: str) -> None:
    value = judgment()
    del value[field]
    with pytest.raises(ValidationError):
        JudgeResult.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_judge_boolean_is_strict(value: object) -> None:
    payload = judgment()
    payload["false_pass"] = value
    with pytest.raises(ValidationError):
        JudgeResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["required_claims", "statements", "policy_violations"])
def test_judge_rejects_duplicate_entries(field: str) -> None:
    payload = judgment()
    payload[field] = ["S01", "S01"] if field == "policy_violations" else payload[field] * 2
    with pytest.raises(ValidationError):
        JudgeResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("value", ["S00", "S09", "s01", "unknown", 1])
def test_judge_rejects_unknown_policy_identifiers(value: object) -> None:
    payload = judgment()
    payload["policy_violations"] = [value]
    with pytest.raises(ValidationError):
        JudgeResult.model_validate_json(json.dumps(payload))


def test_judge_context_requires_exact_coverage() -> None:
    parsed = JudgeResult.model_validate_json(json.dumps(judgment()))
    assert parsed.validate_coverage(["c1"], ["s1"]) is parsed
    for claims, statements in (
        ([], ["s1"]),
        (["c1", "c2"], ["s1"]),
        (["c1"], []),
        (["c1"], ["s1", "s2"]),
        (["c1", "c1"], ["s1"]),
        (["c1"], ["s1", "s1"]),
    ):
        with pytest.raises(ValueError, match="coverage"):
            parsed.validate_coverage(claims, statements)


@pytest.mark.parametrize("field", list(ResultRow.model_fields))
def test_every_result_field_is_required(field: str) -> None:
    value = result_row()
    del value[field]
    with pytest.raises(ValidationError):
        ResultRow.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repeat", True),
        ("repeat", "0"),
        ("repeat", -1),
        ("repeat", 5),
        ("input_tokens", False),
        ("output_tokens", "0"),
        ("input_tokens", -1),
        ("latency_ms", -0.1),
        ("latency_ms", True),
        ("latency_ms", "1"),
        ("latency_ms", float("inf")),
        ("latency_ms", float("nan")),
        ("judge_cost_usd", "-1"),
        ("judge_cost_usd", "NaN"),
        ("run_id", TOY_RUN_ID.replace("-", "")),
        ("source_sha", "1" * 39),
        ("gold_hash", "A" * 64),
        ("trace_ids", [TOY_TRACE_ID, TOY_TRACE_ID]),
        ("retrieved_ids", [TOY_HASH, TOY_HASH]),
        ("usage_complete", "false"),
        ("classification", "pass"),
        ("case_id", "S01"),
        ("arm", "test"),
    ],
)
def test_result_types_bounds_and_canonical_ids(field: str, value: object) -> None:
    row = result_row()
    row[field] = value
    with pytest.raises(ValidationError):
        ResultRow.model_validate_json(json.dumps(row))


def test_result_cost_serialization_is_exact_and_missing_usage_stays_null() -> None:
    row = result_row()
    parsed = ResultRow.model_validate_json(json.dumps(row))
    assert parsed.model_dump(mode="json")["serving_cost_usd"] is None
    row.update(
        input_tokens=1000,
        output_tokens=100,
        serving_cost_usd="0.001500",
        judge_cost_usd="0.00",
        usage_complete=True,
    )
    parsed = ResultRow.model_validate_json(json.dumps(row))
    encoded = parsed.model_dump(mode="json")
    assert encoded["serving_cost_usd"] == "0.0015"
    assert encoded["judge_cost_usd"] == "0"
    assert ResultRow.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "changes",
    [
        {"serving_cost_usd": "0"},
        {"usage_complete": True},
        {"input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0, "serving_cost_usd": "0"},
    ],
)
def test_result_rejects_missing_usage_as_free(changes: dict[str, Any]) -> None:
    row = result_row()
    row.update(changes)
    with pytest.raises(ValidationError, match="usage completeness"):
        ResultRow.model_validate_json(json.dumps(row))


def test_result_assertions_have_unique_nonempty_ids_and_strict_booleans() -> None:
    for value in ({"id": "", "passed": True}, {"id": "x", "passed": 1}):
        with pytest.raises(ValidationError):
            DeterministicAssertion.model_validate_json(json.dumps(value))
    row = result_row()
    row["deterministic_assertions"] *= 2
    with pytest.raises(ValidationError, match="unique"):
        ResultRow.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("field", list(GoldApproval.model_fields))
def test_approval_fields_are_required(field: str) -> None:
    value = approval()
    del value[field]
    with pytest.raises(ValidationError):
        GoldApproval.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, 2])
def test_schema_version_is_integer_one(value: object) -> None:
    manifest = approval()
    manifest["schema_version"] = value
    with pytest.raises(ValidationError):
        GoldApproval.model_validate_json(json.dumps(manifest))
    with pytest.raises(ValidationError):
        Activation.model_validate_json(
            json.dumps({"schema_version": value, "baseline_phase": "pending"})
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-22",
        "2026-09-22T00:00:00",
        "2026-09-22T00:00:00-04:00",
        "2026-02-30T00:00:00Z",
        0,
        True,
    ],
)
def test_approval_requires_real_utc_iso_timestamp(timestamp: object) -> None:
    value = approval()
    value.update(status="approved", approved_by="Angela", approved_at_utc=timestamp)
    with pytest.raises(ValidationError):
        GoldApproval.model_validate_json(json.dumps(value))


def test_approval_state_requires_both_owner_fields_exactly_when_approved() -> None:
    value = approval()
    assert GoldApproval.model_validate_json(json.dumps(value)).status == "draft"
    value["status"] = "approved"
    with pytest.raises(ValidationError):
        GoldApproval.model_validate_json(json.dumps(value))
    value.update(approved_by="Angela", approved_at_utc="2020-01-01T00:00:00Z")
    assert GoldApproval.model_validate_json(json.dumps(value)).status == "approved"
    value["status"] = "draft"
    with pytest.raises(ValidationError):
        GoldApproval.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},
        {"baseline_phase": "pending"},
        {"schema_version": 1, "baseline_phase": "unknown"},
        {"schema_version": 1, "baseline_phase": "pending", "skip": True},
    ],
)
def test_activation_never_defaults_missing_or_unknown_phase(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Activation.model_validate_json(json.dumps(payload))
