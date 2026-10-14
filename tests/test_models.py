"""Synthetic JSON fixtures exercise the public boundaries without calculations."""

import json
from copy import deepcopy
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from rent_navigator.models import (
    ASK_REQUEST_ADAPTER,
    ERROR_HTTP_STATUSES,
    AskResponse,
    CheckResult,
    Citation,
    ErrorDetail,
    ErrorResponse,
    Extraction,
    ExtractRequest,
    ExtractResponse,
    GeneratedResult,
    LastIncrease,
    NoticeFacts,
    NoticeRequest,
    QuestionRequest,
    RentFacts,
    RentRequest,
    Scope,
    Statement,
    StrictModel,
    ToolResult,
    disclaimer_for,
    provider_tool_definitions,
)

SYNTHETIC_ATTEMPT_ID = "00000000-0000-4000-8000-000000000001"
SYNTHETIC_TRACE_ID = "00000000-0000-4000-8000-000000000002"
SYNTHETIC_SNAPSHOT_DATE = "2026-09-01"
JsonObject = dict[str, Any]


def notice_facts() -> JsonObject:
    return {
        "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
        "effective_on": "2026-09-01",
        "served_on": None,
        "service_method": "unknown",
    }


def rent_facts() -> JsonObject:
    return {
        **notice_facts(),
        "current_cents": 200000,
        "proposed_cents": None,
        "tenancy_start": "2024-02-29",
        "last_increase": {"state": "unknown", "date": None},
        "guideline_status": "controlled",
        "form": "N1",
    }


def extraction() -> JsonObject:
    return {"current_cents": None, "proposed_cents": None, "effective_on": None}


def check_result(check_id: str = "scope") -> JsonObject:
    return {
        "id": check_id,
        "status": "unknown",
        "reason": "missing_fact",
        "rule_ids": [],
    }


def tool_result(*, rent: bool = False) -> JsonObject:
    check_ids = ["scope", "supported_year", "period_start", "notice"]
    if rent:
        check_ids.extend(["spacing", "guideline", "form"])
    return {
        "tool": "rent_increase_check" if rent else "notice_deadline_check",
        "status": "cannot_determine",
        "checks": [check_result(check_id) for check_id in check_ids],
        "deemed_served_on": None,
        "notice_days": None,
        "earliest_notice_on": None,
        "latest_deemed_service_on": None,
        "latest_dispatch_on": None,
        "earliest_spacing_on": None,
        "guideline_percent": None,
        "cap_cents_exact": None,
        "rule_ids": [],
    }


def statement() -> JsonObject:
    return {"id": "s1", "text": "Synthetic statement.", "citation_ids": []}


def citation(chunk_id: str = "a" * 64) -> JsonObject:
    return {
        "id": chunk_id,
        "url": "https://example.invalid/synthetic-source",
        "heading": "Synthetic section",
        "snapshot_date": SYNTHETIC_SNAPSHOT_DATE,
    }


def response_metadata() -> JsonObject:
    return {
        "attempt_id": SYNTHETIC_ATTEMPT_ID,
        "trace_id": SYNTHETIC_TRACE_ID,
        "snapshot_date": SYNTHETIC_SNAPSHOT_DATE,
        "disclaimer": disclaimer_for(date.fromisoformat(SYNTHETIC_SNAPSHOT_DATE)),
    }


def ask_response() -> JsonObject:
    return {
        **response_metadata(),
        "status": "answered",
        "answer": "Synthetic statement.",
        "statements": [statement()],
        "tool_result": None,
        "citations": [],
    }


