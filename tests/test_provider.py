"""Synthetic recording-port tests; no provider requests or measured serving results."""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, AsyncAnthropic
from anthropic.types import (
    Message,
    MessageParam,
    MessageTokensCount,
    ToolChoiceParam,
)
from anthropic.types import (
    Usage as SDKUsage,
)
from pydantic import TypeAdapter

import rent_navigator.provider as provider_module
from rent_navigator.model_policy import policy_manifest
from rent_navigator.models import provider_tool_definitions
from rent_navigator.provider import (
    Deadline,
    MessagesPort,
    ProviderAdapter,
    ProviderFailure,
    Reservation,
    SpendLedger,
    create_client,
)
from rent_navigator.trace import (
    ACTOR_MODEL,
    JUDGE_MODEL,
    PRICING_HASH,
    MetadataSink,
    RequestedModel,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    provider_cost_totals,
)

SYNTHETIC_CONTENT = "synthetic private content must stay outside metadata"
SYNTHETIC_ERROR = "synthetic provider exception containing sensitive payload"
INPUT: list[MessageParam] = [{"role": "user", "content": SYNTHETIC_CONTENT}]
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}
_DEFAULT_USAGE = object()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def reply(
    *, usage: object = _DEFAULT_USAGE, model: str = ACTOR_MODEL, stop: str = "end_turn"
) -> Message:
    values: dict[str, Any] = {
        "id": "msg_synthetic_fixture",
        "content": [{"type": "text", "text": SYNTHETIC_CONTENT}],
        "model": model,
        "role": "assistant",
        "stop_reason": stop,
        "stop_sequence": None,
        "type": "message",
        "usage": SDKUsage(input_tokens=1000, output_tokens=100)
        if usage is _DEFAULT_USAGE
        else usage,
    }
    return Message.model_construct(**values)


def malformed_usage(**values: Any) -> SDKUsage:
    return SDKUsage.model_construct(**values)


