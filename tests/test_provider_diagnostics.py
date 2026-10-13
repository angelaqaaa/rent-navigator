"""Synthetic failure classification and privacy checks; no external transport."""

import asyncio
import json
from collections import UserDict
from decimal import Decimal
from typing import Any, cast, get_args

import httpx2
import pytest
from anthropic import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OverloadedError,
    RateLimitError,
)
from pydantic import ValidationError
from test_eval_judge import isolated_count_replay as isolated_count_replay
from test_provider import RecordingMessages, assert_accounting, perform, reply

from rent_navigator.models import ErrorCode
from rent_navigator.provider import (
    Deadline,
    FailureDiagnostic,
    FailureDiagnosticV2,
    FailureEnvelope,
    ProviderFailure,
    SpendLedger,
    _error_code,
    classify_failure,
    failure_envelope,
)

SENTINEL = "synthetic-private-failure-sentinel"


def sdk_error(kind: str) -> BaseException:
    request = httpx2.Request(
        "POST", f"https://synthetic.invalid/{SENTINEL}", headers={"secret": SENTINEL}
    )
    if kind == "timeout":
        return APITimeoutError(request)
    if kind == "connection":
        return APIConnectionError(message=SENTINEL, request=request)
    if kind == "response_validation":
        return APIResponseValidationError(
            httpx2.Response(200, request=request), body={"secret": SENTINEL}, message=SENTINEL
        )
    status = int(kind)
    classes: dict[int, type[APIStatusError]] = {
        400: BadRequestError,
        429: RateLimitError,
        500: InternalServerError,
        529: OverloadedError,
    }
    return classes[status](
        SENTINEL,
        response=httpx2.Response(
            status,
            request=request,
            headers={"request-id": SENTINEL, "anthropic-workspace-id": SENTINEL},
            content=SENTINEL,
        ),
        body={"error": {"message": SENTINEL, "type": SENTINEL}},
    )


@pytest.mark.parametrize(
    "kind,category,status,code",
    [
        ("400", "http_error", 400, "provider_error"),
        ("429", "http_error", 429, "provider_error"),
        ("500", "http_error", 500, "provider_error"),
        ("529", "http_error", 529, "provider_error"),
        ("timeout", "sdk_timeout", None, "deadline_exceeded"),
        ("connection", "sdk_connection", None, "provider_error"),
        ("response_validation", "sdk_response_validation", 200, "provider_error"),
    ],
)
@pytest.mark.parametrize("operation", ["count", "generation"])
def test_actual_adapter_preserves_sdk_diagnostic_and_conservative_accounting(
    kind: str, category: str, status: int | None, code: str, operation: str
) -> None:
    original = sdk_error(kind)
    fake = RecordingMessages(
        count_error=original if operation == "count" else None,
        create_error=original if operation == "generation" else None,
    )
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    failure = outcome.failure
    assert failure is not None and failure.code == code
    assert failure.diagnostic.model_dump(mode="json") == {
        "schema_version": 2,
        "category": category,
        "http_status": status,
        "rejection_reason": "unclassified" if category == "http_error" and status == 400 else None,
    }
    assert failure.__context__ is None and failure.__cause__ is None
    assert set(vars(failure)) == {"code", "message", "diagnostic"}
    assert (
        SENTINEL not in str(failure) + repr(failure) + failure_envelope(failure).model_dump_json()
    )
    assert SENTINEL not in outcome.log
    assert len(fake.counts) == 1 and len(fake.creates) == int(operation == "generation")
    generation = operation == "generation"
    assert_accounting(
        outcome,
        actual=None if generation else "0",
        reserved="0.027" if generation else "0",
        complete=not generation,
        generation_count=int(generation),
    )
    assert ledger.committed_usd == (Decimal("0.027") if generation else Decimal(0))
    assert ledger.stopped is generation


def test_sdk_timeout_subclass_precedes_connection_and_repeated_wrapping_is_lossless() -> None:
    original = sdk_error("timeout")
    assert isinstance(original, APIConnectionError)
    first = ProviderFailure.from_exception(original)
    second = ProviderFailure.from_exception(first)
    third = ProviderFailure.from_exception(second)
    assert first is not second and second is not third
    assert first.diagnostic.category == "sdk_timeout"
    assert failure_envelope(first) == failure_envelope(second) == failure_envelope(third)
    outcome = asyncio.run(perform(RecordingMessages(create_error=third), SpendLedger(Decimal("1"))))
    assert outcome.failure is not None
    assert failure_envelope(outcome.failure) == failure_envelope(first)
    assert outcome.failure.__context__ is None and outcome.failure.__cause__ is None


def test_normalization_drops_preexisting_raw_exception_context_without_losing_diagnostic() -> None:
    unsafe_chain = None
    try:
        try:
            raise sdk_error("429")
        except APIStatusError as error:
            raise ProviderFailure.from_exception(error) from None
    except ProviderFailure as wrapped:
        assert isinstance(wrapped.__context__, APIStatusError)
        unsafe_chain = wrapped
    assert unsafe_chain is not None
    outcome = asyncio.run(
        perform(RecordingMessages(create_error=unsafe_chain), SpendLedger(Decimal("1")))
    )
    assert outcome.failure is not None
    assert outcome.failure.diagnostic.category == "http_error"
    assert outcome.failure.diagnostic.http_status == 429
    assert outcome.failure.__context__ is None and outcome.failure.__cause__ is None


