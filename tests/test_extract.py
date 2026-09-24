"""Synthetic extraction protocol checks; no live requests or measured claims."""

import asyncio
import builtins
import importlib
import inspect
import json
import os
import socket
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from typing import Any, cast
from uuid import UUID

import pytest
from anthropic import transform_schema
from anthropic.types import Message, MessageTokensCount, TextBlock

import rent_navigator.extract as extract_module
from rent_navigator.extract import EXTRACTION_SYSTEM, extract_letter, extraction_config_hash
from rent_navigator.models import Extraction, ExtractRequest
from rent_navigator.provider import Deadline, ProviderAdapter, ProviderFailure, SpendLedger
from rent_navigator.trace import (
    ACTOR_MODEL,
    PRICING_HASH,
    MetadataSink,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    provider_cost_totals,
)

RAW_SENTINEL = "SYNTHETIC PRIVATE LETTER SENTINEL"
REDACTED_FIXTURE = "Synthetic rent $1,234.50 to $1,259.80 effective April 1, 2027."
VALID_JSON = '{"current_cents":123450,"proposed_cents":125980,"effective_on":"2027-04-01"}'
ATTEMPT_ID = UUID("99f67a72-a97e-48c6-a223-6e3d13cc5a84")
TRACE_ID = UUID("32704c15-0831-4945-80ac-10769783df7f")


class SyntheticClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class RecordingMessages:
    def __init__(self, response: Message) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[str] = []

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.events.append("count")
        self.calls.append(("count", kwargs))
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        self.events.append("create")
        self.calls.append(("create", kwargs))
        return self.response


def synthetic_message(
    text: str = VALID_JSON,
    *,
    stop_reason: str | None = "end_turn",
    content: list[dict[str, Any]] | None = None,
) -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_extraction",
            "type": "message",
            "role": "assistant",
            "model": ACTOR_MODEL,
            "content": content if content is not None else [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )


def synthetic_trace(clock: SyntheticClock) -> tuple[TraceRecorder, StringIO]:
    stream = StringIO()
    trace = TraceRecorder(
        TraceContext(
            attempt_id=ATTEMPT_ID,
            trace_id=TRACE_ID,
            phase="extraction",
            source_commit="a" * 40,
            config_hash=extraction_config_hash(),
            corpus_hash="b" * 64,
            pricing_hash=PRICING_HASH,
        ),
        MetadataSink(stream),
        monotonic=clock,
        utc_now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
    )
    return trace, stream


def synthetic_request(letter: str = RAW_SENTINEL) -> ExtractRequest:
    return ExtractRequest(attempt_id=ATTEMPT_ID, letter=letter)


def synthetic_redactor(letter: str) -> str:
    assert letter == RAW_SENTINEL
    return REDACTED_FIXTURE


def records_from(stream: StringIO) -> list[TraceRecord]:
    return [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]


def test_redaction_precedes_identical_full_count_and_create_requests() -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())
    budget = SpendLedger(Decimal("1"))
    provider = ProviderAdapter(fake, budget=budget)

    def redact(letter: str) -> str:
        fake.events.append("redact")
        return synthetic_redactor(letter)

    result = asyncio.run(
        extract_letter(
            synthetic_request(),
            provider=provider,
            trace=trace,
            redact=redact,
            deadline=Deadline.start(clock=clock),
        )
    )
    assert result == Extraction(
        current_cents=123450, proposed_cents=125980, effective_on=date(2027, 4, 1)
    )
    assert fake.events == ["redact", "count", "create"]
    assert len(fake.calls) == 2
    for operation, parameters in fake.calls:
        assert parameters["model"] == ACTOR_MODEL
        assert parameters["system"] == EXTRACTION_SYSTEM
        assert parameters["messages"] == [{"role": "user", "content": REDACTED_FIXTURE}]
        assert parameters["output_config"] == {
            "format": {
                "type": "json_schema",
                "schema": transform_schema(Extraction.model_json_schema()),
            }
        }
        assert RAW_SENTINEL not in json.dumps(parameters)
        if operation == "create":
            assert parameters["max_tokens"] == 1200
            assert parameters["extra_body"] == {"temperature": 0}
            assert parameters["thinking"] == {"type": "disabled"}
    # The caller can append stages because extraction has not finished its trace.
    with trace.stage("validation"):
        pass
    endpoint = trace.finish("ok")
    assert [stage.stage for stage in endpoint.stage_durations] == [
        "redaction",
        "preflight",
        "validation",
        "generation",
        "validation",
        "validation",
    ]
    records = records_from(stream)
    assert len(records) == 3
    assert records[0].provider_operation == "count_tokens"
    assert records[0].actual_cost_usd == Decimal(0)
    assert records[1].provider_operation == "generation"
    assert provider_cost_totals(records).actual_cost_usd == Decimal("0.0015")
    assert endpoint.actual_cost_usd == Decimal("0.0015")
    for forbidden in (RAW_SENTINEL, REDACTED_FIXTURE, VALID_JSON):
        assert forbidden not in stream.getvalue()