PUBLIC_FIXTURES: list[tuple[type[StrictModel], JsonObject]] = [
    (
        ExtractRequest,
        {"attempt_id": SYNTHETIC_ATTEMPT_ID, "letter": "Synthetic letter."},
    ),
    (Extraction, extraction()),
    (Scope, {"ordinary": "unknown", "period_start": "excluded"}),
    (NoticeFacts, notice_facts()),
    (LastIncrease, {"state": "none", "date": None}),
    (RentFacts, rent_facts()),
    (
        QuestionRequest,
        {
            "mode": "question",
            "attempt_id": SYNTHETIC_ATTEMPT_ID,
            "question": "Synthetic question?",
        },
    ),
    (
        NoticeRequest,
        {
            "mode": "notice",
            "attempt_id": SYNTHETIC_ATTEMPT_ID,
            "confirmed": True,
            "facts": notice_facts(),
        },
    ),
    (
        RentRequest,
        {
            "mode": "rent",
            "attempt_id": SYNTHETIC_ATTEMPT_ID,
            "confirmed": True,
            "facts": rent_facts(),
        },
    ),
    (CheckResult, check_result()),
    (ToolResult, tool_result()),
    (Statement, statement()),
    (
        GeneratedResult,
        {"kind": "answer", "refusal_reason": None, "statements": [statement()]},
    ),
    (Citation, citation()),
    (AskResponse, ask_response()),
    (ExtractResponse, {**response_metadata(), "extraction": extraction()}),
    (ErrorDetail, {"code": "invalid_request", "message": "Invalid request."}),
    (
        ErrorResponse,
        {
            **response_metadata(),
            "attempt_id": None,
            "error": {"code": "invalid_request", "message": "Invalid request."},
        },
    ),
]


@pytest.mark.parametrize(("model", "payload"), PUBLIC_FIXTURES)
def test_all_public_models_round_trip_json(model: type[StrictModel], payload: JsonObject) -> None:
    parsed = model.model_validate_json(json.dumps(payload))
    assert json.loads(parsed.model_dump_json()) == payload


@pytest.mark.parametrize(("model", "payload"), PUBLIC_FIXTURES)
def test_every_listed_field_is_required_even_when_nullable(
    model: type[StrictModel], payload: JsonObject
) -> None:
    for field in payload:
        incomplete = deepcopy(payload)
        del incomplete[field]
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(incomplete))


@pytest.mark.parametrize(("model", "payload"), PUBLIC_FIXTURES)
def test_all_public_models_reject_extra_json_fields(
    model: type[StrictModel], payload: JsonObject
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**payload, "unexpected": "payload"}))


@pytest.mark.parametrize("field", ["current_cents", "proposed_cents"])
@pytest.mark.parametrize("invalid", [True, False, "1", 0, -1, 1.0, "", "unknown"])
@pytest.mark.parametrize("model", [Extraction, RentFacts])
def test_cents_reject_coercion_and_nonpositive_values(
    field: str, invalid: object, model: type[StrictModel]
) -> None:
    payload = extraction() if model is Extraction else rent_facts()
    payload[field] = invalid
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("value", [None, 1, 200001])
def test_nullable_cents_accept_only_null_or_positive_integer(value: int | None) -> None:
    payload = {**extraction(), "current_cents": value, "proposed_cents": value}
    assert Extraction.model_validate_json(json.dumps(payload)).current_cents == value


@pytest.mark.parametrize(
    "invalid",
    [
        "2026-02-29",
        "2024-02-30",
        "2026-13-01",
        "2026-00-01",
        "0000-01-01",
        "2026-9-01",
        "20260901",
        "2026-W01-1",
        "2026-09-01T00:00:00",
        "2026-09-01\n",
        1788220800,
        True,
        "",
        "unknown",
        "none",
    ],
)
def test_date_json_requires_a_real_exact_gregorian_date(invalid: object) -> None:
    with pytest.raises(ValidationError):
        Extraction.model_validate_json(json.dumps({**extraction(), "effective_on": invalid}))


@pytest.mark.parametrize("valid", ["0001-01-01", "2024-02-29", "9999-12-31"])
def test_date_json_round_trip_is_canonical(valid: str) -> None:
    parsed = Extraction.model_validate_json(json.dumps({**extraction(), "effective_on": valid}))
    assert parsed.effective_on == date.fromisoformat(valid)
    assert parsed.model_dump(mode="json")["effective_on"] == valid


@pytest.mark.parametrize(
    "invalid",
    [
        "ABCDEFAB-1234-4321-8123-ABCDEFABCDEF",
        "00000000000040008000000000000001",
        "{00000000-0000-4000-8000-000000000001}",
        "urn:uuid:00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001\n",
        "not-a-uuid",
        None,
        1,
        True,
    ],
)
def test_uuid_wire_format_is_canonical(invalid: object) -> None:
    with pytest.raises(ValidationError):
        ExtractRequest.model_validate_json(
            json.dumps({"attempt_id": invalid, "letter": "Synthetic."})
        )


