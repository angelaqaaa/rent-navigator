"""Synthetic metadata fixtures only; these are not serving measurements."""

import json
import traceback
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from rent_navigator.model_policy import MODEL_POLICIES, RequestedModel, policy_manifest
from rent_navigator.trace import (
    ACTOR_MODEL,
    JUDGE_MODEL,
    PRICING_HASH,
    CheckMetadata,
    MetadataSink,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    UnsafeMetadataError,
    Usage,
    cost_for_usage,
    provider_cost_totals,
)

SYNTHETIC_ATTEMPT = UUID("6ac019e5-e9d6-4257-8b15-079d3ce102ce")
SYNTHETIC_TRACE = UUID("dd848c46-c7b9-40db-9951-2dbf91781f67")
SYNTHETIC_ANALYSIS_TRACE = UUID("ae5f90ce-58b6-4343-91b2-974a1b631c66")
SYNTHETIC_UTC = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class SyntheticClock:
    def __init__(self) -> None:
        self.elapsed = 100.0

    def monotonic(self) -> float:
        return self.elapsed

    def utc_now(self) -> datetime:
        return SYNTHETIC_UTC + timedelta(seconds=self.elapsed - 100)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


def synthetic_context() -> TraceContext:
    return TraceContext(
        attempt_id=SYNTHETIC_ATTEMPT,
        trace_id=SYNTHETIC_TRACE,
        phase="extraction",
        source_commit="a" * 40,
        config_hash="b" * 64,
        corpus_hash=None,
        pricing_hash=PRICING_HASH,
    )


def synthetic_recorder(
    *, context: TraceContext | None = None
) -> tuple[TraceRecorder, SyntheticClock, StringIO]:
    clock, stream = SyntheticClock(), StringIO()
    recorder = TraceRecorder(
        context or synthetic_context(),
        MetadataSink(stream),
        monotonic=clock.monotonic,
        utc_now=clock.utc_now,
    )
    return recorder, clock, stream


def records_from(stream: StringIO) -> list[TraceRecord]:
    return [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]


def test_synthetic_exact_actor_and_judge_costs() -> None:
    usage = Usage.model_validate_json('{"input_tokens": 1234, "output_tokens": 567}')
    actor = cost_for_usage(ACTOR_MODEL, usage)
    judge = cost_for_usage(JUDGE_MODEL, usage)
    assert actor.actual_cost_usd == Decimal("0.004069")
    assert actor.reserved_cost_usd == Decimal("0.019")
    assert judge.actual_cost_usd == Decimal("0.008138")
    assert judge.reserved_cost_usd == Decimal("0.034")
    assert actor.usage_complete is True
    assert json.loads(actor.model_dump_json())["actual_cost_usd"] == "0.004069"


@pytest.mark.parametrize(
    "usage",
    [
        None,
        Usage(input_tokens=None, output_tokens=None),
        Usage(input_tokens=100, output_tokens=None),
        Usage(input_tokens=None, output_tokens=100),
    ],
)
@pytest.mark.parametrize(("model", "reserved"), [(ACTOR_MODEL, "0.019"), (JUDGE_MODEL, "0.034")])
def test_synthetic_missing_usage_retains_reservation(
    usage: Usage | None, model: RequestedModel, reserved: str
) -> None:
    cost = cost_for_usage(model, usage)
    assert cost.actual_cost_usd is None
    assert cost.usage_complete is False
    assert cost.reserved_cost_usd == Decimal(reserved)