@pytest.mark.parametrize(
    "text, expected",
    [
        (VALID_JSON, (123450, 125980, date(2027, 4, 1))),
        ('{"current_cents":null,"proposed_cents":null,"effective_on":null}', (None, None, None)),
        ('{"current_cents":1,"proposed_cents":null,"effective_on":null}', (1, None, None)),
        (
            '{"current_cents":null,"proposed_cents":1,"effective_on":"2028-02-29"}',
            (None, 1, date(2028, 2, 29)),
        ),
        (
            '{"current_cents":null,"proposed_cents":null,"effective_on":"0001-01-01"}',
            (None, None, date(1, 1, 1)),
        ),
    ],
)
def test_strict_extraction_accepts_exact_values_and_nullable_unknowns(
    text: str, expected: tuple[int | None, int | None, date | None]
) -> None:
    clock = SyntheticClock()
    trace, _stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message(text))
    result = asyncio.run(
        extract_letter(
            synthetic_request(),
            provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
            trace=trace,
            redact=synthetic_redactor,
            deadline=Deadline.start(clock=clock),
        )
    )
    assert (result.current_cents, result.proposed_cents, result.effective_on) == expected
    assert Extraction.model_validate_json(result.model_dump_json()) == result
    trace.finish("ok")


INVALID_JSON_VALUES = [
    "SYNTHETIC INVALID GENERATED SENTINEL",
    "```json\n" + VALID_JSON + "\n```",
    VALID_JSON[:-1],
    VALID_JSON + " trailing",
    "null",
    "[]",
    "{}",
    '{"current_cents":123450,"proposed_cents":125980}',
    '{"proposed_cents":125980,"effective_on":"2027-04-01"}',
    '{"current_cents":123450,"effective_on":"2027-04-01"}',
    VALID_JSON[:-1] + ',"scope":"confirmed"}',
]
for _field in ("current_cents", "proposed_cents"):
    for _value in (True, False, "123450", 0, -1, 1.5, "unknown", "none", ""):
        _payload = json.loads(VALID_JSON)
        _payload[_field] = _value
        INVALID_JSON_VALUES.append(json.dumps(_payload))
for _value in ("2027-02-29", "2027-4-1", "2027-04-01T00:00:00", "April 1, 2027", "", 1):
    _payload = json.loads(VALID_JSON)
    _payload["effective_on"] = _value
    INVALID_JSON_VALUES.append(json.dumps(_payload))