class HostileUnknown(Exception):
    def __str__(self) -> str:
        raise AssertionError("Exception text must not be inspected")

    def __repr__(self) -> str:
        raise AssertionError("Exception representation must not be inspected")


class HostileStatus(APIStatusError):
    def __str__(self) -> str:
        raise AssertionError("SDK exception text must not be inspected")

    def __repr__(self) -> str:
        raise AssertionError("SDK exception representation must not be inspected")

    def __getattribute__(self, name: str) -> Any:
        if name in {"request", "response", "body", "headers", "message", "request_id", "type"}:
            raise AssertionError("Provider payload must not be inspected")
        return super().__getattribute__(name)


@pytest.mark.parametrize("typed", [False, True])
def test_hostile_exception_text_and_payload_are_never_inspected(typed: bool) -> None:
    error: BaseException = HostileUnknown(SENTINEL)
    if typed:
        error = HostileStatus(
            SENTINEL,
            response=httpx2.Response(
                400, request=httpx2.Request("POST", "https://synthetic.invalid")
            ),
            body={"secret": SENTINEL},
        )
    outcome = asyncio.run(perform(RecordingMessages(create_error=error), SpendLedger(Decimal("1"))))
    assert outcome.failure is not None
    envelope = failure_envelope(outcome.failure)
    assert envelope.diagnostic.category == ("http_error" if typed else "unknown")
    assert envelope.diagnostic.http_status == (400 if typed else None)
    assert SENTINEL not in envelope.model_dump_json() + str(outcome.failure) + outcome.log
    assert outcome.failure.__context__ is None and outcome.failure.__cause__ is None


@pytest.mark.parametrize("kind", ["400", "response_validation"])
@pytest.mark.parametrize("status", [True, False, "400", 400.0, 99, 600, None, HostileUnknown()])
def test_invalid_sdk_status_is_not_coerced_or_formatted(kind: str, status: object) -> None:
    error = sdk_error(kind)
    cast(Any, error).status_code = status
    diagnostic = classify_failure(error)
    assert diagnostic.category == ("unknown" if kind == "400" else "sdk_response_validation")
    assert diagnostic.http_status is None


@pytest.mark.parametrize("kind,status", [("400", 399), ("response_validation", 100), ("400", 599)])
def test_sdk_status_category_specific_boundaries(kind: str, status: int) -> None:
    error = sdk_error(kind)
    cast(Any, error).status_code = status
    diagnostic = classify_failure(error)
    assert diagnostic.http_status == (None if status == 399 else status)
    assert diagnostic.category == (
        "unknown" if status == 399 else "sdk_response_validation" if status == 100 else "http_error"
    )


@pytest.mark.parametrize(
    "error,category,code",
    [
        (TimeoutError(SENTINEL), "local_timeout", "deadline_exceeded"),
        (asyncio.CancelledError(SENTINEL), "cancelled", "deadline_exceeded"),
        (ProviderFailure("budget_exhausted"), "local_guard", "budget_exhausted"),
        (ProviderFailure("deadline_exceeded"), "local_timeout", "deadline_exceeded"),
        (RuntimeError(SENTINEL), "unknown", "provider_error"),
    ],
)
def test_local_failures_preserve_safe_code_and_never_retain_the_original(
    error: BaseException, category: str, code: str
) -> None:
    failure = ProviderFailure.from_exception(error)
    assert failure.diagnostic.category == category and failure.diagnostic.http_status is None
    assert failure.code == code == _error_code(error)
    assert failure.__context__ is None and failure.__cause__ is None
    assert set(vars(failure)) == {"code", "message", "diagnostic"}
    assert SENTINEL not in failure_envelope(error).model_dump_json() + str(failure)


@pytest.mark.parametrize("known", [False, True])
def test_local_response_validation_keeps_existing_known_usage_and_unknown_hold(known: bool) -> None:
    response = reply(stop="max_tokens") if known else reply(model=SENTINEL)
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(RecordingMessages(response), ledger))
    assert outcome.failure is not None
    assert outcome.failure.code == ("invalid_generated_output" if known else "provider_error")
    assert outcome.failure.diagnostic.category == "local_guard"
    assert outcome.failure.__context__ is None and outcome.failure.__cause__ is None
    assert_accounting(outcome, actual="0.0015" if known else None, reserved="0.027", complete=known)
    assert ledger.committed_usd == Decimal("0.0015" if known else "0.027")


def test_actual_deadline_normalization_retains_no_timeout_exception_chain() -> None:
    async def scenario() -> None:
        with pytest.raises(ProviderFailure) as caught:
            async with Deadline.start().limit():
                raise TimeoutError(SENTINEL)
        assert caught.value.diagnostic.category == "local_timeout"
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert SENTINEL not in str(caught.value)

    asyncio.run(scenario())