def test_shared_model_policy_and_pricing_identity_cover_model_limits() -> None:
    manifest = policy_manifest()
    assert manifest == {
        ACTOR_MODEL: {
            "preflight_limit": 15000,
            "reserved_input_tokens": 16000,
            "max_output_tokens": 600,
            "input_per_million": "1",
            "output_per_million": "5",
        },
        JUDGE_MODEL: {
            "preflight_limit": 12000,
            "reserved_input_tokens": 13000,
            "max_output_tokens": 800,
            "input_per_million": "2",
            "output_per_million": "10",
        },
    }
    assert MODEL_POLICIES[ACTOR_MODEL].reservation_usd == Decimal("0.019")
    assert MODEL_POLICIES[JUDGE_MODEL].reservation_usd == Decimal("0.034")
    assert (
        PRICING_HASH
        == sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert PRICING_HASH != "b36ccff4b6bc11e7343861e7400ba18a710b14659b6cb1a3d7eab72390897b4d"
    manifest[ACTOR_MODEL]["reserved_input_tokens"] = 8000
    assert policy_manifest()[ACTOR_MODEL]["reserved_input_tokens"] == 16000


def test_current_capacity_forecast_preserves_exact_remaining_budget() -> None:
    actor = MODEL_POLICIES[ACTOR_MODEL].reservation_usd
    judge = MODEL_POLICIES[JUDGE_MODEL].reservation_usd
    release = 298 * actor + 160 * judge
    gate = 77 * actor + 39 * judge
    development = 3 * actor + judge
    assert (release, gate, development) == (Decimal("11.102"), Decimal("2.789"), Decimal("0.091"))
    before_correction = release + 3 * gate + 13 * development
    after_correction = release + 2 * gate + 13 * development
    assert before_correction == Decimal("20.652")
    assert after_correction == Decimal("17.863")
    assert before_correction == gate + after_correction
    assert Decimal("21") - Decimal("0.172127") - before_correction == Decimal("0.175873")


def test_synthetic_zero_is_known_usage_and_overage_is_not_clamped() -> None:
    zero = cost_for_usage(ACTOR_MODEL, Usage(input_tokens=0, output_tokens=0))
    assert zero.actual_cost_usd == Decimal(0)
    assert zero.usage_complete is True
    overage = cost_for_usage(ACTOR_MODEL, Usage(input_tokens=17000, output_tokens=600))
    assert overage.actual_cost_usd == Decimal("0.020")
    assert overage.reserved_cost_usd == Decimal("0.019")


@pytest.mark.parametrize(
    "payload",
    [
        {"input_tokens": True, "output_tokens": 0},
        {"input_tokens": "1", "output_tokens": 0},
        {"input_tokens": -1, "output_tokens": 0},
        {"input_tokens": 1},
        {"input_tokens": 1, "output_tokens": 0, "cache_creation_input_tokens": 10},
    ],
)
def test_synthetic_usage_rejects_coercion_missing_fields_and_new_categories(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        Usage.model_validate_json(json.dumps(payload))


def test_synthetic_stage_timing_call_metadata_and_no_double_counting() -> None:
    trace, clock, stream = synthetic_recorder()
    with trace.stage("preflight"):
        with trace.provider_call(ACTOR_MODEL, operation="count_tokens"):
            clock.advance(0.125)
    with trace.stage("generation"):
        with trace.provider_call(ACTOR_MODEL) as call:
            clock.advance(0.250)
            call.usage = Usage(input_tokens=1000, output_tokens=100)
            call.returned_model_id = ACTOR_MODEL
    clock.advance(0.125)
    endpoint = trace.finish("ok")
    records = records_from(stream)
    assert len(records) == 3
    assert [record.record_kind for record in records] == [
        "provider_call",
        "provider_call",
        "endpoint",
    ]
    assert [record.provider_call_index for record in records] == [1, 2, None]
    assert records[0].actual_cost_usd == Decimal(0)
    assert records[0].reserved_cost_usd == Decimal(0)
    assert records[0].usage_complete is True
    assert records[1].requested_model_id == ACTOR_MODEL
    assert records[1].returned_model_id == ACTOR_MODEL
    assert endpoint.duration_ms == 500
    assert [stage.duration_ms for stage in endpoint.stage_durations] == [125, 250]
    assert endpoint.timestamp == SYNTHETIC_UTC
    assert endpoint.corpus_hash is None
    total = provider_cost_totals(records)
    assert total.actual_cost_usd == Decimal("0.0015")
    assert total.reserved_cost_usd == Decimal("0.019")
    assert total.input_tokens == 1000
    assert total.output_tokens == 100
    assert total.actual_cost_usd == endpoint.actual_cost_usd
    with pytest.raises(ValueError, match="Duplicate provider call"):
        provider_cost_totals([*records, records[1]])
    with pytest.raises(ValueError, match="already finished"):
        trace.finish("ok")


def test_synthetic_failed_call_and_stage_keep_duration_and_unknown_cost() -> None:
    trace, clock, stream = synthetic_recorder()
    with pytest.raises(TimeoutError, match="private synthetic letter"):
        with trace.stage("generation"), trace.provider_call(ACTOR_MODEL) as call:
            clock.advance(0.375)
            call.response_code = "deadline_exceeded"
            raise TimeoutError("private synthetic letter and raw provider exception")
    endpoint = trace.finish("deadline_exceeded")
    records = records_from(stream)
    assert len(records) == 2
    assert records[0].duration_ms == 375
    assert endpoint.duration_ms == 375
    assert endpoint.stage_durations[0].status == "failed"
    assert records[0].response_code == endpoint.response_code == "deadline_exceeded"
    assert endpoint.actual_cost_usd is None
    assert endpoint.usage_complete is False
    assert endpoint.reserved_cost_usd == Decimal("0.019")
    assert "private synthetic letter" not in stream.getvalue()
    assert "raw provider exception" not in stream.getvalue()


def test_synthetic_billed_failed_call_is_included_and_unclassified_error_is_safe() -> None:
    trace, clock, stream = synthetic_recorder()
    with pytest.raises(RuntimeError):
        with trace.provider_call(ACTOR_MODEL) as call:
            call.usage = Usage(input_tokens=100, output_tokens=20)
            clock.advance(0.125)
            raise RuntimeError("synthetic rejected output")
    endpoint = trace.finish("invalid_generated_output")
    assert records_from(stream)[0].response_code == "provider_error"
    assert endpoint.actual_cost_usd == Decimal("0.0002")
    assert endpoint.usage_complete is True


def test_synthetic_missing_call_makes_entire_endpoint_cost_incomplete() -> None:
    trace, _, stream = synthetic_recorder()
    with trace.provider_call(ACTOR_MODEL) as call:
        call.usage = Usage(input_tokens=1000, output_tokens=100)
    with trace.provider_call(ACTOR_MODEL) as call:
        call.usage = Usage(input_tokens=50, output_tokens=None)
    endpoint = trace.finish("provider_error")
    total = provider_cost_totals(records_from(stream))
    assert total.input_tokens == 1050
    assert total.output_tokens is None
    assert endpoint.actual_cost_usd is total.actual_cost_usd is None
    assert total.reserved_cost_usd == Decimal("0.038")


def test_synthetic_attempt_links_extraction_and_analysis_without_text() -> None:
    extract, clock, stream = synthetic_recorder()
    clock.advance(0.125)
    extract_record = extract.finish("ok")
    analysis_context = TraceContext.model_validate(
        synthetic_context().model_copy(
            update={"trace_id": SYNTHETIC_ANALYSIS_TRACE, "phase": "analysis"}
        )
    )
    analysis, analysis_clock, _ = synthetic_recorder(context=analysis_context)
    analysis_clock.advance(0.250)
    analysis_record = analysis.finish(
        "answered",
        tool_name="notice_deadline_check",
        check_statuses=(CheckMetadata(id="notice", status="fail"),),
        retrieved_evidence_ids=("f" * 64, "a" * 64),
        cited_evidence_ids=("a" * 64,),
    )
    assert extract_record.attempt_id == analysis_record.attempt_id
    assert extract_record.trace_id != analysis_record.trace_id
    assert extract_record.phase == "extraction"
    assert analysis_record.phase == "analysis"
    assert analysis_record.check_statuses[0].status == "fail"
    assert extract_record.duration_ms + analysis_record.duration_ms == 375
    assert records_from(stream)[0].actual_cost_usd == Decimal(0)


@pytest.mark.parametrize(
    "key",
    ["question", "letter", "tool_arguments", "generated_text", "ip", "exception", "message"],
)
def test_synthetic_metadata_wire_rejects_forbidden_fields(key: str) -> None:
    trace, _, _ = synthetic_recorder()
    payload = json.loads(trace.finish("ok").model_dump_json())
    payload[key] = "private synthetic payload"
    with pytest.raises(ValidationError):
        TraceRecord.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("phase", "private synthetic letter"),
        ("response_code", "private synthetic exception"),
        ("source_commit", "127.0.0.1"),
        ("config_hash", "synthetic@example.com"),
        ("corpus_hash", "private synthetic letter"),
        ("pricing_hash", "a" * 64),
        ("returned_model_id", "private synthetic output"),
        ("returned_model_id", "claude-synthetic@example.com"),
        ("requested_model_id", "claude-unapproved"),
        ("tool_name", "private synthetic arguments"),
        ("retrieved_evidence_ids", ["private synthetic question"]),
        ("cited_evidence_ids", ["a" * 64, "a" * 64]),
        (
            "stage_durations",
            [{"stage": "private synthetic question", "duration_ms": 1, "status": "completed"}],
        ),
        ("check_statuses", [{"id": "scope", "status": "private synthetic output"}]),
        ("attempt_id", "not-a-uuid"),
        ("duration_ms", -1),
        ("timestamp", "2026-09-19T12:00:00"),
    ],
)
def test_synthetic_metadata_values_reject_prose_and_invalid_state(key: str, value: Any) -> None:
    trace, _, _ = synthetic_recorder()
    payload = json.loads(trace.finish("ok").model_dump_json())
    payload[key] = value
    with pytest.raises(ValidationError):
        TraceRecord.model_validate_json(json.dumps(payload))