@pytest.mark.parametrize("text", INVALID_JSON_VALUES)
def test_invalid_output_is_not_repaired_and_billed_usage_survives(
    text: str, caplog: pytest.LogCaptureFixture
) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message(text))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "invalid_generated_output"
    endpoint = trace.finish(error.value.code)
    assert len(fake.calls) == 2
    assert endpoint.response_code == "invalid_generated_output"
    assert endpoint.stage_durations[-1].stage == "validation"
    assert endpoint.stage_durations[-1].status == "failed"
    records = records_from(stream)
    assert records[1].usage_complete is True
    assert records[1].actual_cost_usd == Decimal("0.0015")
    assert endpoint.actual_cost_usd == provider_cost_totals(records).actual_cost_usd
    assert endpoint.reserved_cost_usd == Decimal("0.027")
    logs = stream.getvalue() + caplog.text
    assert RAW_SENTINEL not in logs
    assert "SYNTHETIC INVALID GENERATED SENTINEL" not in logs
    assert "ValidationError" not in logs
    assert "input_value" not in logs
    assert text not in str(error.value)


@pytest.mark.parametrize(
    "stop_reason",
    [
        None,
        "max_tokens",
        "refusal",
        "tool_use",
        "stop_sequence",
        "pause_turn",
        "model_context_window_exceeded",
    ],
)
def test_non_end_turn_cannot_be_successful_extraction(stop_reason: str | None) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message(stop_reason=stop_reason))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "invalid_generated_output"
    trace.finish(error.value.code)
    assert len(fake.calls) == 2
    assert provider_cost_totals(records_from(stream)).actual_cost_usd == Decimal("0.0015")


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "text", "text": VALID_JSON}, {"type": "text", "text": ""}],
        [{"type": "tool_use", "id": "toolu_synthetic", "name": "rent_increase_check", "input": {}}],
        [{"type": "thinking", "thinking": "synthetic", "signature": "synthetic"}],
        [{"type": "redacted_thinking", "data": "synthetic"}],
        [
            {
                "type": "text",
                "text": VALID_JSON,
                "citations": [
                    {
                        "type": "char_location",
                        "cited_text": "synthetic",
                        "document_index": 0,
                        "document_title": "synthetic",
                        "start_char_index": 0,
                        "end_char_index": 9,
                    }
                ],
            }
        ],
    ],
)
def test_extraction_rejects_unexpected_blocks(content: list[dict[str, Any]]) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message(content=content))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "invalid_generated_output"
    trace.finish(error.value.code)
    assert provider_cost_totals(records_from(stream)).actual_cost_usd == Decimal("0.0015")


def test_redactor_is_required_without_default_and_failure_prevents_all_requests() -> None:
    parameter = inspect.signature(extract_letter).parameters["redact"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())

    def failed_redactor(_letter: str) -> str:
        raise ValueError(RAW_SENTINEL)

    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=failed_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "provider_error"
    endpoint = trace.finish(error.value.code)
    assert fake.calls == []
    assert endpoint.actual_cost_usd == Decimal(0)
    assert endpoint.stage_durations[0].status == "failed"
    assert RAW_SENTINEL not in stream.getvalue()
    assert RAW_SENTINEL not in str(error.value)


@pytest.mark.parametrize("expires_before_redaction", [True, False])
def test_redaction_uses_the_existing_absolute_deadline(expires_before_redaction: bool) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())
    deadline = Deadline.start(clock=clock)
    entered: list[str] = []
    if expires_before_redaction:
        clock.now += 45

    def redact(letter: str) -> str:
        entered.append("redaction")
        clock.now += 45
        return synthetic_redactor(letter)

    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=redact,
                deadline=deadline,
            )
        )
    assert error.value.code == "deadline_exceeded"
    endpoint = trace.finish(error.value.code)
    assert fake.calls == []
    assert entered == ([] if expires_before_redaction else ["redaction"])
    assert endpoint.actual_cost_usd == Decimal(0)
    assert RAW_SENTINEL not in stream.getvalue()


def test_validation_time_is_inside_the_shared_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())
    original: Callable[..., Extraction] = Extraction.model_validate_json

    def validate(*args: Any, **kwargs: Any) -> Extraction:
        result = original(*args, **kwargs)
        clock.now += 45
        return result

    monkeypatch.setattr(Extraction, "model_validate_json", validate)
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "deadline_exceeded"
    endpoint = trace.finish(error.value.code)
    assert len(fake.calls) == 2
    assert endpoint.stage_durations[-1].status == "failed"
    assert provider_cost_totals(records_from(stream)).actual_cost_usd == Decimal("0.0015")