class RecordingMessages:
    def __init__(
        self,
        response: Message | None = None,
        *,
        estimate: object = 1000,
        count_error: BaseException | None = None,
        create_error: BaseException | None = None,
        before_count: Callable[[], Awaitable[None]] | None = None,
        before_create: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.response = response if response is not None else reply()
        self.estimate = estimate
        self.count_error = count_error
        self.create_error = create_error
        self.before_count = before_count
        self.before_create = before_create
        self.counts: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.counts.append(deepcopy(kwargs))
        if self.before_count is not None:
            await self.before_count()
        if self.count_error is not None:
            raise self.count_error
        values: dict[str, Any] = {"input_tokens": self.estimate}
        return MessageTokensCount.model_construct(**values)

    async def create(self, **kwargs: Any) -> Message:
        self.creates.append(deepcopy(kwargs))
        if self.before_create is not None:
            await self.before_create()
        if self.create_error is not None:
            raise self.create_error
        return self.response


def recorder(clock: Clock, *, trace_number: int = 1) -> tuple[TraceRecorder, StringIO]:
    stream = StringIO()
    context = TraceContext(
        attempt_id=UUID("92f1bd5d-8e8b-44aa-9025-98a971f80683"),
        trace_id=UUID(int=trace_number),
        phase="analysis",
        source_commit="a" * 40,
        config_hash="b" * 64,
        corpus_hash="c" * 64,
        pricing_hash=PRICING_HASH,
    )
    return TraceRecorder(
        context,
        MetadataSink(stream),
        monotonic=clock,
        utc_now=lambda: datetime(2026, 9, 22, 12, tzinfo=UTC),
    ), stream


@dataclass
class Outcome:
    response: Message | None
    failure: ProviderFailure | None
    records: list[TraceRecord]
    log: str


async def perform(
    fake: RecordingMessages,
    ledger: SpendLedger,
    *,
    model: RequestedModel = ACTOR_MODEL,
    clock: Clock | None = None,
    deadline: Deadline | None = None,
    trace_number: int = 1,
) -> Outcome:
    clock = clock or Clock()
    trace, stream = recorder(clock, trace_number=trace_number)
    adapter = ProviderAdapter(fake, budget=ledger)
    response, failure = None, None
    with trace:
        try:
            response = await adapter.generate(
                model=model,
                system="synthetic system instructions",
                messages=INPUT,
                trace=trace,
                deadline=deadline or Deadline.start(clock=clock),
            )
        except ProviderFailure as error:
            failure = error
            trace.finish(error.code)
        else:
            trace.finish("ok")
    log = stream.getvalue()
    return Outcome(
        response,
        failure,
        [TraceRecord.model_validate_json(line) for line in log.splitlines()],
        log,
    )


def assert_accounting(
    outcome: Outcome,
    *,
    actual: str | None,
    reserved: str,
    complete: bool,
    generation_count: int = 1,
) -> None:
    generation = [record for record in outcome.records if record.provider_operation == "generation"]
    assert len(generation) == generation_count
    totals = provider_cost_totals(outcome.records)
    assert totals.actual_cost_usd == (Decimal(actual) if actual is not None else None)
    assert totals.reserved_cost_usd == Decimal(reserved)
    assert totals.usage_complete is complete
    endpoint = outcome.records[-1]
    assert endpoint.record_kind == "endpoint"
    assert endpoint.actual_cost_usd == totals.actual_cost_usd
    assert endpoint.reserved_cost_usd == totals.reserved_cost_usd
    assert endpoint.usage_complete == complete
    indices = [record.provider_call_index for record in outcome.records[:-1]]
    assert indices == list(range(1, len(indices) + 1))
    assert SYNTHETIC_CONTENT not in outcome.log
    assert SYNTHETIC_ERROR not in outcome.log


@pytest.mark.parametrize(
    ("model", "max_tokens", "actual", "reservation"),
    [(ACTOR_MODEL, 600, "0.0015", "0.019"), (JUDGE_MODEL, 800, "0.003", "0.024")],
)
def test_fixed_generation_settings_and_count_payload_parity(
    model: RequestedModel, max_tokens: int, actual: str, reservation: str
) -> None:
    async def scenario() -> None:
        clock, ledger = Clock(), SpendLedger(Decimal("1"))
        fake = RecordingMessages(reply(model=model))
        trace, stream = recorder(clock)
        tools = provider_tool_definitions()
        choice: ToolChoiceParam = {"type": "any", "disable_parallel_tool_use": True}
        original_messages, original_tools = deepcopy(INPUT), deepcopy(tools)
        adapter = ProviderAdapter(fake, budget=ledger)
        with trace:
            response = await adapter.generate(
                model=model,
                system="synthetic system instructions",
                messages=INPUT,
                trace=trace,
                deadline=Deadline.start(clock=clock),
                tools=tools,
                tool_choice=choice,
                output_schema=SCHEMA,
            )
            trace.finish("ok")
        assert response is fake.response
        assert len(fake.counts) == len(fake.creates) == 1
        count, create = fake.counts[0], fake.creates[0]
        for field in (
            "model",
            "system",
            "messages",
            "tools",
            "tool_choice",
            "output_config",
            "thinking",
        ):
            assert count[field] == create[field]
        assert create["model"] == model
        assert create["max_tokens"] == max_tokens
        assert create["thinking"] == {"type": "disabled"}
        assert create["stream"] is False
        assert create["service_tier"] == "standard_only"
        assert "service_tier" not in count
        assert create["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
        assert create["tool_choice"] == choice
        assert create["messages"] == original_messages
        assert create["tools"] == original_tools
        assert INPUT == original_messages and tools == original_tools
        assert (
            not {"max_tokens", "stream", "temperature", "top_p", "top_k", "extra_body"}
            & count.keys()
        )
        if model == ACTOR_MODEL:
            assert create["extra_body"] == {"temperature": 0}
        else:
            assert "extra_body" not in create
        assert (
            not {"temperature", "top_p", "top_k", "cache_control", "extra_headers"} & create.keys()
        )
        records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
        assert_accounting(
            Outcome(response, None, records, stream.getvalue()),
            actual=actual,
            reserved=reservation,
            complete=True,
        )
        assert ledger.committed_usd == Decimal(actual)
        assert not ledger.stopped and not ledger.reforecast_required

    asyncio.run(scenario())


def test_no_optional_generation_features_are_inserted() -> None:
    fake, ledger = RecordingMessages(), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is None
    for payload in (fake.counts[0], fake.creates[0]):
        assert (
            not {
                "tools",
                "tool_choice",
                "output_config",
                "cache_control",
                "stop_sequences",
                "metadata",
            }
            & payload.keys()
        )


@pytest.mark.parametrize(
    ("model", "estimate", "allowed", "actual", "reservation"),
    [
        (ACTOR_MODEL, 15000, True, "0.0015", "0.019"),
        (ACTOR_MODEL, 15001, False, "0.0015", "0.019"),
        (JUDGE_MODEL, 7000, True, "0.003", "0.024"),
        (JUDGE_MODEL, 7001, False, "0.003", "0.024"),
    ],
)
def test_preflight_limit_rejects_before_any_paid_call(
    model: RequestedModel, estimate: int, allowed: bool, actual: str, reservation: str
) -> None:
    fake = RecordingMessages(reply(model=model), estimate=estimate)
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model))
    assert len(fake.counts) == 1
    assert len(fake.creates) == int(allowed)
    if allowed:
        assert outcome.failure is None
        assert_accounting(outcome, actual=actual, reserved=reservation, complete=True)
    else:
        assert outcome.failure is not None and outcome.failure.code == "budget_exhausted"
        assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)
        assert ledger.committed_usd == 0


@pytest.mark.parametrize("estimate", [None, True, "1000", -1])
def test_invalid_count_response_never_enters_generation(estimate: object) -> None:
    fake, ledger = RecordingMessages(estimate=estimate), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert not fake.creates
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        malformed_usage(input_tokens=1000, output_tokens=None),
        malformed_usage(input_tokens=None, output_tokens=100),
        malformed_usage(input_tokens=None, output_tokens=None),
        malformed_usage(input_tokens=True, output_tokens=100),
        malformed_usage(input_tokens="1000", output_tokens=100),
        malformed_usage(input_tokens=-1, output_tokens=100),
    ],
)
@pytest.mark.parametrize(("model", "reservation"), [(ACTOR_MODEL, "0.019"), (JUDGE_MODEL, "0.024")])
def test_missing_partial_or_invalid_usage_keeps_reservation_and_stops_batches(
    usage: object,
    model: RequestedModel,
    reservation: str,
) -> None:
    fake, ledger = RecordingMessages(reply(model=model, usage=usage)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model))
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert_accounting(outcome, actual=None, reserved=reservation, complete=False)
    assert ledger.committed_usd == Decimal(reservation)
    assert ledger.stopped
    retry = asyncio.run(perform(fake, ledger, model=model, trace_number=2))
    assert retry.failure is not None
    assert len(fake.creates) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"cache_creation_input_tokens": 1},
        {"cache_read_input_tokens": 1},
        {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 1}},
        {"server_tool_use": {"web_fetch_requests": 1, "web_search_requests": 0}},
        {"server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 1}},
        {"unrecognized_billed_tokens": 1},
    ],
)
@pytest.mark.parametrize(("model", "reservation"), [(ACTOR_MODEL, "0.019"), (JUDGE_MODEL, "0.024")])
def test_nonzero_unpriced_categories_make_entire_cost_unknown(
    extra: dict[str, Any], model: RequestedModel, reservation: str
) -> None:
    usage = SDKUsage.model_validate({"input_tokens": 1000, "output_tokens": 100, **extra})
    fake, ledger = RecordingMessages(reply(model=model, usage=usage)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model))
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert_accounting(outcome, actual=None, reserved=reservation, complete=False)
    assert ledger.stopped and ledger.reforecast_required