def test_synthetic_sink_revalidates_bypassed_models_without_echoing_payload() -> None:
    trace, _, _ = synthetic_recorder()
    record = trace.finish("ok")
    forged = record.model_copy(update={"response_code": "private synthetic payload"})
    stream = StringIO()
    with pytest.raises(UnsafeMetadataError) as error:
        MetadataSink(stream).emit(forged)
    assert stream.getvalue() == ""
    assert str(error.value) == "Invalid trace metadata"
    assert "private synthetic payload" not in str(error.value)


def test_synthetic_record_requires_utc_and_all_nullable_metadata_fields() -> None:
    trace, _, _ = synthetic_recorder()
    record = trace.finish("ok")
    payload = json.loads(record.model_dump_json())
    for key in ("attempt_id", "corpus_hash", "returned_model_id", "tool_name", "actual_cost_usd"):
        changed = dict(payload)
        del changed[key]
        with pytest.raises(ValidationError):
            TraceRecord.model_validate_json(json.dumps(changed))
    with pytest.raises(ValidationError):
        TraceRecord.model_validate(
            record.model_copy(
                update={"timestamp": datetime(2026, 9, 19, tzinfo=timezone(timedelta(hours=1)))}
            )
        )


def test_synthetic_provider_record_rejects_fabricated_cost_and_unknown_free_usage() -> None:
    trace, _, stream = synthetic_recorder()
    with trace.provider_call(ACTOR_MODEL):
        pass
    record = records_from(stream)[0]
    payload = json.loads(record.model_dump_json())
    payload["actual_cost_usd"] = "0"
    with pytest.raises(ValidationError):
        TraceRecord.model_validate_json(json.dumps(payload))
    payload = json.loads(record.model_dump_json())
    payload["reserved_cost_usd"] = "0"
    with pytest.raises(ValidationError):
        TraceRecord.model_validate_json(json.dumps(payload))