def diagnostic_wire(**updates: Any) -> dict[str, Any]:
    return {"schema_version": 1, "category": "http_error", "http_status": 400, **updates}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", False),
        ("schema_version", "1"),
        ("schema_version", 1.0),
        ("schema_version", 2),
        ("schema_version", None),
        ("category", "provider_error"),
        ("category", None),
        ("http_status", True),
        ("http_status", False),
        ("http_status", "400"),
        ("http_status", 400.0),
        ("http_status", None),
        ("http_status", 399),
        ("http_status", 600),
        ("body", SENTINEL),
    ],
)
def test_diagnostic_json_rejects_unsafe_or_coerced_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        FailureDiagnostic.model_validate_json(json.dumps(diagnostic_wire(**{field: value})))


@pytest.mark.parametrize("field", ["schema_version", "category", "http_status"])
def test_diagnostic_json_requires_nullable_fields(field: str) -> None:
    wire = diagnostic_wire()
    del wire[field]
    with pytest.raises(ValidationError):
        FailureDiagnostic.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    "category",
    ["sdk_timeout", "sdk_connection", "local_timeout", "cancelled", "local_guard", "unknown"],
)
def test_non_http_diagnostics_require_explicit_null_status(category: str) -> None:
    with pytest.raises(ValidationError):
        FailureDiagnostic.model_validate_json(json.dumps(diagnostic_wire(category=category)))
    valid = diagnostic_wire(category=category, http_status=None)
    assert (
        json.loads(FailureDiagnostic.model_validate_json(json.dumps(valid)).model_dump_json())
        == valid
    )


@pytest.mark.parametrize("status", [None, 100, 200, 599])
def test_response_validation_status_json_accepts_only_its_documented_range(
    status: int | None,
) -> None:
    wire = diagnostic_wire(category="sdk_response_validation", http_status=status)
    assert FailureDiagnostic.model_validate_json(json.dumps(wire)).http_status == status


@pytest.mark.parametrize("code", get_args(ErrorCode))
def test_locally_constructed_failures_use_exact_code_category_mapping(code: ErrorCode) -> None:
    failure = ProviderFailure(code)
    envelope = failure_envelope(failure)
    assert envelope.code == code
    assert envelope.diagnostic.category == (
        "local_timeout" if code == "deadline_exceeded" else "local_guard"
    )
    assert isinstance(envelope.diagnostic, FailureDiagnosticV2)
    assert envelope.diagnostic.rejection_reason is None
    assert FailureEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "code,category,status",
    [
        ("provider_error", "sdk_timeout", None),
        ("provider_error", "local_timeout", None),
        ("provider_error", "cancelled", None),
        ("deadline_exceeded", "sdk_connection", None),
        ("deadline_exceeded", "http_error", 429),
        ("deadline_exceeded", "sdk_response_validation", 200),
        ("deadline_exceeded", "unknown", None),
        ("deadline_exceeded", "local_guard", None),
        ("rate_limited", "http_error", 429),
        ("invalid_generated_output", "sdk_response_validation", 200),
    ],
)
def test_failure_envelope_rejects_code_category_mismatch(
    code: str, category: str, status: int | None, version: int
) -> None:
    diagnostic = diagnostic_wire(category=category, http_status=status)
    if version == 2:
        diagnostic = diagnostic_v2_wire(
            category=category, http_status=status, rejection_reason=None
        )
    wire = {"code": code, "diagnostic": diagnostic}
    with pytest.raises(ValidationError):
        FailureEnvelope.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize("change", ["missing_code", "missing_diagnostic", "extra", "invalid_code"])
def test_failure_envelope_rejects_missing_extra_or_unknown_code(change: str) -> None:
    wire = {"code": "provider_error", "diagnostic": diagnostic_wire()}
    if change.startswith("missing_"):
        del wire[change.removeprefix("missing_")]
    elif change == "extra":
        wire["message"] = SENTINEL
    else:
        wire["code"] = "unknown"
    with pytest.raises(ValidationError):
        FailureEnvelope.model_validate_json(json.dumps(wire))


def test_diagnostic_and_envelope_are_immutable_and_revalidate_constructed_values() -> None:
    diagnostic = FailureDiagnostic.model_validate_json(json.dumps(diagnostic_wire()))
    envelope = FailureEnvelope(code="provider_error", diagnostic=diagnostic)
    with pytest.raises(ValidationError, match="frozen"):
        cast(Any, diagnostic).http_status = 500
    with pytest.raises(ValidationError, match="frozen"):
        cast(Any, envelope).code = "deadline_exceeded"
    with pytest.raises(ValidationError):
        ProviderFailure("deadline_exceeded", diagnostic=diagnostic)
    with pytest.raises(ValidationError):
        ProviderFailure(
            "provider_error", diagnostic=diagnostic.model_copy(update={"http_status": True})
        )


def diagnostic_v2_wire(**updates: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "category": "http_error",
        "http_status": 400,
        "rejection_reason": "unclassified",
        **updates,
    }