@pytest.mark.parametrize(
    "extra",
    [
        {"cache_creation_input_tokens": 0, "cache_read_input_tokens": None},
        {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}},
        {"server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 0}},
        {"unrecognized_billed_tokens": 0},
        {"unrecognized_billed_tokens": None},
        {"output_tokens_details": {"thinking_tokens": 100}},
        {"service_tier": "standard", "inference_geo": "us"},
    ],
)
def test_zero_categories_and_nonbilling_metadata_preserve_known_usage(
    extra: dict[str, Any],
) -> None:
    usage = SDKUsage.model_validate({"input_tokens": 1000, "output_tokens": 100, **extra})
    fake, ledger = RecordingMessages(reply(usage=usage)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is None
    assert_accounting(outcome, actual="0.0015", reserved="0.019", complete=True)
    assert not ledger.stopped and not ledger.reforecast_required


@pytest.mark.parametrize(
    ("model", "input_tokens", "overage", "cost", "reservation"),
    [
        (ACTOR_MODEL, 16000, False, "0.0165", "0.019"),
        (ACTOR_MODEL, 16001, True, "0.016501", "0.019"),
        (JUDGE_MODEL, 8000, False, "0.017", "0.024"),
        (JUDGE_MODEL, 8001, True, "0.017002", "0.024"),
    ],
)
def test_input_reservation_overage_keeps_exact_cost_and_stops_further_batches(
    model: RequestedModel, input_tokens: int, overage: bool, cost: str, reservation: str
) -> None:
    fake = RecordingMessages(
        reply(model=model, usage=SDKUsage(input_tokens=input_tokens, output_tokens=100))
    )
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model))
    assert_accounting(outcome, actual=cost, reserved=reservation, complete=True)
    assert ledger.committed_usd == Decimal(cost)
    assert ledger.stopped == overage
    assert ledger.reforecast_required == overage
    if overage:
        assert outcome.failure is not None and outcome.failure.code == "provider_error"
    else:
        assert outcome.failure is None