def test_uuid_is_native_in_python_and_canonical_in_json() -> None:
    payload = {"attempt_id": SYNTHETIC_ATTEMPT_ID, "letter": "Synthetic."}
    parsed = ExtractRequest.model_validate_json(json.dumps(payload))
    assert parsed.attempt_id == UUID(SYNTHETIC_ATTEMPT_ID)
    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("state", "value"), [("known", "2025-01-01"), ("none", None), ("unknown", None)]
)
def test_last_increase_valid_state_date_pair(state: str, value: str | None) -> None:
    parsed = LastIncrease.model_validate_json(json.dumps({"state": state, "date": value}))
    assert parsed.state == state


@pytest.mark.parametrize(
    ("state", "value"),
    [
        ("known", None),
        ("none", "2025-01-01"),
        ("unknown", "2025-01-01"),
        (None, None),
        ("None", None),
        ("null", None),
        ("none", "unknown"),
    ],
)
def test_last_increase_states_and_null_are_not_interchangeable(
    state: object, value: object
) -> None:
    with pytest.raises(ValidationError):
        LastIncrease.model_validate_json(json.dumps({"state": state, "date": value}))


def test_adverse_dates_remain_valid_input() -> None:
    payload = rent_facts()
    payload.update(
        served_on="2027-01-01",
        service_method="mail",
        tenancy_start="2027-02-01",
        last_increase={"state": "known", "date": "2027-03-01"},
    )
    parsed = RentFacts.model_validate_json(json.dumps(payload))
    assert parsed.model_dump(mode="json") == payload


def test_known_increase_before_known_tenancy_is_inconsistent() -> None:
    payload = rent_facts()
    payload["last_increase"] = {"state": "known", "date": "2024-02-28"}
    with pytest.raises(ValidationError, match="precedes"):
        RentFacts.model_validate_json(json.dumps(payload))
    payload["tenancy_start"] = None
    RentFacts.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("form", ["N1", "N2", "other", "unknown"])
def test_form_wire_values(form: str) -> None:
    payload = {**rent_facts(), "form": form}
    assert RentFacts.model_validate_json(json.dumps(payload)).form == form


@pytest.mark.parametrize("form", ["n1", "n2", "NONE", None, 1])
def test_form_rejects_case_changes_and_coercion(form: object) -> None:
    with pytest.raises(ValidationError):
        RentFacts.model_validate_json(json.dumps({**rent_facts(), "form": form}))


@pytest.mark.parametrize(
    ("model", "payload", "field", "limit"),
    [
        (ExtractRequest, {"attempt_id": SYNTHETIC_ATTEMPT_ID}, "letter", 4000),
        (
            QuestionRequest,
            {"attempt_id": SYNTHETIC_ATTEMPT_ID, "mode": "question"},
            "question",
            1500,
        ),
    ],
)
def test_text_limits_count_unicode_characters(
    model: type[StrictModel], payload: JsonObject, field: str, limit: int
) -> None:
    for valid in ["租", "租" * limit]:
        model.model_validate_json(json.dumps({**payload, field: valid}))
    for invalid in ["", "租" * (limit + 1), 1, True, None]:
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps({**payload, field: invalid}))


@pytest.mark.parametrize("mode", ["question", "notice", "rent"])
def test_ask_request_discriminated_json_boundary(mode: str) -> None:
    payload: JsonObject = {"mode": mode, "attempt_id": SYNTHETIC_ATTEMPT_ID}
    if mode == "question":
        payload["question"] = "Synthetic question?"
    else:
        payload.update(confirmed=True, facts=rent_facts() if mode == "rent" else notice_facts())
    assert ASK_REQUEST_ADAPTER.validate_json(json.dumps(payload)).mode == mode
    for extra in [
        {"letter": "Synthetic."},
        {"tool": "notice_deadline_check"},
        {"instructions": "Synthetic."},
    ]:
        with pytest.raises(ValidationError):
            ASK_REQUEST_ADAPTER.validate_json(json.dumps({**payload, **extra}))
    other_variant = {"facts": notice_facts()} if mode == "question" else {"question": "Synthetic?"}
    with pytest.raises(ValidationError):
        ASK_REQUEST_ADAPTER.validate_json(json.dumps({**payload, **other_variant}))