@pytest.mark.parametrize("version", [1, 2])
def test_versioned_envelopes_round_trip_without_migration(version: int) -> None:
    diagnostic = diagnostic_wire() if version == 1 else diagnostic_v2_wire()
    wire = {"code": "provider_error", "diagnostic": diagnostic}
    envelope = FailureEnvelope.model_validate_json(json.dumps(wire))
    assert json.loads(envelope.model_dump_json()) == wire
    assert set(envelope.diagnostic.model_dump()) == set(diagnostic)
    assert type(envelope.diagnostic) is (FailureDiagnostic if version == 1 else FailureDiagnosticV2)
    wrapped = ProviderFailure("provider_error", diagnostic=envelope.diagnostic)
    for _ in range(3):
        wrapped = ProviderFailure.from_exception(wrapped)
        assert failure_envelope(wrapped).model_dump(mode="json") == wire
        assert wrapped.__context__ is None and wrapped.__cause__ is None


@pytest.mark.parametrize("field", ["schema_version", "category", "http_status", "rejection_reason"])
def test_v2_json_requires_all_four_fields_even_when_nullable(field: str) -> None:
    wire = diagnostic_v2_wire()
    del wire[field]
    with pytest.raises(ValidationError):
        FailureDiagnosticV2.model_validate_json(json.dumps(wire))
    with pytest.raises(ValidationError):
        FailureEnvelope.model_validate_json(
            json.dumps({"code": "provider_error", "diagnostic": wire})
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", False),
        ("schema_version", "2"),
        ("schema_version", 2.0),
        ("schema_version", 1),
        ("schema_version", 3),
        ("schema_version", None),
        ("category", "provider_error"),
        ("category", None),
        ("http_status", True),
        ("http_status", "400"),
        ("http_status", 400.0),
        ("http_status", None),
        ("http_status", 399),
        ("http_status", 600),
        ("rejection_reason", None),
        ("rejection_reason", "unknown"),
        ("rejection_reason", SENTINEL),
        ("rejection_reason", True),
        ("rejection_reason", 0),
        ("body", SENTINEL),
    ],
)
def test_v2_json_rejects_coercions_unknown_values_and_payload_fields(
    field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        FailureDiagnosticV2.model_validate_json(json.dumps(diagnostic_v2_wire(**{field: value})))


@pytest.mark.parametrize(
    "category,status",
    [
        ("http_error", 429),
        ("sdk_response_validation", 400),
        ("sdk_timeout", None),
        ("sdk_connection", None),
        ("local_timeout", None),
        ("cancelled", None),
        ("local_guard", None),
        ("unknown", None),
    ],
)
@pytest.mark.parametrize("reason", ["schema_complexity", "unclassified", None])
def test_v2_rejection_reason_is_null_outside_http_error_400(
    category: str, status: int | None, reason: str | None
) -> None:
    wire = diagnostic_v2_wire(category=category, http_status=status, rejection_reason=reason)
    if reason is not None:
        with pytest.raises(ValidationError):
            FailureDiagnosticV2.model_validate_json(json.dumps(wire))
    else:
        assert (
            FailureDiagnosticV2.model_validate_json(json.dumps(wire)).model_dump(mode="json")
            == wire
        )


@pytest.mark.parametrize("reason", ["schema_complexity", "unclassified"])
def test_v2_http_error_400_accepts_only_its_two_nonnull_reasons(reason: str) -> None:
    wire = diagnostic_v2_wire(rejection_reason=reason)
    diagnostic = FailureDiagnosticV2.model_validate_json(json.dumps(wire))
    assert diagnostic.model_dump(mode="json") == wire


@pytest.mark.parametrize("version", [True, False, "1", "2", 1.0, 2.0, 0, 3, None])
def test_envelope_discriminator_never_coerces_or_defaults_version(version: object) -> None:
    wire = {"code": "provider_error", "diagnostic": diagnostic_v2_wire(schema_version=version)}
    with pytest.raises(ValidationError):
        FailureEnvelope.model_validate_json(json.dumps(wire))


def test_envelope_union_is_explicit_in_schema_and_v1_fields_stay_exact() -> None:
    assert set(FailureDiagnostic.model_fields) == {"schema_version", "category", "http_status"}
    assert set(FailureDiagnosticV2.model_fields) == {
        "schema_version",
        "category",
        "http_status",
        "rejection_reason",
    }
    schema = FailureEnvelope.model_json_schema()["properties"]["diagnostic"]
    assert len(schema["oneOf"]) == 2
    assert {entry["$ref"] for entry in schema["oneOf"]} == {
        "#/$defs/FailureDiagnostic",
        "#/$defs/FailureDiagnosticV2",
    }
    definitions = FailureEnvelope.model_json_schema()["$defs"]
    assert definitions["FailureDiagnostic"]["properties"]["schema_version"]["const"] == 1
    assert definitions["FailureDiagnosticV2"]["properties"]["schema_version"]["const"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("http_status", True),
        ("rejection_reason", None),
        ("category", "sdk_connection"),
    ],
)
def test_v2_is_frozen_and_revalidates_copied_and_constructed_values(
    field: str, value: object
) -> None:
    diagnostic = FailureDiagnosticV2.model_validate_json(json.dumps(diagnostic_v2_wire()))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(diagnostic, field, value)
    corrupted = diagnostic.model_copy(update={field: value})
    with pytest.raises(ValidationError):
        ProviderFailure("provider_error", diagnostic=corrupted)
    constructed = FailureDiagnosticV2.model_construct(**diagnostic_v2_wire(**{field: value}))
    with pytest.raises(ValidationError):
        ProviderFailure("provider_error", diagnostic=constructed)


