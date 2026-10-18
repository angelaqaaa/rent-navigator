"""Synthetic judge capacity and one shared deadline through final publication."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from anthropic.types import Message
from anthropic.types import Usage as SDKUsage
from test_eval_judge import RUN_ID, harness, message

import rent_navigator.eval.judge as judge_module
from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    JudgeRecorder,
    build_judge_packet,
    build_smoke_input,
)
from rent_navigator.eval.runner import (
    JudgeAccounting,
    JudgeEvaluation,
    JudgeFailure,
    JudgeInput,
    validate_judge_accounting,
)
from rent_navigator.models import ErrorCode
from rent_navigator.provider import Deadline, Reservation, SpendLedger
from rent_navigator.trace import TraceRecord, TraceRecorder, provider_cost_totals


@pytest.fixture
def value() -> JudgeInput:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    return build_smoke_input(case, corpus)


INVALID_DURATIONS = [
    True,
    False,
    "180",
    None,
    float("nan"),
    float("inf"),
    float("-inf"),
    0,
    -1,
    300.00000000000006,
    10**10000,
    -(10**10000),
]
INVALID_DURATION_IDS = [
    "true",
    "false",
    "string",
    "null",
    "nan",
    "positive-infinity",
    "negative-infinity",
    "zero",
    "negative",
    "over300",
    "huge-positive-int",
    "huge-negative-int",
]


@pytest.mark.parametrize("duration", INVALID_DURATIONS, ids=INVALID_DURATION_IDS)
def test_explicit_duration_rejects_before_clock_or_numeric_conversion(duration: object) -> None:
    def forbidden_clock() -> float:
        raise AssertionError("Invalid duration must be rejected before reading the clock")

    with pytest.raises(ValueError):
        Deadline.start(clock=forbidden_clock, duration_seconds=cast(Any, duration))


@pytest.mark.parametrize("duration", [1, 45.0, 180, 180.0, 300, 300.0])
def test_explicit_duration_numeric_boundaries(duration: float) -> None:
    deadline = Deadline.start(clock=lambda: 10.0, duration_seconds=duration)
    assert deadline.expires_at == 10.0 + duration
    assert deadline.remaining() == duration
    assert Deadline.start(clock=lambda: 10.0).remaining() == 45.0


@pytest.mark.parametrize("timeout", INVALID_DURATIONS, ids=INVALID_DURATION_IDS)
@pytest.mark.parametrize("operation", ["count_tokens", "generation"])
def test_preparation_and_public_replay_reject_invalid_dynamic_timeout(
    value: JudgeInput, timeout: object, operation: str
) -> None:
    test = harness(value)
    evaluated = test.run()
    recorder = JudgeRecorder(
        test.port,
        packet=build_judge_packet(value),
        context=test.context,
        run_id=RUN_ID,
        stream=test.raw,
    )
    payload = deepcopy(test.port.counts[0] if operation == "count_tokens" else test.port.creates[0])
    payload["timeout"] = timeout
    with pytest.raises(ValueError):
        recorder.prepared(payload, operation)
    records = test.records()
    index = 0 if operation == "count_tokens" else 2
    records[index] = records[index].model_copy(update={"value": payload})
    # Raw record validation and prepared reconstruction are both genuine replay stages.
    with pytest.raises(ValueError):
        judge_module.verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluated,
            judgment=evaluated.judgment,
            run_id=RUN_ID,
        )


def test_exact300_is_valid_for_preparation_and_replay(value: JudgeInput) -> None:
    test = harness(value)
    evaluated = test.run()
    records = test.records()
    for index in (0, 2):
        payload = deepcopy(records[index].value)
        payload["timeout"] = 300
        records[index] = records[index].model_copy(update={"value": payload})
    judge_module.verify_judge_records(
        records,
        value=value,
        context=test.context,
        accounting=evaluated,
        judgment=evaluated.judgment,
        run_id=RUN_ID,
    )


@pytest.mark.parametrize(
    "input_tokens,output_tokens,code,actual",
    [
        (25000, 16384, None, "0.21384"),
        (25001, 16384, "provider_error", "0.213842"),
        (25000, 16385, "invalid_generated_output", "0.213850"),
    ],
)
def test_judge_input_output_cost_boundaries_preserve_known_usage(
    value: JudgeInput,
    input_tokens: int,
    output_tokens: int,
    code: ErrorCode | None,
    actual: str,
) -> None:
    test = harness(value)
    test.port.response = message(
        value, usage=SDKUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    )
    if code is None:
        evaluated = test.run()
        accounting: JudgeAccounting = evaluated
        test.verify(evaluated, evaluated.judgment)
    else:
        with pytest.raises(JudgeFailure) as caught:
            test.run()
        assert caught.value.code == code
        accounting = caught.value.accounting
        test.verify(accounting)
    assert accounting.cost.actual_cost_usd == test.budget.committed_usd == Decimal(actual)
    assert accounting.cost.reserved_cost_usd == Decimal("0.21384")
    assert accounting.cost.usage_complete
    assert len(test.port.counts) == len(test.port.creates) == 1
    if input_tokens > 25000:
        assert test.budget.stopped


def test_judge_progress_beyond45_uses_decreasing_same300_budget(value: JudgeInput) -> None:
    test = harness(value)
    test.port.count_delay, test.port.generation_delay = 55.0, 65.0
    evaluated = test.run()
    assert test.clock.now > 10.0 + 45.0
    assert test.port.counts[0]["timeout"] == 300.0
    assert test.port.creates[0]["timeout"] == 245.0
    assert evaluated.records[-1].response_code == "ok"
    test.verify(evaluated, evaluated.judgment)


def test_k2_judge_can_use_one300_second_budget_beyond180(value: JudgeInput) -> None:
    test = harness(value)
    test.port.count_delay, test.port.generation_delay = 150.0, 149.0
    evaluated = test.run()
    assert test.port.counts[0]["timeout"] == 300.0
    assert test.port.creates[0]["timeout"] == 150.0
    assert evaluated.records[-1].response_code == "ok"
    assert evaluated.cost.actual_cost_usd == Decimal("0.003")
    assert evaluated.cost.reserved_cost_usd == Decimal("0.21384")
    assert Deadline.start(clock=lambda: 0.0).remaining() == 45.0
    test.verify(evaluated, evaluated.judgment)


def test_individually_below300_stages_cannot_refresh_combined_deadline(value: JudgeInput) -> None:
    test = harness(value)
    test.port.count_delay, test.port.generation_delay = 100.0, 201.0
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert test.port.creates[0]["timeout"] == 200.0
    assert caught.value.accounting.cost.actual_cost_usd == Decimal("0.003")
    test.verify(caught.value.accounting)


@pytest.mark.parametrize("assembly", ["packet", "schema"])
def test_assembly_uses_shared_deadline_before_dispatch(
    value: JudgeInput, assembly: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)
    name = "build_judge_packet" if assembly == "packet" else "judge_schema"
    original = getattr(judge_module, name)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        test.clock.now += 300.0
        return result

    monkeypatch.setattr(judge_module, name, delayed)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert not test.port.counts and not test.port.creates
    assert not test.raw.getvalue()
    assert caught.value.accounting.cost.actual_cost_usd == 0
    assert caught.value.accounting.records[-1].response_code == "deadline_exceeded"


def test_budget_admission_expiry_refunds_undispatched_generation(value: JudgeInput) -> None:
    test = harness(value)

    class DelayedBudget(SpendLedger):
        def reserve(self, model: Any) -> Reservation:
            reservation = super().reserve(model)
            test.clock.now += 300.0
            return reservation

    test.budget = DelayedBudget(Decimal("1"))
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert len(test.port.counts) == 1 and not test.port.creates
    assert caught.value.accounting.cost.actual_cost_usd == test.budget.committed_usd == 0
    assert caught.value.accounting.cost.reserved_cost_usd == 0
    test.verify(caught.value.accounting)


@pytest.mark.parametrize("boundary", ["accounting", "evaluation"])
def test_final_success_validation_expiry_publishes_one_failure_endpoint_and_no_judgment(
    value: JudgeInput, boundary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)
    observed_candidates: list[str] = []
    finishes: list[str] = []
    original_finish = TraceRecorder.finish

    def counted_finish(self: TraceRecorder, code: Any, **kwargs: Any) -> TraceRecord:
        finishes.append(code)
        return original_finish(self, code, **kwargs)

    monkeypatch.setattr(TraceRecorder, "finish", counted_finish)

    def verify_unpublished() -> None:
        published = [
            TraceRecord.model_validate_json(line) for line in test.metadata.getvalue().splitlines()
        ]
        assert len(published) == 2
        assert all(record.record_kind == "provider_call" for record in published)
        assert len(test.records()) == 4
        assert test.records()[-1].value["content"][0]["text"]

    if boundary == "accounting":
        original_accounting = validate_judge_accounting

        def delayed_accounting(accounting: JudgeAccounting, context: Any) -> None:
            verify_unpublished()
            endpoint = [record for record in accounting.records if record.record_kind == "endpoint"]
            assert len(endpoint) == 1
            observed_candidates.append(endpoint[0].response_code)
            original_accounting(accounting, context)
            if endpoint[0].response_code == "ok":
                test.clock.now += 300.0

        monkeypatch.setattr(judge_module, "validate_judge_accounting", delayed_accounting)
    else:
        original_evaluation = JudgeEvaluation

        def delayed_evaluation(**kwargs: Any) -> JudgeEvaluation:
            verify_unpublished()
            evaluated = original_evaluation(**kwargs)
            observed_candidates.append(evaluated.records[-1].response_code)
            test.clock.now += 300.0
            return evaluated

        monkeypatch.setattr(judge_module, "JudgeEvaluation", delayed_evaluation)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert not hasattr(caught.value, "judgment")
    assert finishes == ["ok"]
    assert observed_candidates[0] == "ok"
    if boundary == "accounting":
        assert observed_candidates == ["ok", "deadline_exceeded"]
    metadata = [
        TraceRecord.model_validate_json(line) for line in test.metadata.getvalue().splitlines()
    ]
    endpoints = [record for record in metadata if record.record_kind == "endpoint"]
    assert len(metadata) == 3 and len(endpoints) == 1
    assert endpoints[0].response_code == "deadline_exceeded"
    assert (
        endpoints[0].duration_ms < 300000
    )  # Captured before final validation, not its total duration.
    assert all(record.response_code == "ok" for record in metadata[:2])
    accounting = caught.value.accounting
    assert accounting.cost == provider_cost_totals(metadata)
    assert accounting.records == metadata
    assert accounting.cost.actual_cost_usd == test.budget.committed_usd == Decimal("0.003")
    assert accounting.cost.reserved_cost_usd == Decimal("0.21384")
    # Replay without instrumentation proves retained raw/trace linkage.
    monkeypatch.undo()
    test.verify(accounting)


@pytest.mark.parametrize("boundary", ["success", "accounting", "evaluation"])
def test_buffered_metadata_is_visible_at_each_publication_and_stable_before_close(
    value: JudgeInput, boundary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)
    metadata_path = tmp_path / "synthetic-judge-metadata.jsonl"
    finishes: list[str] = []
    candidates: list[str] = []
    original_finish = TraceRecorder.finish
    original_create = test.port.create
    original_accounting = validate_judge_accounting
    original_evaluation = JudgeEvaluation

    def published_records() -> list[TraceRecord]:
        return [
            TraceRecord.model_validate_json(line)
            for line in metadata_path.read_text(encoding="utf-8").splitlines()
        ]

    def verify_calls_visible_without_endpoint() -> None:
        published = published_records()
        assert [record.provider_operation for record in published] == [
            "count_tokens",
            "generation",
        ]
        assert all(record.record_kind == "provider_call" for record in published)

    def counted_finish(self: TraceRecorder, code: Any, **kwargs: Any) -> TraceRecord:
        finishes.append(code)
        return original_finish(self, code, **kwargs)

    async def inspect_before_generation(**kwargs: Any) -> Message:
        published = published_records()
        assert len(published) == 1
        assert published[0].record_kind == "provider_call"
        assert published[0].provider_operation == "count_tokens"
        return await original_create(**kwargs)

    def inspect_accounting(accounting: JudgeAccounting, context: Any) -> None:
        verify_calls_visible_without_endpoint()
        endpoint = [record for record in accounting.records if record.record_kind == "endpoint"]
        assert len(endpoint) == 1
        candidates.append(endpoint[0].response_code)
        original_accounting(accounting, context)
        if boundary == "accounting" and endpoint[0].response_code == "ok":
            test.clock.now += 300.0

    def inspect_evaluation(**kwargs: Any) -> JudgeEvaluation:
        verify_calls_visible_without_endpoint()
        evaluated = original_evaluation(**kwargs)
        if boundary == "evaluation":
            test.clock.now += 300.0
        return evaluated

    monkeypatch.setattr(TraceRecorder, "finish", counted_finish)
    monkeypatch.setattr(test.port, "create", inspect_before_generation)
    monkeypatch.setattr(judge_module, "validate_judge_accounting", inspect_accounting)
    monkeypatch.setattr(judge_module, "JudgeEvaluation", inspect_evaluation)
    # The buffer is deliberately larger than the complete synthetic metadata output.
    with metadata_path.open("w", encoding="utf-8", buffering=1024 * 1024) as metadata:
        judge = judge_module.RealJudge(
            test.port,
            budget=test.budget,
            metadata=metadata,
            raw_provider=test.raw,
            run_id=RUN_ID,
            clock=test.clock,
        )
        accounting: JudgeAccounting
        if boundary == "success":
            evaluated = asyncio.run(judge.evaluate(value, context=test.context))
            accounting = evaluated
            assert candidates == ["ok"]
        else:
            with pytest.raises(JudgeFailure) as caught:
                asyncio.run(judge.evaluate(value, context=test.context))
            assert caught.value.code == "deadline_exceeded"
            assert not hasattr(caught.value, "judgment")
            accounting = caught.value.accounting
            assert candidates == ["ok", "deadline_exceeded"]
        assert not metadata.closed
        published = published_records()
        assert published == accounting.records
        assert len(published) == 3
        assert [record.record_kind for record in published] == [
            "provider_call",
            "provider_call",
            "endpoint",
        ]
        assert published[-1].response_code == (
            "ok" if boundary == "success" else "deadline_exceeded"
        )
        assert finishes == ["ok"]
        assert accounting.cost == provider_cost_totals(published)
        before_close = metadata_path.read_bytes()
        before_close_hash = sha256(before_close).hexdigest()
    assert metadata_path.read_bytes() == before_close
    assert sha256(metadata_path.read_bytes()).hexdigest() == before_close_hash
    monkeypatch.undo()
    test.verify(accounting, evaluated.judgment if boundary == "success" else None)


def test_cancellation_retains_unknown_usage_and_finishes_without_background_task(
    value: JudgeInput,
) -> None:
    test = harness(value)
    test.port.response = asyncio.CancelledError()
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.accounting.cost.actual_cost_usd is None
    assert caught.value.accounting.cost.reserved_cost_usd == Decimal("0.21384")
    assert test.budget.stopped and test.budget.committed_usd == Decimal("0.21384")
    assert len(test.port.counts) == len(test.port.creates) == 1
    assert (
        len(
            [
                record
                for record in caught.value.accounting.records
                if record.record_kind == "endpoint"
            ]
        )
        == 1
    )
    test.verify(caught.value.accounting)


def test_pending_judge_generation_cancels_without_leaving_a_task(
    value: JudgeInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)

    async def scenario() -> JudgeAccounting:
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def pending_create(**kwargs: Any) -> Message:
            test.port.creates.append(deepcopy(kwargs))
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("A cancelled generation cannot return a judgment")

        monkeypatch.setattr(test.port, "create", pending_create)
        task = asyncio.create_task(
            judge_module.RealJudge(
                test.port,
                budget=test.budget,
                metadata=test.metadata,
                raw_provider=test.raw,
                run_id=RUN_ID,
                clock=test.clock,
            ).evaluate(value, context=test.context)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(JudgeFailure) as caught:
            await task
        assert caught.value.code == "deadline_exceeded"
        assert cancelled.is_set() and task.done()
        assert asyncio.all_tasks() == {asyncio.current_task()}
        return caught.value.accounting

    accounting = asyncio.run(scenario())
    assert accounting.cost.actual_cost_usd is None and not accounting.cost.usage_complete
    assert accounting.cost.reserved_cost_usd == test.budget.committed_usd == Decimal("0.21384")
    assert test.budget.stopped
    assert len(test.port.counts) == len(test.port.creates) == 1
    test.verify(accounting)