def test_synthetic_collection_rejects_endpoint_only_and_missing_call_details() -> None:
    trace, _, stream = synthetic_recorder()
    with trace.provider_call(ACTOR_MODEL) as call:
        call.usage = Usage(input_tokens=100, output_tokens=20)
    with trace.provider_call(ACTOR_MODEL):
        pass
    endpoint = trace.finish("provider_error")
    records = records_from(stream)
    for incomplete in ([endpoint], [records[0], endpoint]):
        with pytest.raises(ValueError, match="does not match"):
            provider_cost_totals(incomplete)
    with pytest.raises(ValueError, match="Incomplete provider call collection"):
        provider_cost_totals([records[1]])
    with pytest.raises(ValueError, match="Duplicate endpoint summary"):
        provider_cost_totals([*records, endpoint])
    assert provider_cost_totals(records).actual_cost_usd is None


def test_synthetic_endpoint_context_captures_unhandled_failure_without_text() -> None:
    trace, clock, stream = synthetic_recorder()
    with pytest.raises(RuntimeError), trace:
        with trace.stage("generation"), trace.provider_call(ACTOR_MODEL):
            clock.advance(0.125)
            raise RuntimeError("private synthetic endpoint error")
    records = records_from(stream)
    assert len(records) == 2
    assert records[1].record_kind == "endpoint"
    assert records[1].response_code == "provider_error"
    assert records[1].actual_cost_usd is None
    assert records[1].duration_ms == 125
    assert "private synthetic" not in stream.getvalue()


def test_synthetic_endpoint_cannot_finish_inside_active_stage_or_call() -> None:
    trace, _, stream = synthetic_recorder()
    with trace.stage("preflight"):
        with pytest.raises(ValueError, match="active trace scopes"):
            trace.finish("ok")
    with trace.provider_call(ACTOR_MODEL, operation="count_tokens"):
        with pytest.raises(ValueError, match="active trace scopes"):
            trace.finish("ok")
    trace.finish("ok")
    assert len(records_from(stream)) == 2


def test_synthetic_sink_rejects_bypassed_extra_fields_and_emits_no_warning(
    recwarn: pytest.WarningsRecorder,
) -> None:
    trace, _, _ = synthetic_recorder()
    record = trace.finish("ok")
    stream = StringIO()
    for key in ("question", "letter", "tool_arguments", "exception"):
        forged = record.model_copy(update={key: "private synthetic payload"})
        with pytest.raises(UnsafeMetadataError):
            MetadataSink(stream).emit(forged)
    assert stream.getvalue() == ""
    assert not recwarn.list