@pytest.mark.parametrize("version", [1, 2])
def test_rewrapping_revalidates_a_mutated_provider_failure(version: int) -> None:
    diagnostic = (
        FailureDiagnostic.model_validate(diagnostic_wire())
        if version == 1
        else FailureDiagnosticV2.model_validate(diagnostic_v2_wire())
    )
    failure = ProviderFailure("provider_error", diagnostic=diagnostic)
    failure.diagnostic = diagnostic.model_copy(update={"http_status": True})
    with pytest.raises(ValidationError):
        ProviderFailure.from_exception(failure)


@pytest.mark.parametrize(
    "category",
    ["sdk_timeout", "sdk_connection", "local_timeout", "cancelled", "local_guard", "unknown"],
)
def test_v2_non_http_category_cannot_claim_a_status(category: str) -> None:
    with pytest.raises(ValidationError):
        FailureDiagnosticV2.model_validate_json(
            json.dumps(diagnostic_v2_wire(category=category, rejection_reason=None))
        )


@pytest.mark.parametrize("status", [None, 100, 200, 400, 599])
def test_v2_response_validation_keeps_its_original_status_range(status: int | None) -> None:
    wire = diagnostic_v2_wire(
        category="sdk_response_validation", http_status=status, rejection_reason=None
    )
    assert FailureDiagnosticV2.model_validate_json(json.dumps(wire)).model_dump(mode="json") == wire


def grammar_body(*, nested: bool) -> dict[str, Any]:
    error = {"type": "invalid_request_error", "message": "Schema is too complex for compilation."}
    return {"error": error} if nested else error


def status_error_with_body(body: object, *, status: int = 400) -> BaseException:
    error = sdk_error("400")
    cast(Any, error).status_code = status
    cast(Any, error).body = body
    return error


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize(
    "sentence",
    [
        "Schema is too complex for compilation.",
        "The compiled grammar is too large, which would cause performance issues. "
        "Simplify your tool schemas or reduce the number of strict tools.",
    ],
)
def test_typed_400_exact_structured_sentence_classifies_schema_complexity(
    nested: bool, sentence: str
) -> None:
    detail = {"type": "invalid_request_error", "message": sentence}
    error = status_error_with_body({"error": detail} if nested else detail)
    classified = classify_failure(error)
    assert classified.model_dump(mode="json") == diagnostic_v2_wire(
        rejection_reason="schema_complexity"
    )
    outcome = asyncio.run(perform(RecordingMessages(create_error=error), SpendLedger(Decimal("1"))))
    assert outcome.failure is not None
    assert outcome.failure.diagnostic == classified
    assert SENTINEL not in str(outcome.failure) + repr(outcome.failure) + outcome.log
    assert "Schema is too complex" not in failure_envelope(outcome.failure).model_dump_json()
    assert outcome.failure.__context__ is None and outcome.failure.__cause__ is None
    assert_accounting(outcome, actual=None, reserved="0.027", complete=False)


@pytest.mark.parametrize(
    "message,error_type",
    [
        ("A different schema rejection sentence.", "invalid_request_error"),
        (None, "invalid_request_error"),
        (123, "invalid_request_error"),
        (
            "The compiled grammar is too large, which would cause performance issues. "
            "Simplify your tool schemas or reduce the number of strict tools.",
            "api_error",
        ),
    ],
)
def test_second_sentence_does_not_broaden_type_or_message_matching(
    message: object, error_type: str
) -> None:
    failure = classify_failure(
        status_error_with_body({"error": {"type": error_type, "message": message}})
    )
    assert failure.model_dump(mode="json") == diagnostic_v2_wire(rejection_reason="unclassified")


class HostileDictionary(dict[str, Any]):
    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Dictionary subclasses must not be inspected")

    def __getitem__(self, key: str) -> Any:
        raise AssertionError("Dictionary subclasses must not be inspected")

    def __contains__(self, key: object) -> bool:
        raise AssertionError("Dictionary subclasses must not be inspected")


class StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        SENTINEL,
        UserDict(
            {"type": "invalid_request_error", "message": "Schema is too complex for compilation."}
        ),
        {},
        {"error": None, **grammar_body(nested=False)},
        {"error": grammar_body(nested=False), "type": "invalid_request_error"},
        {"error": {"error": grammar_body(nested=False)}},
        {"error": {}, **grammar_body(nested=False)},
        {"error": [grammar_body(nested=False)]},
        {"error": {"type": "invalid_request_error"}},
        {"error": {"message": "Schema is too complex for compilation."}},
        {"error": {"type": True, "message": "Schema is too complex for compilation."}},
        {"error": {"type": "invalid_request_error", "message": None}},
        {
            "error": {
                "type": StringSubclass("invalid_request_error"),
                "message": "Schema is too complex for compilation.",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": StringSubclass("Schema is too complex for compilation."),
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": "schema is too complex for compilation.",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Schema is too complex for compilation",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": " Schema is too complex for compilation.",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Schema is too complex for compilation.\n",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": SENTINEL + "Schema is too complex for compilation.",
            }
        },
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Schema is too complex for compilation." + SENTINEL * 10000,
            }
        },
        {
            "error": {
                "type": "validation_error",
                "message": "Schema is too complex for compilation.",
            }
        },
        HostileDictionary(grammar_body(nested=False)),
        {"error": HostileDictionary(grammar_body(nested=False))},
    ],
    ids=[
        "null",
        "list",
        "string",
        "custom-mapping",
        "empty",
        "nested-null-root-valid",
        "ambiguous",
        "recursive",
        "nested-empty-root-valid",
        "nested-list",
        "missing-message",
        "missing-type",
        "bool-type",
        "null-message",
        "type-subclass",
        "message-subclass",
        "case",
        "punctuation",
        "leading-space",
        "trailing-newline",
        "sentinel-prefix",
        "long-sentinel-suffix",
        "wrong-type",
        "root-subclass",
        "nested-subclass",
    ],
)
def test_typed_400_malformed_ambiguous_and_near_matches_are_unclassified(body: object) -> None:
    failure = ProviderFailure.from_exception(status_error_with_body(body))
    assert failure.diagnostic.model_dump(mode="json") == diagnostic_v2_wire()
    assert SENTINEL not in failure_envelope(failure).model_dump_json() + str(failure) + repr(
        failure
    )
    assert set(vars(failure)) == {"code", "message", "diagnostic"}
    assert failure.__context__ is None and failure.__cause__ is None


class ObservedBodyStatus(HostileStatus):
    def __init__(self, status: int, failure: BaseException | None = None) -> None:
        super().__init__(
            SENTINEL,
            response=httpx2.Response(
                status, request=httpx2.Request("POST", "https://synthetic.invalid")
            ),
            body=grammar_body(nested=True),
        )
        self.body_reads = 0
        self.body_failure = failure

    def __getattribute__(self, name: str) -> Any:
        if name == "body":
            self.body_reads += 1
            if self.body_failure is not None:
                raise self.body_failure
            return object.__getattribute__(self, name)
        return super().__getattribute__(name)


@pytest.mark.parametrize("status", [100, 200, 399, 401, 429, 500, 599])
def test_other_http_statuses_never_observe_body(status: int) -> None:
    error = ObservedBodyStatus(status)
    diagnostic = classify_failure(error)
    assert diagnostic.schema_version == 2
    assert isinstance(diagnostic, FailureDiagnosticV2) and diagnostic.rejection_reason is None
    assert error.body_reads == 0


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(SENTINEL),
        asyncio.CancelledError(SENTINEL),
        TimeoutError(SENTINEL),
        SystemExit(SENTINEL),
    ],
)
def test_hostile_typed_400_body_observed_once_and_safely_unclassified(
    failure: BaseException,
) -> None:
    error = ObservedBodyStatus(400, failure)
    assert classify_failure(error).model_dump(mode="json") == diagnostic_v2_wire()
    assert error.body_reads == 1


def test_typed_400_body_is_read_only_once_by_pure_classifier() -> None:
    error = ObservedBodyStatus(400)
    assert classify_failure(error).model_dump(mode="json") == diagnostic_v2_wire(
        rejection_reason="schema_complexity"
    )
    assert error.body_reads == 1


class NoBodyResponseValidation(APIResponseValidationError):
    body_reads = 0

    def __getattribute__(self, name: str) -> Any:
        if name == "body":
            self.body_reads += 1
            raise AssertionError("Response validation never inspects body")
        if name in {"request", "response", "headers", "message", "request_id"}:
            raise AssertionError("Response validation never inspects provider payload")
        return super().__getattribute__(name)


def test_response_validation_400_is_not_an_http_rejection_or_body_observation() -> None:
    error = NoBodyResponseValidation(
        httpx2.Response(400, request=httpx2.Request("POST", "https://synthetic.invalid")),
        body=grammar_body(nested=True),
        message=SENTINEL,
    )
    diagnostic = classify_failure(error)
    assert diagnostic.model_dump(mode="json") == diagnostic_v2_wire(
        category="sdk_response_validation", rejection_reason=None
    )
    assert error.body_reads == 0


class PayloadObservationGuard:
    def __getattribute__(self, name: str) -> Any:
        if name in {"body", "request", "response", "headers", "message", "request_id"}:
            fields = object.__getattribute__(self, "__dict__")
            if "observed_payloads" in fields:
                fields["observed_payloads"].append(name)
                raise AssertionError("This category cannot observe provider payload")
        return super().__getattribute__(name)

    def __str__(self) -> str:
        raise AssertionError("No exception text may be inspected")

    def __repr__(self) -> str:
        raise AssertionError("No exception representation may be inspected")