@pytest.mark.parametrize("usage", [_DEFAULT_USAGE, None])
def test_invalid_returned_model_id_retains_accounting_without_leaking_metadata(
    usage: object,
) -> None:
    invalid_model = "synthetic/invalid-model-id"
    fake, ledger = (
        RecordingMessages(reply(model=invalid_model, usage=usage)),
        SpendLedger(Decimal("1")),
    )
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert_accounting(
        outcome,
        actual=None,
        reserved="0.019",
        complete=False,
    )
    generation = next(
        record for record in outcome.records if record.provider_operation == "generation"
    )
    assert generation.returned_model_id is None
    assert generation.response_code == "provider_error"
    assert invalid_model not in outcome.log
    assert "ValidationError" not in outcome.log
    assert "input_value" not in outcome.log


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
@pytest.mark.parametrize("fault", ["other", "missing", "null", "invalid", "bool", "number"])
def test_unverified_returned_model_preserves_full_hold_and_raw_usage(
    model: RequestedModel, fault: str
) -> None:
    response = reply(model=model)
    other = JUDGE_MODEL if model == ACTOR_MODEL else ACTOR_MODEL
    if fault == "missing":
        del response.model
    else:
        response = response.model_copy(
            update={
                "model": {
                    "other": other,
                    "null": None,
                    "invalid": "synthetic/invalid-model-id",
                    "bool": True,
                    "number": 123,
                }[fault]
            }
        )
    fake, ledger = RecordingMessages(response), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model))
    reservation = "0.019" if model == ACTOR_MODEL else "0.024"
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert_accounting(outcome, actual=None, reserved=reservation, complete=False)
    generation = next(r for r in outcome.records if r.provider_operation == "generation")
    assert generation.returned_model_id == (other if fault == "other" else None)
    assert generation.input_tokens is None and generation.output_tokens is None
    assert response.usage.input_tokens == 1000 and response.usage.output_tokens == 100
    assert ledger.committed_usd == Decimal(reservation)
    assert ledger.stopped and ledger.reforecast_required
    subsequent = asyncio.run(perform(fake, ledger, model=model, trace_number=2))
    assert subsequent.failure is not None
    assert len(fake.counts) == len(fake.creates) == 1


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "model_context_window_exceeded"])
def test_invalid_generation_outcomes_preserve_known_cost(stop: str) -> None:
    fake, ledger = RecordingMessages(reply(stop=stop)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None and outcome.failure.code == "invalid_generated_output"
    assert len(fake.counts) == len(fake.creates) == 1
    assert_accounting(outcome, actual="0.0015", reserved="0.019", complete=True)


@pytest.mark.parametrize(
    ("model", "output", "actual", "reservation"),
    [(ACTOR_MODEL, 601, "0.004005", "0.019"), (JUDGE_MODEL, 801, "0.01001", "0.024")],
)
def test_output_above_fixed_budget_is_invalid_but_not_free(
    model: RequestedModel, output: int, actual: str, reservation: str
) -> None:
    fake, ledger = (
        RecordingMessages(
            reply(model=model, usage=SDKUsage(input_tokens=1000, output_tokens=output))
        ),
        SpendLedger(Decimal("1")),
    )
    outcome = asyncio.run(perform(fake, ledger, model=model))
    assert outcome.failure is not None and outcome.failure.code == "invalid_generated_output"
    assert_accounting(outcome, actual=actual, reserved=reservation, complete=True)


def test_native_tool_use_blocks_and_stop_reason_are_preserved() -> None:
    response = Message.model_validate(
        {
            "id": "msg_synthetic_tool",
            "type": "message",
            "role": "assistant",
            "model": ACTOR_MODEL,
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_synthetic",
                    "name": "notice_deadline_check",
                    "input": {"synthetic": "arguments"},
                }
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )
    fake, ledger = RecordingMessages(response), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is None and outcome.response is response
    assert outcome.response.stop_reason == "tool_use"
    assert outcome.response.content == response.content
    assert "arguments" not in outcome.log and "toolu_synthetic" not in outcome.log


def provider_error(kind: str) -> BaseException:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "timeout":
        return APITimeoutError(request)
    if kind == "connection":
        return APIConnectionError(message=SYNTHETIC_ERROR, request=request)
    if kind == "status":
        return APIStatusError(
            SYNTHETIC_ERROR,
            response=httpx2.Response(500, request=request),
            body={"error": SYNTHETIC_ERROR},
        )
    return RuntimeError(SYNTHETIC_ERROR)


@pytest.mark.parametrize("kind", ["timeout", "connection", "status", "unexpected"])
@pytest.mark.parametrize("operation", ["count", "create"])
def test_provider_failures_are_safe_single_attempts_with_complete_accounting(
    kind: str, operation: str
) -> None:
    failure = provider_error(kind)
    fake = RecordingMessages(
        count_error=failure if operation == "count" else None,
        create_error=failure if operation == "create" else None,
    )
    ledger = SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None
    assert outcome.failure.code == ("deadline_exceeded" if kind == "timeout" else "provider_error")
    assert str(outcome.failure) != SYNTHETIC_ERROR
    assert len(fake.counts) == 1
    assert len(fake.creates) == int(operation == "create")
    assert_accounting(
        outcome,
        actual=None if operation == "create" else "0",
        reserved="0.019" if operation == "create" else "0",
        complete=operation == "count",
        generation_count=int(operation == "create"),
    )
    assert ledger.committed_usd == (Decimal("0.019") if operation == "create" else Decimal(0))


def test_deadline_defaults_to_45_seconds_and_rejects_exact_expiry() -> None:
    clock = Clock()
    deadline = Deadline.start(clock=clock)
    clock.advance(44.999)
    deadline.check()
    clock.advance(0.001)
    with pytest.raises(ProviderFailure) as error:
        deadline.check()
    assert error.value.code == "deadline_exceeded"


def test_expired_deadline_performs_no_provider_request() -> None:
    clock, fake, ledger = Clock(), RecordingMessages(), SpendLedger(Decimal("1"))
    deadline = Deadline(expires_at=clock(), clock=clock)
    outcome = asyncio.run(perform(fake, ledger, clock=clock, deadline=deadline))
    assert outcome.failure is not None and outcome.failure.code == "deadline_exceeded"
    assert not fake.counts and not fake.creates
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)


def test_count_uses_the_shared_deadline_and_cannot_start_late_generation() -> None:
    clock, ledger = Clock(), SpendLedger(Decimal("1"))

    async def consume_deadline() -> None:
        clock.advance(45)

    fake = RecordingMessages(before_count=consume_deadline)
    outcome = asyncio.run(perform(fake, ledger, clock=clock))
    assert outcome.failure is not None and outcome.failure.code == "deadline_exceeded"
    assert len(fake.counts) == 1 and not fake.creates
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)


def test_generation_finishing_after_deadline_keeps_returned_usage() -> None:
    clock, ledger = Clock(), SpendLedger(Decimal("1"))

    async def consume_deadline() -> None:
        clock.advance(45)

    fake = RecordingMessages(before_create=consume_deadline)
    outcome = asyncio.run(perform(fake, ledger, clock=clock))
    assert outcome.failure is not None and outcome.failure.code == "deadline_exceeded"
    assert_accounting(outcome, actual="0.0015", reserved="0.019", complete=True)


def test_two_calls_share_one_absolute_deadline() -> None:
    async def scenario() -> None:
        clock, ledger = Clock(), SpendLedger(Decimal("1"))
        deadline = Deadline.start(clock=clock)

        async def count_time() -> None:
            clock.advance(10)

        async def generation_time() -> None:
            clock.advance(20)

        fake = RecordingMessages(before_count=count_time, before_create=generation_time)
        first = await perform(fake, ledger, clock=clock, deadline=deadline)
        assert first.failure is None
        second = await perform(fake, ledger, clock=clock, deadline=deadline, trace_number=2)
        assert second.failure is not None and second.failure.code == "deadline_exceeded"
        assert_accounting(second, actual="0.0015", reserved="0.019", complete=True)
        assert ledger.committed_usd == Decimal("0.003")

    asyncio.run(scenario())


