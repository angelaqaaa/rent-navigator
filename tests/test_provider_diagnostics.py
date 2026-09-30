"""Synthetic failure classification and privacy checks; no external transport."""

import asyncio
import json
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
from test_provider import RecordingMessages, assert_accounting, perform, reply

from rent_navigator.models import ErrorCode
from rent_navigator.provider import (
    Deadline,
    FailureDiagnostic,
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
        "schema_version": 1,
        "category": category,
        "http_status": status,
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
    assert FailureEnvelope.model_validate_json(envelope.model_dump_json()) == envelope


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
    code: str, category: str, status: int | None
) -> None:
    wire = {"code": code, "diagnostic": diagnostic_wire(category=category, http_status=status)}
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