class GuardedConnection(PayloadObservationGuard, APIConnectionError):
    pass


class GuardedTimeout(PayloadObservationGuard, APITimeoutError):
    pass


class GuardedLocalTimeout(PayloadObservationGuard, TimeoutError):
    pass


class GuardedCancellation(PayloadObservationGuard, asyncio.CancelledError):
    pass


class GuardedUnknown(PayloadObservationGuard, Exception):
    pass


@pytest.mark.parametrize(
    "kind,category",
    [
        ("connection", "sdk_connection"),
        ("timeout", "sdk_timeout"),
        ("local_timeout", "local_timeout"),
        ("cancelled", "cancelled"),
        ("unknown", "unknown"),
    ],
)
def test_non_http_categories_never_inspect_exception_payload_or_text(
    kind: str, category: str
) -> None:
    request = httpx2.Request("POST", "https://synthetic.invalid")
    errors: dict[str, BaseException] = {
        "connection": GuardedConnection(message=SENTINEL, request=request),
        "timeout": GuardedTimeout(request),
        "local_timeout": GuardedLocalTimeout(SENTINEL),
        "cancelled": GuardedCancellation(SENTINEL),
        "unknown": GuardedUnknown(SENTINEL),
    }
    error = errors[kind]
    cast(Any, error).observed_payloads = []
    failure = ProviderFailure.from_exception(error)
    assert failure.diagnostic.model_dump(mode="json") == diagnostic_v2_wire(
        category=category, http_status=None, rejection_reason=None
    )
    assert cast(Any, error).observed_payloads == []
    assert (
        SENTINEL not in str(failure) + repr(failure) + failure_envelope(failure).model_dump_json()
    )


def test_untyped_matching_status_and_body_cannot_classify_rejection() -> None:
    error = HostileUnknown(SENTINEL)
    cast(Any, error).status_code = 400
    cast(Any, error).body = grammar_body(nested=True)
    assert classify_failure(error).model_dump(mode="json") == diagnostic_v2_wire(
        category="unknown", http_status=None, rejection_reason=None
    )


class LifecycleStatus(HostileStatus):
    def __init__(self, behavior: str) -> None:
        super().__init__(
            SENTINEL,
            response=httpx2.Response(
                400, request=httpx2.Request("POST", "https://synthetic.invalid")
            ),
            body=grammar_body(nested=True),
        )
        self.behavior = behavior
        self.body_reads = 0
        self.status_reads = 0

    def __getattribute__(self, name: str) -> Any:
        if name == "status_code":
            self.status_reads += 1
            if self.behavior == "hostile_status":
                raise AssertionError(SENTINEL)
        if name == "body":
            self.body_reads += 1
            if self.behavior == "hostile_first" or (
                self.body_reads > 1 and self.behavior == "raising"
            ):
                raise AssertionError(SENTINEL)
            if self.body_reads > 1:
                return {"error": {"type": "invalid_request_error", "message": SENTINEL}}
            return object.__getattribute__(self, name)
        return super().__getattribute__(name)


@pytest.mark.parametrize("operation", ["count", "generation"])
@pytest.mark.parametrize("behavior", ["changing", "raising", "hostile_first"])
def test_direct_adapter_normalizes_original_sdk_lifecycle_once(
    operation: str, behavior: str
) -> None:
    original = LifecycleStatus(behavior)
    initial_keys = set(vars(original))
    fake = RecordingMessages(
        count_error=original if operation == "count" else None,
        create_error=original if operation == "generation" else None,
    )
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    failure = outcome.failure
    assert failure is not None and failure.code == "provider_error"
    reason = "unclassified" if behavior == "hostile_first" else "schema_complexity"
    assert failure.diagnostic.model_dump(mode="json") == diagnostic_v2_wire(rejection_reason=reason)
    assert original.body_reads == original.status_reads == 1
    assert set(vars(original)) == initial_keys
    assert failure.__context__ is None and failure.__cause__ is None
    for _ in range(3):
        failure = ProviderFailure.from_exception(failure)
        assert failure_envelope(failure).diagnostic.model_dump(mode="json") == diagnostic_v2_wire(
            rejection_reason=reason
        )
        assert _error_code(failure) == "provider_error"
        assert original.body_reads == original.status_reads == 1
        assert failure.__context__ is None and failure.__cause__ is None
    assert (
        SENTINEL
        not in outcome.log
        + str(failure)
        + repr(failure)
        + failure_envelope(failure).model_dump_json()
    )
    generation = operation == "generation"
    assert len(fake.counts) == 1 and len(fake.creates) == int(generation)
    assert_accounting(
        outcome,
        actual=None if generation else "0",
        reserved="0.027" if generation else "0",
        complete=not generation,
        generation_count=int(generation),
    )
    assert ledger.committed_usd == Decimal("0.027" if generation else "0")
    assert ledger.stopped is generation


class SDKConnectionAndTimeout(GuardedConnection, TimeoutError):
    pass


class SDKTimeoutAndTimeout(GuardedTimeout, TimeoutError):
    pass