@pytest.mark.parametrize("confirmed", [False, 1, 0, "true", "True", None])
def test_confirmation_requires_json_true(confirmed: object) -> None:
    payload = {
        "mode": "notice",
        "attempt_id": SYNTHETIC_ATTEMPT_ID,
        "confirmed": confirmed,
        "facts": notice_facts(),
    }
    with pytest.raises(ValidationError):
        ASK_REQUEST_ADAPTER.validate_json(json.dumps(payload))


@pytest.mark.parametrize("mode", [None, "Notice", "extract", 1])
def test_unknown_request_discriminator_is_rejected(mode: object) -> None:
    with pytest.raises(ValidationError):
        ASK_REQUEST_ADAPTER.validate_json(
            json.dumps({"mode": mode, "attempt_id": SYNTHETIC_ATTEMPT_ID, "question": "Synthetic?"})
        )


def test_nested_extras_and_wrong_fact_variant_are_rejected() -> None:
    payload = notice_facts()
    payload["scope"]["extra"] = True
    with pytest.raises(ValidationError):
        NoticeFacts.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        ASK_REQUEST_ADAPTER.validate_json(
            json.dumps(
                {
                    "mode": "notice",
                    "attempt_id": SYNTHETIC_ATTEMPT_ID,
                    "confirmed": True,
                    "facts": rent_facts(),
                }
            )
        )


@pytest.mark.parametrize("rent", [False, True])
def test_tool_result_requires_exact_check_sequence(rent: bool) -> None:
    payload = tool_result(rent=rent)
    ToolResult.model_validate_json(json.dumps(payload))
    variants = [
        payload["checks"][:-1],
        payload["checks"][::-1],
        payload["checks"] + [payload["checks"][-1]],
    ]
    for checks in variants:
        with pytest.raises(ValidationError):
            ToolResult.model_validate_json(json.dumps({**payload, "checks": checks}))


def test_rule_ids_require_sorted_unique_exact_union() -> None:
    payload = tool_result()
    payload["checks"][0]["rule_ids"] = ["scope.ordinary"]
    payload["checks"][3]["rule_ids"] = ["notice.90_days", "notice.mail_5_days"]
    payload["rule_ids"] = ["notice.90_days", "notice.mail_5_days", "scope.ordinary"]
    ToolResult.model_validate_json(json.dumps(payload))
    for invalid in [[], payload["rule_ids"][::-1], payload["rule_ids"] * 2]:
        with pytest.raises(ValidationError):
            ToolResult.model_validate_json(json.dumps({**payload, "rule_ids": invalid}))
    for invalid in [
        ["scope.ordinary", "notice.90_days"],
        ["scope.ordinary"] * 2,
        ["invented.rule"],
    ]:
        with pytest.raises(ValidationError):
            CheckResult.model_validate_json(json.dumps({**check_result(), "rule_ids": invalid}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("earliest_spacing_on", "2026-09-01"),
        ("guideline_percent", "2.1"),
        ("cap_cents_exact", "204200"),
    ],
)
def test_notice_results_reject_rent_only_derived_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate_json(json.dumps({**tool_result(), field: value}))


@pytest.mark.parametrize("days", [-90, -1, 0, 90, None])
def test_notice_days_accept_signed_integer_or_null(days: int | None) -> None:
    result = ToolResult.model_validate_json(json.dumps({**tool_result(), "notice_days": days}))
    assert result.notice_days == days


@pytest.mark.parametrize("days", [True, False, "90", 90.0])
def test_notice_days_do_not_coerce(days: object) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate_json(json.dumps({**tool_result(), "notice_days": days}))


@pytest.mark.parametrize("cap", ["204200", "204201.021", "1.9", None])
def test_cap_decimal_strings_round_trip_exactly(cap: str | None) -> None:
    payload = {**tool_result(rent=True), "cap_cents_exact": cap}
    parsed = ToolResult.model_validate_json(json.dumps(payload))
    assert json.loads(parsed.model_dump_json()) == payload