@pytest.mark.parametrize("known_usage", [False, True], ids=["missing-usage", "known-usage"])
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("returned_model_id", "unexpected/model-id"),
        ("response_code", "private synthetic completion status"),
    ],
)
def test_synthetic_invalid_completion_preserves_accounting(
    known_usage: bool,
    field: str,
    invalid_value: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    recwarn: pytest.WarningsRecorder,
) -> None:
    trace, clock, stream = synthetic_recorder()
    with pytest.raises(ValueError) as error, trace:
        with trace.provider_call(ACTOR_MODEL) as call:
            if known_usage:
                call.usage = Usage(input_tokens=1000, output_tokens=100)
            setattr(call, field, invalid_value)
            clock.advance(0.125)

    records = records_from(stream)
    assert len(records) == 2
    detail, endpoint = records
    assert [record.record_kind for record in records] == ["provider_call", "endpoint"]
    assert [record.provider_call_index for record in records] == [1, None]
    total = provider_cost_totals(records)
    for result in (detail, endpoint, total):
        assert result.input_tokens == (1000 if known_usage else None)
        assert result.output_tokens == (100 if known_usage else None)
        assert result.usage_complete is known_usage
        assert result.actual_cost_usd == (Decimal("0.0015") if known_usage else None)
        assert result.reserved_cost_usd == Decimal("0.019")
    for record in records:
        assert record.requested_model_id == ACTOR_MODEL
        assert record.returned_model_id is None
        assert record.response_code == "provider_error"
        assert record.duration_ms == 125
    assert isinstance(error.value, UnsafeMetadataError)
    assert str(error.value) == "Invalid provider completion metadata"
    visible_error = "".join(traceback.format_exception(error.value))
    captured = capsys.readouterr()
    for output in (stream.getvalue(), visible_error, caplog.text, captured.out, captured.err):
        assert invalid_value not in output
        assert "ValidationError" not in output
        assert "input_value=" not in output
    assert not recwarn.list


@pytest.mark.parametrize("known_usage", [False, True], ids=["missing-usage", "known-usage"])
def test_synthetic_rejected_completion_keeps_contiguous_call_accounting(
    known_usage: bool,
) -> None:
    trace, _, stream = synthetic_recorder()
    with pytest.raises(UnsafeMetadataError):
        with trace.provider_call(ACTOR_MODEL) as call:
            call.returned_model_id = "unexpected/model-id"
            if known_usage:
                call.usage = Usage(input_tokens=1000, output_tokens=100)
    with trace.provider_call(ACTOR_MODEL) as call:
        call.usage = Usage(input_tokens=1000, output_tokens=100)
        call.returned_model_id = ACTOR_MODEL
    endpoint = trace.finish("provider_error")
    records = records_from(stream)
    assert [record.provider_call_index for record in records] == [1, 2, None]
    assert records[0].response_code == "provider_error"
    assert records[1].response_code == "ok"
    total = provider_cost_totals(records)
    assert total.actual_cost_usd == endpoint.actual_cost_usd
    assert total.actual_cost_usd == (Decimal("0.003") if known_usage else None)
    assert total.usage_complete is known_usage
    assert total.reserved_cost_usd == endpoint.reserved_cost_usd == Decimal("0.038")
    with pytest.raises(ValueError, match="Duplicate provider call"):
        provider_cost_totals([*records, records[0]])
    with pytest.raises(ValueError, match="does not match"):
        provider_cost_totals([records[0], endpoint])


def test_synthetic_rejected_count_tokens_completion_remains_free() -> None:
    trace, _, stream = synthetic_recorder()
    with pytest.raises(UnsafeMetadataError), trace:
        with trace.provider_call(ACTOR_MODEL, operation="count_tokens") as call:
            call.returned_model_id = "unexpected/model-id"
    records = records_from(stream)
    assert [record.provider_call_index for record in records] == [1, None]
    assert records[0].provider_operation == "count_tokens"
    assert all(record.response_code == "provider_error" for record in records)
    for result in (*records, provider_cost_totals(records)):
        assert result.input_tokens == result.output_tokens == 0
        assert result.usage_complete is True
        assert result.actual_cost_usd == result.reserved_cost_usd == Decimal(0)


def test_synthetic_no_provider_calls_remains_complete_zero() -> None:
    trace, _, stream = synthetic_recorder()
    with trace:
        pass
    records = records_from(stream)
    assert len(records) == 1
    assert records[0].record_kind == "endpoint"
    for result in (records[0], provider_cost_totals(records), provider_cost_totals([])):
        assert result.input_tokens == result.output_tokens == 0
        assert result.usage_complete is True
        assert result.actual_cost_usd == result.reserved_cost_usd == Decimal(0)