def test_config_hash_is_stable_and_tracks_the_fixed_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    original = extraction_config_hash()
    assert original == extraction_config_hash()
    assert len(original) == 64
    monkeypatch.setattr(
        extract_module, "EXTRACTION_SYSTEM", EXTRACTION_SYSTEM + " Synthetic change."
    )
    assert extraction_config_hash() != original


def test_schema_transformation_does_not_change_authoritative_local_constraints() -> None:
    local = Extraction.model_json_schema()
    before = json.dumps(local, sort_keys=True)
    transformed = transform_schema(local)
    assert transformed["required"] == ["current_cents", "proposed_cents", "effective_on"]
    assert transformed["additionalProperties"] is False
    assert "exclusiveMinimum" not in transformed["properties"]["current_cents"]["anyOf"][0]
    assert local["properties"]["current_cents"]["anyOf"][0]["exclusiveMinimum"] == 0
    assert json.dumps(local, sort_keys=True) == before


@pytest.mark.parametrize("text", [None, 1, True, b'{"current_cents":null}'])
def test_malformed_sdk_text_values_are_safely_rejected(text: object) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    block = TextBlock(type="text", text=VALID_JSON).model_copy(update={"text": text})
    message = synthetic_message().model_copy(update={"content": [block]})
    fake = RecordingMessages(message)
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "invalid_generated_output"
    trace.finish(error.value.code)
    assert provider_cost_totals(records_from(stream)).actual_cost_usd == Decimal("0.0015")


@pytest.mark.parametrize("content", [None, VALID_JSON, {"type": "text", "text": VALID_JSON}])
def test_malformed_sdk_content_containers_are_safely_rejected(content: object) -> None:
    clock = SyntheticClock()
    trace, stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message().model_copy(update={"content": content}))
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "invalid_generated_output"
    trace.finish(error.value.code)
    assert provider_cost_totals(records_from(stream)).actual_cost_usd == Decimal("0.0015")


def test_invalid_redactor_result_never_reaches_counting() -> None:
    clock = SyntheticClock()
    trace, _stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())
    invalid_redactor = cast(Callable[[str], str], lambda _letter: None)
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=invalid_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    assert error.value.code == "provider_error"
    trace.finish(error.value.code)
    assert fake.calls == []


def test_redaction_cancellation_propagates_without_provider_requests() -> None:
    clock = SyntheticClock()
    trace, _stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())

    def cancelled_redactor(_letter: str) -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=cancelled_redactor,
                deadline=Deadline.start(clock=clock),
            )
        )
    endpoint = trace.finish("deadline_exceeded")
    assert endpoint.stage_durations[0].status == "failed"
    assert fake.calls == []


def test_import_and_fake_extraction_need_no_file_credentials_or_network_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SyntheticClock()
    trace, _stream = synthetic_trace(clock)
    fake = RecordingMessages(synthetic_message())

    def blocked(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("External access during synthetic extraction")

    getenv = os.getenv

    def read_setting(key: str, default: str | None = None) -> str | None:
        if key.startswith("ANTHROPIC_"):
            raise AssertionError("Credential access during synthetic extraction")
        return getenv(key, default)

    async def run() -> Extraction:
        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", blocked)
            patch.setattr(os, "getenv", read_setting)
            patch.setattr(socket, "create_connection", blocked)
            patch.setattr(socket, "getaddrinfo", blocked)
            importlib.reload(extract_module)
            return await extract_module.extract_letter(
                synthetic_request(),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("1"))),
                trace=trace,
                redact=synthetic_redactor,
                deadline=Deadline.start(clock=clock),
            )

    result = asyncio.run(run())
    assert result.current_cents == 123450
    assert len(fake.calls) == 2
    trace.finish("ok")