@pytest.mark.parametrize(
    "cap",
    [
        204200,
        204200.1,
        True,
        "2e5",
        "204200.0",
        "204200.10",
        "01",
        "+1",
        "1.",
        ".5",
        " 1",
        "-0",
        "1\n",
    ],
)
def test_cap_decimal_strings_reject_noncanonical_values(cap: object) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate_json(
            json.dumps({**tool_result(rent=True), "cap_cents_exact": cap})
        )


@pytest.mark.parametrize("percent", [2.1, "2.10", "1.90", "3", True])
def test_guideline_percentage_is_one_of_the_exact_strings(percent: object) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate_json(
            json.dumps({**tool_result(rent=True), "guideline_percent": percent})
        )


@pytest.mark.parametrize(
    ("model", "payload", "field", "invalid"),
    [
        (CheckResult, check_result(), "id", "rent"),
        (CheckResult, check_result(), "status", "passed"),
        (CheckResult, check_result(), "reason", "other"),
        (ToolResult, tool_result(), "status", "legal"),
        (ToolResult, tool_result(), "tool", "notice_check"),
        (AskResponse, ask_response(), "status", "answer"),
        (
            ErrorDetail,
            {"code": "invalid_request", "message": "Invalid request."},
            "code",
            "internal_error",
        ),
    ],
)
def test_output_enums_are_exact(
    model: type[StrictModel], payload: JsonObject, field: str, invalid: str
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps({**payload, field: invalid}))


@pytest.mark.parametrize("reason", ["needs_confirmation", "out_of_scope", "insufficient_evidence"])
def test_application_refusal_has_no_statements(reason: str) -> None:
    GeneratedResult.model_validate_json(
        json.dumps({"kind": "refusal", "refusal_reason": reason, "statements": []})
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "answer", "refusal_reason": None, "statements": []},
        {"kind": "answer", "refusal_reason": "needs_confirmation", "statements": [statement()]},
        {"kind": "refusal", "refusal_reason": None, "statements": []},
        {"kind": "refusal", "refusal_reason": "out_of_scope", "statements": [statement()]},
    ],
)
def test_generated_answer_refusal_discriminator_invariants(payload: JsonObject) -> None:
    with pytest.raises(ValidationError):
        GeneratedResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "ids",
    [
        ["s2"],
        ["s1", "s1"],
        ["s1", "s3"],
        ["s2", "s1"],
        ["s1", "s2", "s3", "s4", "s6"],
        ["s1", "s2", "s3", "s4", "s5", "s5"],
        ["s1", "s2", "s3", "s4", "s5", "s6", "s7"],
    ],
)
def test_statement_sequence_is_fixed_for_generated_and_http_outputs(ids: list[str]) -> None:
    statements = [{**statement(), "id": statement_id} for statement_id in ids]
    with pytest.raises(ValidationError):
        GeneratedResult.model_validate_json(
            json.dumps({"kind": "answer", "refusal_reason": None, "statements": statements})
        )
    with pytest.raises(ValidationError):
        AskResponse.model_validate_json(json.dumps({**ask_response(), "statements": statements}))


@pytest.mark.parametrize("length", [1, 240, 241, 350, 599, 600])
def test_six_statements_preserve_allowed_unicode_text_and_citations(length: int) -> None:
    statements = [
        {
            **statement(),
            "id": f"s{index}",
            "text": "租" * length,
            "citation_ids": ["a" * 64],
        }
        for index in range(1, 7)
    ]
    generated = {"kind": "answer", "refusal_reason": None, "statements": statements}
    response = {
        **ask_response(),
        "answer": "\n".join(item["text"] for item in statements),
        "statements": statements,
        "citations": [citation()],
    }
    for model, payload in ((GeneratedResult, generated), (AskResponse, response)):
        parsed = model.model_validate_json(json.dumps(payload))
        assert json.loads(parsed.model_dump_json()) == payload


