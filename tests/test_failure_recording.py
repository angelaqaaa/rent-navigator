"""Typed failure evidence across serving and judge paths, without external requests."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types import Message, MessageTokensCount
from eval_fixtures import TOY_HASH, TOY_RUN_ID, TOY_SOURCE_SHA
from pydantic import ValidationError
from test_eval_judge import RUN_ID
from test_eval_judge import harness as judge_harness
from test_eval_runner import _harness as serving_harness

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.evidence import verify_raw_links
from rent_navigator.eval.judge import (
    JudgeRecorder,
    RawJudgeRecord,
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    verify_judge_records,
)
from rent_navigator.eval.recording import (
    RawProviderRecord,
    SyntheticAllowlist,
    SyntheticRecorder,
    verify_synthetic_records,
)
from rent_navigator.eval.runner import (
    AttemptIdentity,
    JudgeFailure,
    PlanEntry,
    run_attempt,
)
from rent_navigator.models import ErrorResponse
from rent_navigator.provider import FailureDiagnostic, MessagesPort, ProviderFailure
from rent_navigator.trace import CostSummary, TraceRecord, provider_cost_totals

Surface = Literal["serving", "judge"]
Operation = Literal["count_tokens", "generation"]
Raw = RawProviderRecord | RawJudgeRecord
SENTINEL = "synthetic-sensitive-message-body-header-request-value"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@dataclass
class Captured:
    raw: list[Raw]
    metadata: list[TraceRecord]
    text: str
    cost: CostSummary
    code: str
    stopped: bool
    committed: Decimal
    replay: Callable[[list[Raw]], None]


async def capture(surface: Surface, corpus: Corpus, port: MessagesPort) -> Captured:
    if surface == "judge":
        case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
        value = build_smoke_input(case, corpus)
        judge = judge_harness(value)
        with pytest.raises(JudgeFailure) as caught:
            await RealJudge(
                port,
                budget=judge.budget,
                metadata=judge.metadata,
                raw_provider=judge.raw,
                run_id=RUN_ID,
                clock=judge.clock,
            ).evaluate(value, context=judge.context)
        accounting = caught.value.accounting

        def replay_judge(raw: list[Raw]) -> None:
            verify_judge_records(
                cast(list[RawJudgeRecord], raw),
                value=value,
                context=judge.context,
                accounting=accounting,
                run_id=RUN_ID,
            )

        return Captured(
            list(judge.records()),
            accounting.records,
            judge.raw.getvalue() + judge.metadata.getvalue(),
            accounting.cost,
            caught.value.code,
            judge.budget.stopped,
            judge.budget.committed_usd,
            replay_judge,
        )
    serving = serving_harness(corpus, arm="baseline")
    outcome = await run_attempt(
        serving.case,
        PlanEntry(case_id=serving.case.id, arm=serving.arm, repeat=0),
        identity=AttemptIdentity(run_id=UUID(TOY_RUN_ID), source_sha=TOY_SOURCE_SHA, gold_hash=TOY_HASH),
        corpus=corpus,
        messages=port,
        budget=serving.budget,
        allowlist=SyntheticAllowlist.from_fixtures([serving.case]),
        retrieve=serving.retrieve,
        metadata=serving.metadata,
        raw_provider=serving.raw,
        clock=serving.clock,
    )
    assert isinstance(outcome.row.response, ErrorResponse)

    def replay_serving(raw: list[Raw]) -> None:
        serving_raw = cast(list[RawProviderRecord], raw)
        verify_synthetic_records(serving_raw, case=serving.case, corpus=corpus, arm=serving.arm)
        verify_raw_links(serving_raw, list(outcome.records), outcome.row.run_id)

    return Captured(
        [
            RawProviderRecord.model_validate_json(line)
            for line in serving.raw.getvalue().splitlines()
        ],
        list(outcome.records),
        serving.raw.getvalue() + serving.metadata.getvalue() + outcome.row.model_dump_json(),
        provider_cost_totals(outcome.records),
        outcome.row.response.error.code,
        serving.budget.stopped,
        serving.budget.committed_usd,
        replay_serving,
    )


def assert_capture(
    evidence: Captured, surface: Surface, operation: Operation, category: str, status: int | None
) -> None:
    code = (
        "deadline_exceeded"
        if category in {"sdk_timeout", "local_timeout", "cancelled"}
        else "provider_error"
    )
    assert evidence.raw[-1].event == "failure"
    assert evidence.raw[-1].value == {
        "code": code,
        "diagnostic": {"schema_version": 1, "category": category, "http_status": status},
    }
    assert evidence.code == evidence.metadata[-1].response_code == code
    assert evidence.metadata[-2].response_code == code
    assert SENTINEL not in evidence.text
    assert evidence.raw[-1].operation == operation
    assert [record.event for record in evidence.raw] == (
        ["request", "failure"]
        if operation == "count_tokens"
        else ["request", "response", "request", "failure"]
    )
    if operation == "count_tokens":
        assert evidence.cost.actual_cost_usd == evidence.committed == 0
        assert evidence.cost.usage_complete and evidence.cost.reserved_cost_usd == 0
        assert not evidence.stopped
    else:
        assert evidence.cost.actual_cost_usd is None and not evidence.cost.usage_complete
        assert (
            evidence.cost.reserved_cost_usd
            == evidence.committed
            == (Decimal(".082") if surface == "judge" else Decimal(".027"))
        )
        assert evidence.stopped
    evidence.replay(evidence.raw)


@pytest.mark.parametrize("surface", ["serving", "judge"])
@pytest.mark.parametrize("operation", ["count_tokens", "generation"])
@pytest.mark.parametrize("mode", [400, 429, 500, 529, "timeout", "connection", "validation"])
def test_locked_sdk_failures_retain_only_typed_diagnostic(
    corpus: Corpus, surface: Surface, operation: Operation, mode: int | str
) -> None:
    paths: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if operation == "generation" and request.url.path.endswith("count_tokens"):
            return httpx2.Response(200, json={"input_tokens": 1000})
        if mode == "timeout":
            raise httpx2.ReadTimeout(SENTINEL, request=request)
        if mode == "connection":
            raise httpx2.ConnectError(SENTINEL, request=request)
        if mode == "validation":
            return httpx2.Response(200, json={"sensitive": SENTINEL})
        return httpx2.Response(
            int(mode),
            json={"type": "error", "error": {"type": "synthetic", "message": SENTINEL}},
            headers={"x-request-id": SENTINEL, "x-sensitive": SENTINEL},
        )

    async def run() -> Captured:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as transport:
            async with AsyncAnthropic(
                api_key="synthetic-test-only",
                http_client=transport,
                max_retries=0,
                _strict_response_validation=True,
            ) as client:
                return await capture(surface, corpus, cast(MessagesPort, client.messages))

    evidence = asyncio.run(run())
    expected_category = {
        "timeout": "sdk_timeout",
        "connection": "sdk_connection",
        "validation": "sdk_response_validation",
    }.get(str(mode), "http_error")
    assert_capture(
        evidence,
        surface,
        operation,
        expected_category,
        mode if isinstance(mode, int) else 200 if mode == "validation" else None,
    )
    assert paths == (
        ["/v1/messages/count_tokens"]
        if operation == "count_tokens"
        else ["/v1/messages/count_tokens", "/v1/messages"]
    )


@pytest.mark.parametrize("surface", ["serving", "judge"])
@pytest.mark.parametrize("operation", ["count_tokens", "generation"])
@pytest.mark.parametrize(
    "category", ["local_timeout", "cancelled", "local_guard", "unknown", "http_error"]
)
def test_local_and_normalized_failures_preserve_diagnostic(
    corpus: Corpus, surface: Surface, operation: Operation, category: str
) -> None:
    error: BaseException
    if category == "local_timeout":
        error = TimeoutError(SENTINEL)
    elif category == "cancelled":
        error = asyncio.CancelledError(SENTINEL)
    elif category == "local_guard":
        error = ProviderFailure("provider_error")
    elif category == "http_error":
        error = ProviderFailure(
            "provider_error",
            diagnostic=FailureDiagnostic(schema_version=1, category="http_error", http_status=429),
        )
    else:
        error = RuntimeError(SENTINEL)
    fake = serving_harness(corpus).fake
    if operation == "count_tokens":
        fake.estimates[0] = error
    else:
        fake.responses[0] = error
    evidence = asyncio.run(capture(surface, corpus, fake))
    assert_capture(
        evidence, surface, operation, category, 429 if category == "http_error" else None
    )


@pytest.mark.parametrize("surface", ["serving", "judge"])
@pytest.mark.parametrize(
    "mutation",
    [
        "code_only",
        "extra_envelope",
        "missing_category",
        "extra_diagnostic",
        "bool_version",
        "wrong_version",
        "bool_status",
        "string_status",
        "bad_status",
        "unknown_category",
        "code_mismatch",
        "metadata_mismatch",
    ],
)
def test_failure_replay_rejects_code_only_and_tampered_envelopes(
    corpus: Corpus, surface: Surface, mutation: str
) -> None:
    fake = serving_harness(corpus).fake
    fake.responses[0] = RuntimeError(SENTINEL)
    evidence = asyncio.run(capture(surface, corpus, fake))
    terminal = evidence.raw[-1]
    value = json.loads(json.dumps(terminal.value))
    diagnostic = value["diagnostic"]
    if mutation == "code_only":
        del value["diagnostic"]
    elif mutation == "extra_envelope":
        value["body"] = SENTINEL
    elif mutation == "missing_category":
        del diagnostic["category"]
    elif mutation == "extra_diagnostic":
        diagnostic["body"] = SENTINEL
    elif mutation == "bool_version":
        diagnostic["schema_version"] = True
    elif mutation == "wrong_version":
        diagnostic["schema_version"] = 2
    elif mutation == "bool_status":
        diagnostic.update(category="http_error", http_status=True)
    elif mutation == "string_status":
        diagnostic.update(category="http_error", http_status="429")
    elif mutation == "bad_status":
        diagnostic.update(category="http_error", http_status=200)
    elif mutation == "unknown_category":
        diagnostic["category"] = SENTINEL
    elif mutation == "code_mismatch":
        value["code"] = "deadline_exceeded"
    else:
        value["code"] = "deadline_exceeded"
        diagnostic["category"] = "local_timeout"
    candidate = terminal.model_copy(update={"value": value})
    if mutation != "metadata_mismatch":
        with pytest.raises(ValidationError):
            type(terminal).model_validate_json(candidate.model_dump_json())
    tampered = [*evidence.raw[:-1], candidate]
    with pytest.raises(ValueError):
        evidence.replay(tampered)
    if surface == "serving":
        with pytest.raises(ValueError):
            verify_raw_links(
                cast(list[RawProviderRecord], tampered), evidence.metadata, terminal.run_id
            )


class DirectPort:
    def __init__(self, operation: Operation, outcome: BaseException | object) -> None:
        self.operation, self.outcome = operation, outcome
        self.calls: list[str] = []

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.calls.append("count_tokens")
        if self.operation == "count_tokens":
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return cast(MessageTokensCount, self.outcome)
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append("generation")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return cast(Message, self.outcome)


def direct_recorder(
    surface: Surface, corpus: Corpus, port: MessagesPort
) -> tuple[MessagesPort, dict[str, Any], dict[str, Any], StringIO]:
    stream = StringIO()
    if surface == "judge":
        case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
        value = build_smoke_input(case, corpus)
        judge = judge_harness(value)
        judge.run()
        return (
            JudgeRecorder(
                port,
                packet=build_judge_packet(value),
                context=judge.context,
                run_id=RUN_ID,
                stream=stream,
            ),
            judge.port.counts[0],
            judge.port.creates[0],
            stream,
        )
    serving = serving_harness(corpus, arm="baseline")
    serving.run()
    recorder = SyntheticRecorder(
        port,
        case=serving.case,
        allowlist=SyntheticAllowlist.from_fixtures([serving.case]),
        corpus=corpus,
        stream=stream,
        run_id=UUID(TOY_RUN_ID),
        attempt_id=serving.case.request.attempt_id,
    )
    recorder.bind(uuid4(), "analysis", serving.case.request, arm="baseline")
    return recorder, serving.fake.counts[0], serving.fake.creates[0], stream


@pytest.mark.parametrize("surface", ["serving", "judge"])
@pytest.mark.parametrize("operation", ["count_tokens", "generation"])
@pytest.mark.parametrize("failure_location", ["dispatch", "response", "prepared"])
def test_recorder_normalization_does_not_retain_exception_context(
    corpus: Corpus, surface: Surface, operation: Operation, failure_location: str
) -> None:
    request = httpx2.Request("POST", "https://synthetic.invalid", headers={"sensitive": SENTINEL})
    error = APIStatusError(
        SENTINEL,
        response=httpx2.Response(429, request=request, headers={"sensitive": SENTINEL}),
        body={"sensitive": SENTINEL},
    )
    port = DirectPort(operation, error if failure_location == "dispatch" else object())
    recorder, count_payload, generation_payload, stream = direct_recorder(surface, corpus, port)
    payload = dict(count_payload if operation == "count_tokens" else generation_payload)
    if failure_location == "prepared":
        payload["unapproved_sensitive_field"] = SENTINEL

    async def run() -> ProviderFailure:
        if operation == "generation":
            await recorder.count_tokens(**count_payload)
        # This simulates a caller already handling another exception. The safe failure
        # must not inherit that exception through Python's implicit context chain.
        try:
            raise RuntimeError(SENTINEL)
        except RuntimeError:
            with pytest.raises(ProviderFailure) as caught:
                if operation == "count_tokens":
                    await recorder.count_tokens(**payload)
                else:
                    await recorder.create(**payload)
        return caught.value

    failure = asyncio.run(run())
    assert failure.__context__ is None and failure.__cause__ is None
    assert failure.code == "provider_error"
    assert failure.diagnostic.category == (
        "http_error" if failure_location == "dispatch" else "local_guard"
    )
    assert SENTINEL not in str(failure) + stream.getvalue()
    assert not any(isinstance(value, BaseException) for value in vars(failure).values())
    expected_calls = ["count_tokens"] if operation == "generation" else []
    if failure_location != "prepared":
        expected_calls.append(operation)
    assert port.calls == expected_calls


@pytest.mark.parametrize("surface", ["serving", "judge"])
@pytest.mark.parametrize("operation", ["count_tokens", "generation"])
def test_recorder_preserves_cancellation_after_recording_safe_diagnostic(
    corpus: Corpus, surface: Surface, operation: Operation
) -> None:
    port = DirectPort(operation, asyncio.CancelledError(SENTINEL))
    recorder, count_payload, generation_payload, stream = direct_recorder(surface, corpus, port)

    async def run() -> None:
        if operation == "generation":
            await recorder.count_tokens(**count_payload)
        with pytest.raises(asyncio.CancelledError):
            if operation == "count_tokens":
                await recorder.count_tokens(**count_payload)
            else:
                await recorder.create(**generation_payload)

    asyncio.run(run())
    terminal = json.loads(stream.getvalue().splitlines()[-1])
    assert terminal["value"] == {
        "code": "deadline_exceeded",
        "diagnostic": {"schema_version": 1, "category": "cancelled", "http_status": None},
    }
    assert SENTINEL not in stream.getvalue()
    assert port.calls == (
        ["count_tokens"] if operation == "count_tokens" else ["count_tokens", "generation"]
    )