def test_insufficient_budget_does_not_enter_generation() -> None:
    fake, ledger = RecordingMessages(), SpendLedger(Decimal("0.018999"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None and outcome.failure.code == "budget_exhausted"
    assert not fake.creates
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)


def test_concurrent_attempts_cannot_overreserve_shared_budget() -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_generation() -> None:
            entered.set()
            await release.wait()

        fake, ledger = (
            RecordingMessages(before_create=hold_generation),
            SpendLedger(Decimal("0.019")),
        )
        first = asyncio.create_task(perform(fake, ledger))
        await entered.wait()
        assert ledger.committed_usd == Decimal("0.019")
        second = await perform(fake, ledger, trace_number=2)
        assert second.failure is not None and second.failure.code == "budget_exhausted"
        assert len(fake.creates) == 1
        release.set()
        completed = await first
        assert completed.failure is None
        assert ledger.committed_usd == Decimal("0.0015")
        assert_accounting(completed, actual="0.0015", reserved="0.019", complete=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(("model", "reservation"), [(ACTOR_MODEL, "0.019"), (JUDGE_MODEL, "0.024")])
def test_task_cancellation_records_inflight_generation_and_leaves_no_background_call(
    model: RequestedModel, reservation: str
) -> None:
    async def scenario() -> None:
        entered, ended = asyncio.Event(), asyncio.Event()

        async def blocked_generation() -> None:
            entered.set()
            try:
                await asyncio.Future[None]()
            finally:
                ended.set()

        fake, ledger, clock = (
            RecordingMessages(before_create=blocked_generation),
            SpendLedger(Decimal("1")),
            Clock(),
        )
        trace, stream = recorder(clock)
        adapter = ProviderAdapter(fake, budget=ledger)

        async def invoke() -> None:
            with trace:
                await adapter.generate(
                    model=model,
                    system="synthetic",
                    messages=INPUT,
                    trace=trace,
                    deadline=Deadline.start(clock=clock),
                )

        task = asyncio.create_task(invoke())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and task.done()
        records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
        assert_accounting(
            Outcome(None, None, records, stream.getvalue()),
            actual=None,
            reserved=reservation,
            complete=False,
        )
        assert ledger.committed_usd == Decimal(reservation) and ledger.stopped
        assert len(fake.creates) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("tier", ["priority", "batch"])
def test_unpriced_service_tier_requires_reforecast(tier: str) -> None:
    usage = SDKUsage.model_validate(
        {"input_tokens": 1000, "output_tokens": 100, "service_tier": tier}
    )
    fake, ledger = RecordingMessages(reply(usage=usage)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is not None and outcome.failure.code == "provider_error"
    assert ledger.stopped
    assert_accounting(outcome, actual=None, reserved="0.019", complete=False)


@pytest.mark.parametrize(("model", "reservation"), [(ACTOR_MODEL, "0.019"), (JUDGE_MODEL, "0.024")])
def test_zero_usage_is_known_free_and_reservation_refund_permits_the_next_call(
    model: RequestedModel, reservation: str
) -> None:
    fake = RecordingMessages(reply(model=model, usage=SDKUsage(input_tokens=0, output_tokens=0)))
    ledger = SpendLedger(Decimal(reservation))
    first = asyncio.run(perform(fake, ledger, model=model))
    assert first.failure is None
    assert_accounting(first, actual="0", reserved=reservation, complete=True)
    assert ledger.committed_usd == 0 and not ledger.stopped
    second = asyncio.run(perform(fake, ledger, model=model, trace_number=2))
    assert second.failure is None and len(fake.creates) == 2
    assert ledger.committed_usd == 0


def test_stage_durations_include_preflight_generation_and_validation() -> None:
    clock = Clock()

    async def count_time() -> None:
        clock.advance(2)

    async def generation_time() -> None:
        clock.advance(3)

    fake, ledger = (
        RecordingMessages(before_count=count_time, before_create=generation_time),
        SpendLedger(Decimal("1")),
    )
    outcome = asyncio.run(perform(fake, ledger, clock=clock))
    assert outcome.failure is None
    endpoint = outcome.records[-1]
    assert endpoint.duration_ms == 5000
    stages = {stage.stage: stage for stage in endpoint.stage_durations}
    assert stages["preflight"].duration_ms == 2000
    assert stages["generation"].duration_ms == 3000
    assert stages["validation"].status == "completed"
    assert [record.duration_ms for record in outcome.records[:-1]] == [2000, 3000]


def test_expiry_during_validation_does_not_return_success(monkeypatch: pytest.MonkeyPatch) -> None:
    clock, ledger, fake = Clock(), SpendLedger(Decimal("1")), RecordingMessages()
    original = TypeAdapter.validate_python

    def expire_after_validation(self: Any, *args: Any, **kwargs: Any) -> Any:
        value = original(self, *args, **kwargs)
        clock.advance(45)
        return value

    monkeypatch.setattr(TypeAdapter, "validate_python", expire_after_validation)
    outcome = asyncio.run(perform(fake, ledger, clock=clock))
    assert outcome.failure is not None and outcome.failure.code == "deadline_exceeded"
    assert_accounting(outcome, actual="0.0015", reserved="0.019", complete=True)
    assert (
        next(
            stage for stage in outcome.records[-1].stage_durations if stage.stage == "validation"
        ).status
        == "failed"
    )


def test_count_cancellation_stays_free_and_does_not_launch_generation() -> None:
    async def scenario() -> None:
        entered, ended = asyncio.Event(), asyncio.Event()

        async def blocked_count() -> None:
            entered.set()
            try:
                await asyncio.Future[None]()
            finally:
                ended.set()

        fake, ledger, clock = (
            RecordingMessages(before_count=blocked_count),
            SpendLedger(Decimal("1")),
            Clock(),
        )
        trace, stream = recorder(clock)
        adapter = ProviderAdapter(fake, budget=ledger)

        async def invoke() -> None:
            with trace:
                try:
                    await adapter.generate(
                        model=ACTOR_MODEL,
                        system="synthetic",
                        messages=INPUT,
                        trace=trace,
                        deadline=Deadline.start(clock=clock),
                    )
                except asyncio.CancelledError:
                    trace.finish("deadline_exceeded")
                    raise

        task = asyncio.create_task(invoke())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and not fake.creates
        records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
        assert records[0].response_code == records[-1].response_code == "deadline_exceeded"
        assert_accounting(
            Outcome(None, None, records, stream.getvalue()),
            actual="0",
            reserved="0",
            complete=True,
            generation_count=0,
        )
        assert ledger.committed_usd == 0 and not ledger.stopped

    asyncio.run(scenario())


def test_deadline_limit_cancels_awaited_work_without_background_tasks() -> None:
    async def scenario() -> None:
        ended = False
        deadline = Deadline(
            expires_at=100.001,
            clock=Clock(),
        )
        with pytest.raises(ProviderFailure) as error:
            async with deadline.limit():
                try:
                    await asyncio.Future[None]()
                finally:
                    ended = True
        assert error.value.code == "deadline_exceeded"
        assert ended

    asyncio.run(scenario())


def test_real_sdk_serialization_uses_fixed_base_no_retry_and_no_debug_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        requests: list[httpx2.Request] = []

        def respond(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            if request.url.path.endswith("/count_tokens"):
                return httpx2.Response(200, json={"input_tokens": 1000})
            return httpx2.Response(
                200,
                json={
                    "id": "msg_synthetic_transport",
                    "type": "message",
                    "role": "assistant",
                    "model": ACTOR_MODEL,
                    "content": [{"type": "text", "text": SYNTHETIC_CONTENT}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1000, "output_tokens": 100},
                },
            )

        constructor_calls: list[dict[str, Any]] = []

        def construct(**kwargs: Any) -> AsyncAnthropic:
            constructor_calls.append(kwargs)
            return AsyncAnthropic(
                **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
            )

        monkeypatch.setattr(provider_module, "AsyncAnthropic", construct)
        monkeypatch.setattr(
            os,
            "environ",
            {"ANTHROPIC_BASE_URL": "https://synthetic.invalid", "ANTHROPIC_LOG": "debug"},
        )
        prefixes = ("anthropic", "httpx2", "httpcore2")
        for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
                monkeypatch.setattr(logging.getLogger(name), "disabled", False)
                monkeypatch.setattr(logging.getLogger(name), "level", logging.getLogger(name).level)
        caplog.set_level(logging.DEBUG)
        client = create_client(api_key="synthetic-test-key")
        try:
            assert constructor_calls == [
                {
                    "api_key": "synthetic-test-key",
                    "base_url": "https://api.anthropic.com",
                    "max_retries": 0,
                    "timeout": 45.0,
                }
            ]
            assert client.max_retries == 0
            clock = Clock()
            trace, trace_stream = recorder(clock)
            adapter = ProviderAdapter(
                cast(MessagesPort, client.messages), budget=SpendLedger(Decimal("1"))
            )
            with trace:
                await adapter.generate(
                    model=ACTOR_MODEL,
                    system="synthetic",
                    messages=INPUT,
                    trace=trace,
                    deadline=Deadline.start(clock=clock),
                    output_schema=SCHEMA,
                )
                trace.finish("ok")
            assert len(requests) == 2
            assert all(request.url.host == "api.anthropic.com" for request in requests)
            count_body, create_body = [json.loads(request.content) for request in requests]
            assert create_body["temperature"] == 0
            assert create_body["service_tier"] == "standard_only"
            assert create_body["max_tokens"] == 600 and create_body["stream"] is False
            for name in ("model", "system", "messages", "thinking", "output_config"):
                assert count_body[name] == create_body[name]
            assert "temperature" not in count_body and "max_tokens" not in count_body
            assert SYNTHETIC_CONTENT not in caplog.text
            assert "synthetic-test-key" not in caplog.text
            assert SYNTHETIC_CONTENT not in trace_stream.getvalue()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_client_rejects_custom_headers_without_constructing_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_constructor(**kwargs: Any) -> AsyncAnthropic:
        raise AssertionError("SDK constructor must not run")

    monkeypatch.setattr(provider_module, "AsyncAnthropic", forbidden_constructor)
    monkeypatch.setattr(os, "environ", {"ANTHROPIC_CUSTOM_HEADERS": "synthetic: unsafe"})
    with pytest.raises(ProviderFailure) as error:
        create_client(api_key="synthetic-test-key")
    assert error.value.code == "provider_error"
    assert "synthetic: unsafe" not in str(error.value)


@pytest.mark.parametrize("failed_operation", ["count", "generation"])
def test_real_sdk_does_not_retry_failed_mock_transport_calls(
    failed_operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def respond(request: httpx2.Request) -> httpx2.Response:
            paths.append(request.url.path)
            if failed_operation == "generation" and request.url.path.endswith("/count_tokens"):
                return httpx2.Response(200, json={"input_tokens": 1000})
            return httpx2.Response(
                500,
                json={"type": "error", "error": {"type": "api_error", "message": SYNTHETIC_ERROR}},
            )

        def construct(**kwargs: Any) -> AsyncAnthropic:
            return AsyncAnthropic(
                **kwargs,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
            )

        monkeypatch.setattr(provider_module, "AsyncAnthropic", construct)
        monkeypatch.setattr(os, "environ", {})
        prefixes = ("anthropic", "httpx2", "httpcore2")
        for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
                monkeypatch.setattr(logging.getLogger(name), "disabled", False)
                monkeypatch.setattr(logging.getLogger(name), "level", logging.getLogger(name).level)
        client = create_client(api_key="synthetic-test-key")
        try:
            clock, ledger = Clock(), SpendLedger(Decimal("1"))
            trace, stream = recorder(clock)
            adapter = ProviderAdapter(cast(MessagesPort, client.messages), budget=ledger)
            with trace:
                with pytest.raises(ProviderFailure) as error:
                    await adapter.generate(
                        model=ACTOR_MODEL,
                        system="synthetic",
                        messages=INPUT,
                        trace=trace,
                        deadline=Deadline.start(clock=clock),
                    )
                assert error.value.code == "provider_error"
                trace.finish(error.value.code)
            assert paths == (
                ["/v1/messages/count_tokens", "/v1/messages"]
                if failed_operation == "generation"
                else ["/v1/messages/count_tokens"]
            )
            records = [
                TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()
            ]
            generation = failed_operation == "generation"
            assert_accounting(
                Outcome(None, error.value, records, stream.getvalue()),
                actual=None if generation else "0",
                reserved="0.019" if generation else "0",
                complete=not generation,
                generation_count=int(generation),
            )
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("input_tokens", "output_tokens"), [(1000, None), (None, 100)])
def test_partial_usage_preserves_each_valid_known_counter(
    input_tokens: int | None, output_tokens: int | None
) -> None:
    usage = malformed_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    fake, ledger = RecordingMessages(reply(usage=usage)), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger))
    generation = next(
        record for record in outcome.records if record.provider_operation == "generation"
    )
    assert generation.input_tokens == input_tokens
    assert generation.output_tokens == output_tokens
    assert outcome.records[-1].input_tokens == input_tokens
    assert outcome.records[-1].output_tokens == output_tokens
    assert_accounting(outcome, actual=None, reserved="0.019", complete=False)


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
def test_expiry_during_reservation_refunds_undispatched_generation(model: RequestedModel) -> None:
    clock = Clock()

    class ExpiringLedger(SpendLedger):
        def reserve(self, model: RequestedModel) -> Reservation:
            reservation = super().reserve(model)
            clock.advance(46)
            return reservation

    fake, ledger = RecordingMessages(), ExpiringLedger(Decimal("1"))
    outcome = asyncio.run(perform(fake, ledger, model=model, clock=clock))
    assert outcome.failure is not None and outcome.failure.code == "deadline_exceeded"
    assert len(fake.counts) == 1
    assert not fake.creates
    assert [record.provider_operation for record in outcome.records] == ["count_tokens", None]
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)
    assert ledger.committed_usd == 0
    assert not ledger.stopped and not ledger.reforecast_required


def test_lazy_provider_loggers_cannot_emit_payload_after_client_creation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    prefix = "httpcore2.synthetic_" + uuid4().hex
    during_name, future_name = prefix + ".during.child", prefix + ".future.child"
    prefixes = ("anthropic", "httpx2", "httpcore2")
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if any(name == family or name.startswith(family + ".") for family in prefixes):
            logger = logging.getLogger(name)
            monkeypatch.setattr(logger, "disabled", False)
            monkeypatch.setattr(logger, "level", logging.NOTSET)
    created: list[logging.Logger] = []
    sentinel = "synthetic lazy logger private payload"

    def construct(**kwargs: Any) -> AsyncAnthropic:
        logger = logging.getLogger(during_name)
        monkeypatch.setattr(logger, "disabled", logger.disabled)
        monkeypatch.setattr(logger, "level", logger.level)
        created.append(logger)
        logger.debug(sentinel)
        logger.warning(sentinel)
        return cast(AsyncAnthropic, object())

    monkeypatch.setattr(provider_module, "AsyncAnthropic", construct)
    monkeypatch.setattr(os, "environ", {})
    caplog.set_level(logging.DEBUG)
    create_client(api_key="synthetic-test-key")
    assert len(created) == 1 and created[0].disabled is True
    future = logging.getLogger(future_name)
    assert future.disabled is False
    assert future.getEffectiveLevel() > logging.CRITICAL
    for logger in (*created, future):
        logger.debug(sentinel)
        logger.warning(sentinel)
        logger.critical(sentinel)
    assert sentinel not in caplog.text


async def observe_cancelled_child(
    adapter: ProviderAdapter, clock: Clock, *, model: RequestedModel = ACTOR_MODEL
) -> Outcome:
    trace, stream = recorder(clock)

    async def invoke() -> None:
        with trace:
            try:
                await adapter.generate(
                    model=model,
                    system="synthetic",
                    messages=INPUT,
                    trace=trace,
                    deadline=Deadline.start(clock=clock),
                )
            except asyncio.CancelledError:
                trace.finish("deadline_exceeded")
                raise
            else:
                trace.finish("ok")

    child = asyncio.create_task(invoke())
    with pytest.raises(asyncio.CancelledError):
        await child
    assert child.cancelled()
    log = stream.getvalue()
    return Outcome(
        None,
        None,
        [TraceRecord.model_validate_json(line) for line in log.splitlines()],
        log,
    )


def assert_no_generation_after_queued_cancellation(outcome: Outcome, ledger: SpendLedger) -> None:
    assert [record.provider_operation for record in outcome.records] == ["count_tokens", None]
    assert outcome.records[-1].response_code == "deadline_exceeded"
    assert_accounting(outcome, actual="0", reserved="0", complete=True, generation_count=0)
    assert ledger.committed_usd == 0
    assert not ledger.stopped and not ledger.reforecast_required


def test_queued_cancellation_after_count_return_never_dispatches_generation() -> None:
    class CancellingMessages(RecordingMessages):
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            count = await super().count_tokens(**kwargs)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            return count

    async def scenario() -> None:
        fake, ledger, clock = CancellingMessages(), SpendLedger(Decimal("1")), Clock()
        outcome = await observe_cancelled_child(ProviderAdapter(fake, budget=ledger), clock)
        assert len(fake.counts) == 1
        assert not fake.creates
        assert_no_generation_after_queued_cancellation(outcome, ledger)

    asyncio.run(scenario())


def test_queued_cancellation_after_sdk_count_return_never_sends_messages_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def respond(request: httpx2.Request) -> httpx2.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/count_tokens"):
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                return httpx2.Response(200, json={"input_tokens": 1000})
            return httpx2.Response(
                200,
                json={
                    "id": "msg_synthetic_cancel_boundary",
                    "type": "message",
                    "role": "assistant",
                    "model": ACTOR_MODEL,
                    "content": [{"type": "text", "text": SYNTHETIC_CONTENT}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1000, "output_tokens": 100},
                },
            )

        monkeypatch.setattr(os, "environ", {})
        client = AsyncAnthropic(
            api_key="synthetic-test-key",
            base_url="https://api.anthropic.com",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        )
        try:
            ledger, clock = SpendLedger(Decimal("1")), Clock()
            adapter = ProviderAdapter(cast(MessagesPort, client.messages), budget=ledger)
            outcome = await observe_cancelled_child(adapter, clock)
            assert paths == ["/v1/messages/count_tokens"]
            assert_no_generation_after_queued_cancellation(outcome, ledger)
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
def test_queued_cancellation_during_reservation_refunds_without_generation(
    model: RequestedModel,
) -> None:
    class CancellingLedger(SpendLedger):
        def reserve(self, model: RequestedModel) -> Reservation:
            ticket = super().reserve(model)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            return ticket

    async def scenario() -> None:
        fake, ledger, clock = RecordingMessages(), CancellingLedger(Decimal("1")), Clock()
        outcome = await observe_cancelled_child(
            ProviderAdapter(fake, budget=ledger), clock, model=model
        )
        assert len(fake.counts) == 1
        assert not fake.creates
        assert_no_generation_after_queued_cancellation(outcome, ledger)

    asyncio.run(scenario())


def test_shared_ledger_freeze_refunds_already_reserved_undispatched_call() -> None:
    async def scenario() -> None:
        reservations: list[Reservation] = []

        class RecordingLedger(SpendLedger):
            def reserve(self, model: RequestedModel) -> Reservation:
                ticket = super().reserve(model)
                reservations.append(ticket)
                return ticket

        ledger = RecordingLedger(Decimal("0.038"))

        async def verify_both_reserved_before_first_dispatch() -> None:
            assert len(reservations) == 2
            assert ledger.committed_usd == Decimal("0.038")
            assert not ledger.stopped

        first_fake = RecordingMessages(
            reply(usage=None), before_create=verify_both_reserved_before_first_dispatch
        )
        second_fake = RecordingMessages()
        # Both tasks reach the dispatch checkpoint before the first task resumes.
        first_task = asyncio.create_task(perform(first_fake, ledger, trace_number=1))
        second_task = asyncio.create_task(perform(second_fake, ledger, trace_number=2))
        first, second = await asyncio.gather(first_task, second_task)
        assert len(reservations) == 2
        assert len(first_fake.creates) == 1
        assert first.failure is not None and first.failure.code == "provider_error"
        assert_accounting(first, actual=None, reserved="0.019", complete=False)
        assert second.failure is not None and second.failure.code == "provider_error"
        assert len(second_fake.counts) == 1
        assert not second_fake.creates
        assert [record.provider_operation for record in second.records] == ["count_tokens", None]
        assert second.records[-1].response_code == "provider_error"
        assert_accounting(second, actual="0", reserved="0", complete=True, generation_count=0)
        assert ledger.committed_usd == Decimal("0.019")
        assert ledger.stopped and ledger.reforecast_required

    asyncio.run(scenario())


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
@pytest.mark.parametrize(
    "field",
    [
        "preflight_limit",
        "reserved_input_tokens",
        "max_output_tokens",
        "input_per_million",
        "output_per_million",
    ],
)
def test_provider_configuration_identity_covers_each_models_complete_policy(
    model: RequestedModel, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = provider_module.configuration_hash(system="fixed synthetic system")
    amended = policy_manifest()
    old_value = amended[model][field]
    amended[model][field] = (
        old_value + 1 if isinstance(old_value, int) else str(Decimal(old_value) + 1)
    )
    monkeypatch.setattr(provider_module, "policy_manifest", lambda: amended)
    changed = provider_module.configuration_hash(system="fixed synthetic system")
    assert changed != original
    assert changed == provider_module.configuration_hash(system="fixed synthetic system")


def test_retained_second_request_estimate_is_admitted_by_uniform_actor_policy() -> None:
    fake = RecordingMessages(
        reply(usage=SDKUsage(input_tokens=12289, output_tokens=100)), estimate=12289
    )
    ledger = SpendLedger(Decimal("0.019"))
    outcome = asyncio.run(perform(fake, ledger))
    assert outcome.failure is None
    assert len(fake.counts) == len(fake.creates) == 1
    assert fake.creates[0]["max_tokens"] == 600
    assert_accounting(outcome, actual="0.012789", reserved="0.019", complete=True)
    assert ledger.committed_usd == Decimal("0.012789")
    assert not ledger.stopped