@pytest.mark.parametrize("text", ["", "租" * 601, None, True, 1, 1.5, [], {}])
def test_statement_text_rejects_size_violations_and_nonstring_wire_types(text: object) -> None:
    invalid = {**statement(), "text": text}
    for model, payload in (
        (Statement, invalid),
        (GeneratedResult, {"kind": "answer", "refusal_reason": None, "statements": [invalid]}),
        (AskResponse, {**ask_response(), "statements": [invalid]}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("status,statements", [("answered", []), ("refused", [statement()])])
def test_http_answer_and_refusal_statement_requirements_are_unchanged(
    status: str, statements: list[JsonObject]
) -> None:
    with pytest.raises(ValidationError):
        AskResponse.model_validate_json(
            json.dumps({**ask_response(), "status": status, "statements": statements})
        )


def test_baseline_generated_statements_may_have_no_citations() -> None:
    parsed = GeneratedResult.model_validate_json(
        json.dumps({"kind": "answer", "refusal_reason": None, "statements": [statement()]})
    )
    assert parsed.statements[0].citation_ids == []


def test_http_citations_are_unique_and_sorted_by_chunk_id() -> None:
    payload = {**ask_response(), "citations": [citation("a" * 64), citation("b" * 64)]}
    AskResponse.model_validate_json(json.dumps(payload))
    for citations in [payload["citations"][::-1], [citation(), citation()]]:
        with pytest.raises(ValidationError):
            AskResponse.model_validate_json(json.dumps({**payload, "citations": citations}))


def test_fact_refusal_can_preserve_executed_tool_result() -> None:
    payload = {
        **ask_response(),
        "status": "refused",
        "answer": "Synthetic refusal.",
        "statements": [],
        "tool_result": tool_result(),
    }
    parsed = AskResponse.model_validate_json(json.dumps(payload))
    assert parsed.tool_result is not None
    assert parsed.status == "refused"


@pytest.mark.parametrize(
    "model,payload",
    [
        (AskResponse, ask_response()),
        (ExtractResponse, {**response_metadata(), "extraction": extraction()}),
        (
            ErrorResponse,
            {
                **response_metadata(),
                "error": {"code": "provider_error", "message": "Provider unavailable."},
            },
        ),
    ],
)
def test_response_disclaimer_is_fixed_and_matches_snapshot(
    model: type[StrictModel], payload: JsonObject
) -> None:
    for change in [{"disclaimer": "Synthetic replacement."}, {"snapshot_date": "2026-09-02"}]:
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps({**payload, **change}))


def test_error_codes_have_exact_http_status_mapping() -> None:
    assert ERROR_HTTP_STATUSES == {
        "invalid_request": 422,
        "body_too_large": 413,
        "rate_limited": 429,
        "busy": 429,
        "budget_exhausted": 429,
        "stale_corpus": 503,
        "provider_error": 502,
        "invalid_generated_output": 502,
        "tool_protocol_error": 502,
        "deadline_exceeded": 504,
    }


def test_provider_definitions_use_the_exact_public_fact_schemas() -> None:
    definitions = provider_tool_definitions()
    assert [definition["name"] for definition in definitions] == [
        "notice_deadline_check",
        "rent_increase_check",
    ]
    fact_models: list[type[StrictModel]] = [NoticeFacts, RentFacts]
    for definition, model in zip(definitions, fact_models, strict=True):
        schema: JsonObject = json.loads(json.dumps(definition["input_schema"]))
        assert schema == model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for nested in schema["$defs"].values():
            assert nested["additionalProperties"] is False
            assert set(nested["required"]) == set(nested["properties"])
    assert json.loads(json.dumps(definitions)) == definitions


def test_request_union_publishes_a_mode_discriminator() -> None:
    schema = ASK_REQUEST_ADAPTER.json_schema()
    assert schema["discriminator"]["propertyName"] == "mode"
    assert set(schema["discriminator"]["mapping"]) == {"question", "notice", "rent"}


def test_native_date_and_uuid_values_are_supported_for_server_construction() -> None:
    request = ExtractRequest(attempt_id=UUID(SYNTHETIC_ATTEMPT_ID), letter="Synthetic.")
    parsed = Extraction(current_cents=None, proposed_cents=None, effective_on=date(2024, 2, 29))
    assert request.model_dump(mode="json")["attempt_id"] == SYNTHETIC_ATTEMPT_ID
    assert parsed.model_dump(mode="json")["effective_on"] == "2024-02-29"


def test_json_schema_requires_nullable_values() -> None:
    schema = TypeAdapter(Extraction).json_schema()
    assert set(schema["required"]) == {"current_cents", "proposed_cents", "effective_on"}
    assert {option["type"] for option in schema["properties"]["current_cents"]["anyOf"]} == {
        "integer",
        "null",
    }