class SDKStatusAndTimeout(LifecycleStatus, TimeoutError):
    pass


class CodeOnlyValidation(PayloadObservationGuard, NoBodyResponseValidation):
    status_reads = 0
    status_forbidden = False

    def __getattribute__(self, name: str) -> Any:
        if name == "status_code":
            self.status_reads += 1
            if self.status_forbidden:
                raise AssertionError(SENTINEL)
        return super().__getattribute__(name)


class SDKValidationAndTimeout(CodeOnlyValidation, TimeoutError):
    pass


@pytest.mark.parametrize(
    "kind,code",
    [
        ("status", "provider_error"),
        ("status_timeout", "provider_error"),
        ("status_hostile_status", "provider_error"),
        ("status_hostile_body", "provider_error"),
        ("connection", "provider_error"),
        ("connection_timeout", "provider_error"),
        ("timeout", "deadline_exceeded"),
        ("timeout_timeout", "deadline_exceeded"),
        ("validation", "provider_error"),
        ("validation_timeout", "provider_error"),
        ("validation_hostile_status", "provider_error"),
        ("local_timeout", "deadline_exceeded"),
        ("cancelled", "deadline_exceeded"),
        ("unknown", "provider_error"),
    ],
)
def test_code_only_classification_never_reads_sdk_body_status_or_text(kind: str, code: str) -> None:
    request = httpx2.Request("POST", "https://synthetic.invalid")
    response = httpx2.Response(400, request=request)
    errors: dict[str, BaseException] = {
        "status": LifecycleStatus("raising"),
        "status_timeout": SDKStatusAndTimeout("raising"),
        "status_hostile_status": LifecycleStatus("hostile_status"),
        "status_hostile_body": LifecycleStatus("hostile_first"),
        "connection": GuardedConnection(message=SENTINEL, request=request),
        "connection_timeout": SDKConnectionAndTimeout(message=SENTINEL, request=request),
        "timeout": GuardedTimeout(request),
        "timeout_timeout": SDKTimeoutAndTimeout(request),
        "validation": CodeOnlyValidation(
            response, body=grammar_body(nested=True), message=SENTINEL
        ),
        "validation_timeout": SDKValidationAndTimeout(
            response, body=grammar_body(nested=True), message=SENTINEL
        ),
        "validation_hostile_status": CodeOnlyValidation(
            response, body=grammar_body(nested=True), message=SENTINEL
        ),
        "local_timeout": GuardedLocalTimeout(SENTINEL),
        "cancelled": GuardedCancellation(SENTINEL),
        "unknown": GuardedUnknown(SENTINEL),
    }
    error = errors[kind]
    cast(Any, error).observed_payloads = []
    if isinstance(error, CodeOnlyValidation):
        error.status_reads = 0
        error.status_forbidden = kind == "validation_hostile_status"
    assert _error_code(error) == code
    assert cast(Any, error).observed_payloads == []
    if isinstance(error, LifecycleStatus):
        assert error.body_reads == error.status_reads == 0
    if isinstance(error, NoBodyResponseValidation):
        assert error.body_reads == 0
    if isinstance(error, CodeOnlyValidation):
        assert error.status_reads == 0


@pytest.mark.parametrize("version", [1, 2])
def test_code_only_provider_failure_preserves_validated_explicit_version(version: int) -> None:
    wire = diagnostic_wire(category="local_guard", http_status=None)
    diagnostic: FailureDiagnostic | FailureDiagnosticV2
    if version == 2:
        diagnostic = FailureDiagnosticV2.model_validate(
            diagnostic_v2_wire(category="local_guard", http_status=None, rejection_reason=None)
        )
    else:
        diagnostic = FailureDiagnostic.model_validate(wire)
    failure = ProviderFailure("budget_exhausted", diagnostic=diagnostic)
    assert _error_code(failure) == "budget_exhausted"
    assert failure.diagnostic.schema_version == version
    assert set(failure.diagnostic.model_dump()) == set(
        wire if version == 1 else diagnostic.model_dump()
    )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("mutation", ["code", "status", "version"])
def test_code_only_provider_failure_revalidates_corrupted_values(
    version: int, mutation: str
) -> None:
    diagnostic = (
        FailureDiagnostic.model_validate(diagnostic_wire())
        if version == 1
        else FailureDiagnosticV2.model_validate(diagnostic_v2_wire())
    )
    failure = ProviderFailure("provider_error", diagnostic=diagnostic)
    if mutation == "code":
        failure.code = "deadline_exceeded"
    else:
        failure.diagnostic = diagnostic.model_copy(
            update={"http_status" if mutation == "status" else "schema_version": True}
        )
    with pytest.raises(ValidationError):
        _error_code(failure)


def test_code_only_provider_failure_revalidates_required_v2_reason() -> None:
    diagnostic = FailureDiagnosticV2.model_validate(diagnostic_v2_wire())
    failure = ProviderFailure("provider_error", diagnostic=diagnostic)
    failure.diagnostic = diagnostic.model_copy(update={"rejection_reason": None})
    with pytest.raises(ValidationError):
        _error_code(failure)
