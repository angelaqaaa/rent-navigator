"""Synthetic integration through the ordinary extraction and analysis seams."""

import asyncio
import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
from typing import Any
from uuid import UUID

import pytest
from anthropic.types import Message, MessageTokensCount
from eval_fixtures import TOY_HASH, TOY_RUN_ID, TOY_SOURCE_SHA, toy_cases

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.models import (
    Arm,
    GoldCase,
    JudgeClaimResult,
    JudgeResult,
    JudgeStatementResult,
)
from rent_navigator.eval.recording import SyntheticAllowlist, SyntheticRecorder
from rent_navigator.eval.runner import (
    AttemptIdentity,
    AttemptOutcome,
    JudgeEvaluation,
    JudgeInput,
    PlanEntry,
    run_attempt,
)
from rent_navigator.index import SearchHit
from rent_navigator.models import (
    AskResponse,
    ErrorResponse,
    NoticeRequest,
    RentFacts,
    RentRequest,
    ToolResult,
)
from rent_navigator.provider import ProviderFailure, SpendLedger
from rent_navigator.rent import rent_increase_check
from rent_navigator.trace import (
    ACTOR_MODEL,
    JUDGE_MODEL,
    MetadataSink,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    Usage,
    cost_for_usage,
    provider_cost_totals,
)

JUDGE_CONFIG = "d" * 64
ERROR_SENTINEL = "synthetic-private-exception-detail-do-not-retain"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def _message(content: list[dict[str, Any]], *, stop: str = "end_turn") -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_eval",
            "type": "message",
            "role": "assistant",
            "model": ACTOR_MODEL,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )


def _final(corpus: Corpus, arm: Arm, *, refusal: bool = False) -> Message:
    return _message(
        [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "kind": "refusal" if refusal else "answer",
                        "refusal_reason": "insufficient_evidence" if refusal else None,
                        "statements": []
                        if refusal
                        else [
                            {
                                "id": "s1",
                                "text": "Synthetic output proposition.",
                                "citation_ids": [corpus.chunks[0].id]
                                if arm == "production"
                                else [],
                            }
                        ],
                    }
                ),
            }
        ]
    )


def _selection(case: GoldCase) -> Message:
    assert isinstance(case.request, NoticeRequest | RentRequest)
    return _message(
        [
            {
                "type": "tool_use",
                "id": "toolu_synthetic_eval",
                "name": case.expected_tool,
                "input": case.request.facts.model_dump(mode="json"),
            }
        ],
        stop="tool_use",
    )


def _extraction(**changes: object) -> Message:
    return _message(
        [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "current_cents": None,
                        "proposed_cents": None,
                        "effective_on": None,
                        **changes,
                    }
                ),
            }
        ]
    )


class ScriptedMessages:
    def __init__(self, responses: Sequence[Message | BaseException], clock: Clock) -> None:
        self.responses = list(responses)
        self.clock = clock
        self.counts: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.estimates: list[int | BaseException] = [1000] * 4

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.counts.append(deepcopy(kwargs))
        self.clock.now += 0.1
        estimate = self.estimates[len(self.counts) - 1]
        if isinstance(estimate, BaseException):
            raise estimate
        return MessageTokensCount(input_tokens=estimate)

    async def create(self, **kwargs: Any) -> Message:
        self.creates.append(deepcopy(kwargs))
        self.clock.now += 0.2
        response = self.responses[len(self.creates) - 1]
        if isinstance(response, BaseException):
            raise response
        return response


class SyntheticJudge:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.inputs: list[JudgeInput] = []
        self.contexts: list[TraceContext] = []
        self.invalid_coverage = False
        self.missing_usage = False
        self.error: Exception | None = None
        self.config_hash = JUDGE_CONFIG

    async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation:
        self.inputs.append(value)
        self.contexts.append(context)
        self.clock.now += 50.0
        if self.error is not None:
            raise self.error
        stream = StringIO()
        with TraceRecorder(context, MetadataSink(stream), monotonic=self.clock) as trace:
            with trace.provider_call(JUDGE_MODEL) as call:
                call.usage = (
                    None if self.missing_usage else Usage(input_tokens=100, output_tokens=50)
                )
                call.returned_model_id = JUDGE_MODEL
        judgment = JudgeResult(
            required_claims=[]
            if self.invalid_coverage
            else [JudgeClaimResult(id=claim.id, result="met") for claim in value.required_claims],
            statements=[
                JudgeStatementResult(
                    id=statement.id,
                    factual="supported",
                    citation_support="supported" if statement.citation_ids else "not_applicable",
                )
                for statement in value.response.statements
            ],
            false_pass=False,
            policy_violations=[],
        )
        return JudgeEvaluation(
            judgment=judgment,
            config_hash=self.config_hash,
            requested_model_id=JUDGE_MODEL,
            returned_model_id=JUDGE_MODEL,
            cost=cost_for_usage(
                JUDGE_MODEL,
                None if self.missing_usage else Usage(input_tokens=100, output_tokens=50),
            ),
            records=[
                TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()
            ],
        )


@dataclass
class Harness:
    corpus: Corpus
    case: GoldCase
    arm: Arm
    clock: Clock
    fake: ScriptedMessages
    judge: SyntheticJudge | None
    metadata: StringIO = field(default_factory=StringIO)
    raw: StringIO = field(default_factory=StringIO)
    queries: list[str] = field(default_factory=list)
    budget: SpendLedger = field(default_factory=lambda: SpendLedger(Decimal("1")))

    def retrieve(self, question: str) -> tuple[SearchHit, ...]:
        self.queries.append(question)
        return (SearchHit(self.corpus.chunks[0], -1.0),)

    def run(
        self, *, warmup: bool = False, allowlist: SyntheticAllowlist | None = None
    ) -> AttemptOutcome:
        return asyncio.run(
            run_attempt(
                self.case,
                PlanEntry(case_id=self.case.id, arm=self.arm, repeat=0),
                identity=AttemptIdentity(
                    run_id=UUID(TOY_RUN_ID), source_sha=TOY_SOURCE_SHA, gold_hash=TOY_HASH
                ),
                corpus=self.corpus,
                messages=self.fake,
                budget=self.budget,
                allowlist=allowlist or SyntheticAllowlist.from_fixtures([self.case]),
                retrieve=self.retrieve,
                metadata=self.metadata,
                raw_provider=self.raw,
                judge=self.judge,
                judge_config_hash=JUDGE_CONFIG,
                clock=self.clock,
                warmup=warmup,
            )
        )


def _harness(corpus: Corpus, case_id: str = "Q01", *, arm: Arm = "production") -> Harness:
    case = next(case for case in toy_cases(corpus) if case.id == case_id)
    clock = Clock()
    responses = []
    if case.letter is not None:
        responses.append(_extraction())
    if case.kind != "qa":
        responses.append(_selection(case))
    responses.append(_final(corpus, arm))
    return Harness(
        corpus, case, arm, clock, ScriptedMessages(responses, clock), SyntheticJudge(clock)
    )


@pytest.mark.parametrize(
    "case_id,call_count", [("Q01", 1), ("N01", 2), ("R01", 2), ("R02", 3), ("R04", 3)]
)
@pytest.mark.parametrize("arm", ["production", "baseline"])
def test_actual_pipeline_synthetic_cases_and_arm_fairness(
    corpus: Corpus, case_id: str, call_count: int, arm: Arm
) -> None:
    harness = _harness(corpus, case_id, arm=arm)
    outcome = harness.run()
    assert outcome.row.classification == "success", outcome.reasons
    assert len(harness.fake.creates) == len(harness.fake.counts) == call_count
    assert outcome.row.serving_cost_usd == call_count * Decimal("0.0015")
    assert outcome.row.judge_cost_usd == Decimal("0.0007")
    assert outcome.row.latency_ms == pytest.approx(call_count * 300)
    assert outcome.row.retrieved_ids == ([corpus.chunks[0].id] if arm == "production" else [])
    assert len(harness.queries) == (1 if arm == "production" else 0)
    assert outcome.row.attempt_id != harness.case.request.attempt_id
    assert outcome.row.actual_tool_args == harness.case.expected_tool_args
    assert outcome.row.actual_tool_result == harness.case.expected_tool_result
    assert outcome.row.actual_extract == harness.case.expected_extract
    assert harness.judge is not None
    assert len(harness.judge.inputs) == 1
    assert harness.judge.inputs[0].evidence == tuple(
        corpus.chunk(identifier) for identifier in sorted(set(harness.case.evidence_ids))
    )
    assert not hasattr(harness.judge.inputs[0], "arm")
    if harness.case.letter:
        assert len(outcome.row.trace_ids) == 2
        assert len(set(outcome.row.trace_ids)) == 2
        assert {record.phase for record in outcome.records} == {"extraction", "analysis"}
        assert {record.attempt_id for record in outcome.records} == {outcome.row.attempt_id}
    if arm == "baseline":
        assert all(
            '"evidence"' not in json.dumps(call["messages"]) for call in harness.fake.creates
        )


@pytest.mark.parametrize(
    "field,value", [("current_cents", 1), ("proposed_cents", 2), ("effective_on", "2027-01-01")]
)
def test_extraction_mismatch_stops_without_replacing_observations(
    corpus: Corpus, field: str, value: object
) -> None:
    harness = _harness(corpus, "R02")
    harness.fake.responses[0] = _extraction(**{field: value})
    outcome = harness.run()
    assert outcome.row.classification == "incorrect"
    assert outcome.row.actual_extract != harness.case.expected_extract
    assert outcome.row.actual_tool_args is None
    assert outcome.row.actual_tool_result is None
    assert outcome.row.response is None
    assert len(outcome.row.trace_ids) == 1
    assert len(harness.fake.creates) == 1
    assert not harness.queries
    assert harness.judge is not None and not harness.judge.inputs
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    assert outcome.row.latency_ms == pytest.approx(300)


def test_tool_result_is_observed_before_failed_second_preflight(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01")
    harness.fake.estimates[1] = 16001
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "budget_exhausted"
    assert outcome.row.actual_tool_args == harness.case.expected_tool_args
    assert outcome.row.actual_tool_result == harness.case.expected_tool_result
    assert all(assertion.passed for assertion in outcome.row.deterministic_assertions)
    assert len(harness.fake.counts) == 2 and len(harness.fake.creates) == 1
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    assert "observation_missing" not in outcome.reasons


def test_unvalidated_tool_use_does_not_fabricate_execution(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01")
    selection = harness.fake.responses[0]
    assert isinstance(selection, Message)
    payload = selection.model_dump(mode="json")
    payload["content"][0]["input"]["current_cents"] = 999
    harness.fake.responses[0] = Message.model_validate(payload)
    outcome = harness.run()
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "tool_protocol_error"
    assert outcome.row.actual_tool_args is None
    assert outcome.row.actual_tool_result is None
    assert len(harness.fake.creates) == len(harness.fake.counts) == 1
    assert outcome.row.serving_cost_usd == Decimal("0.0015")


def test_retrieval_ids_are_not_rule_union_or_citation_union(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01")
    rule_ids = {identifier for rule in corpus.rules for identifier in rule.evidence_ids}
    chosen = next(
        identifier for identifier in sorted(rule_ids) if identifier != corpus.chunks[0].id
    )
    generated = _final(corpus, "production")
    payload = generated.model_dump(mode="json")
    final = json.loads(payload["content"][0]["text"])
    final["statements"][0]["citation_ids"] = [chosen]
    payload["content"][0]["text"] = json.dumps(final)
    harness.fake.responses[-1] = Message.model_validate(payload)
    outcome = harness.run()
    assert isinstance(outcome.row.response, AskResponse)
    assert outcome.row.retrieved_ids == [corpus.chunks[0].id]
    assert [citation.id for citation in outcome.row.response.citations] == [chosen]


def test_missing_usage_preserves_unknown_cost_and_reservation(corpus: Corpus) -> None:
    harness = _harness(corpus)
    message = harness.fake.responses[0]
    assert isinstance(message, Message)
    harness.fake.responses[0] = message.model_copy(update={"usage": None})
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert not outcome.row.usage_complete
    assert outcome.row.serving_cost_usd is None
    totals = provider_cost_totals(outcome.records)
    assert totals.actual_cost_usd is None
    assert totals.reserved_cost_usd == Decimal("0.019")
    assert harness.budget.stopped
    assert "serving_usage_missing" in outcome.reasons


@pytest.mark.parametrize("problem", ["missing", "coverage", "identity", "exception"])
def test_judge_failure_is_incomplete_error_without_raw_exception(
    corpus: Corpus, problem: str
) -> None:
    harness = _harness(corpus)
    assert harness.judge is not None
    if problem == "missing":
        harness.judge = None
    elif problem == "coverage":
        harness.judge.invalid_coverage = True
    elif problem == "identity":
        harness.judge.config_hash = "f" * 64
    else:
        harness.judge.error = RuntimeError(ERROR_SENTINEL)
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert outcome.row.judge is None
    assert "judge_missing" in outcome.reasons or "judge_invalid" in outcome.reasons
    assert (
        ERROR_SENTINEL
        not in harness.raw.getvalue() + harness.metadata.getvalue() + outcome.row.model_dump_json()
    )
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    if problem == "coverage":
        assert outcome.row.judge_cost_usd == Decimal("0.0007")


def test_clock_cost_and_metadata_exclude_judge_from_serving(corpus: Corpus) -> None:
    harness = _harness(corpus, "R02")
    outcome = harness.run()
    assert harness.clock.now == pytest.approx(150.9)
    assert outcome.row.latency_ms == pytest.approx(900)
    assert outcome.row.serving_cost_usd == Decimal("0.0045")
    assert outcome.row.judge_cost_usd == Decimal("0.0007")
    assert all(record.phase != "judge" for record in outcome.records)
    assert outcome.judge_evaluation is not None
    assert all(record.phase == "judge" for record in outcome.judge_evaluation.records)
    assert provider_cost_totals(outcome.records).actual_cost_usd == Decimal("0.0045")
    assert "Synthetic output proposition" not in harness.metadata.getvalue()
    assert "Synthetic isolated letter" not in harness.metadata.getvalue()


def test_raw_exception_becomes_safe_failure_code(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.fake.responses[0] = RuntimeError(ERROR_SENTINEL)
    outcome = harness.run()
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "provider_error"
    events = [json.loads(line) for line in harness.raw.getvalue().splitlines()]
    assert events[-1]["event"] == "failure"
    assert events[-1]["value"] == {"code": "provider_error"}
    assert (
        ERROR_SENTINEL
        not in harness.raw.getvalue() + harness.metadata.getvalue() + outcome.row.model_dump_json()
    )


def test_declared_fixture_membership_is_checked_before_recording(corpus: Corpus) -> None:
    harness = _harness(corpus)
    with pytest.raises(ValueError, match="allowlist"):
        harness.run(allowlist=SyntheticAllowlist.from_fixtures([_harness(corpus, "Q02").case]))
    assert not harness.metadata.getvalue() and not harness.raw.getvalue()
    assert not harness.fake.counts and not harness.fake.creates


def test_warmups_never_call_judge(corpus: Corpus) -> None:
    harness = _harness(corpus)
    outcome = harness.run(warmup=True)
    assert harness.judge is not None and not harness.judge.inputs
    assert outcome.row.judge is None and outcome.row.judge_cost_usd is None
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    assert "missing_judge" not in outcome.reasons


def test_refusal_occupies_attempt_without_judge(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.fake.responses[0] = _final(corpus, "production", refusal=True)
    outcome = harness.run()
    assert outcome.row.classification == "refusal"
    assert harness.judge is not None and not harness.judge.inputs
    assert outcome.row.latency_ms == pytest.approx(300)
    assert outcome.row.serving_cost_usd == Decimal("0.0015")


def test_recorder_rejects_extra_prepared_payload_before_logging(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.run()
    stream = StringIO()
    recorder = SyntheticRecorder(
        harness.fake,
        case=harness.case,
        allowlist=SyntheticAllowlist.from_fixtures([harness.case]),
        corpus=corpus,
        stream=stream,
        run_id=UUID(TOY_RUN_ID),
        attempt_id=UUID(int=901),
    )
    recorder.bind(UUID(int=902), "analysis", harness.case.request)
    payload = deepcopy(harness.fake.counts[0])
    payload["private_headers"] = ERROR_SENTINEL
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.count_tokens(**payload))
    assert stream.getvalue() == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("system", ERROR_SENTINEL),
        ("model", "unexpected-model"),
        ("thinking", {"type": "enabled", "budget_tokens": 1000}),
        ("extra_body", {"private_text": ERROR_SENTINEL}),
        (
            "output_config",
            {"format": {"type": "json_schema", "schema": {"description": ERROR_SENTINEL}}},
        ),
        ("timeout", 0),
        ("timeout", 46),
        ("timeout", float("inf")),
        ("timeout", float("nan")),
        ("timeout", True),
    ],
)
def test_recorder_rejects_unapproved_configuration_before_logging(
    corpus: Corpus, field: str, value: object
) -> None:
    harness = _harness(corpus)
    harness.run()
    stream = StringIO()
    recorder = SyntheticRecorder(
        harness.fake,
        case=harness.case,
        allowlist=SyntheticAllowlist.from_fixtures([harness.case]),
        corpus=corpus,
        stream=stream,
        run_id=UUID(TOY_RUN_ID),
        attempt_id=UUID(int=903),
    )
    recorder.bind(UUID(int=904), "analysis", harness.case.request)
    payload = deepcopy(harness.fake.counts[0])
    payload[field] = value
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.count_tokens(**payload))
    assert stream.getvalue() == ""
    assert len(harness.fake.counts) == 1


def test_cancelled_attempt_is_preserved_with_cost_reservation(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.fake.responses[0] = asyncio.CancelledError()
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert "interrupted" in outcome.reasons
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "deadline_exceeded"
    assert outcome.row.serving_cost_usd is None
    assert provider_cost_totals(outcome.records).reserved_cost_usd == Decimal("0.019")
    assert any(record.record_kind == "endpoint" for record in outcome.records)


def test_unobservable_executed_tool_is_not_recomputed(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rent_navigator.agent as agent_module

    harness = _harness(corpus, "R01")
    calls = 0
    original = rent_increase_check

    def counted(facts: RentFacts, *, corpus: Corpus) -> ToolResult:
        nonlocal calls
        calls += 1
        return original(facts, corpus=corpus)

    def fail_display(value: object) -> str:
        raise ValueError(ERROR_SENTINEL)

    monkeypatch.setattr(agent_module, "rent_increase_check", counted)
    monkeypatch.setattr(agent_module, "_cents_to_cad", fail_display)
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert "observation_missing" in outcome.reasons
    assert outcome.row.actual_tool_args is None
    assert outcome.row.actual_tool_result is None
    assert calls == 1
    assert len(harness.fake.creates) == 1
    assert ERROR_SENTINEL not in harness.raw.getvalue() + harness.metadata.getvalue()


def test_missing_judge_usage_retains_reservation_and_future_stop_reason(corpus: Corpus) -> None:
    harness = _harness(corpus)
    assert harness.judge is not None
    harness.judge.missing_usage = True
    outcome = harness.run()
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    assert outcome.row.usage_complete
    assert outcome.row.judge_cost_usd is None
    assert "judge_usage_missing" in outcome.reasons
    assert outcome.judge_accounting is not None
    assert outcome.judge_accounting.cost.reserved_cost_usd == Decimal("0.024")
    assert not outcome.judge_accounting.cost.usage_complete


def test_invalid_optional_judge_metadata_preserves_known_accounting(corpus: Corpus) -> None:
    class InvalidCompletionMetadata(SyntheticJudge):
        async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation:
            evaluation = await super().evaluate(value, context=context)
            return evaluation.model_copy(update={"returned_model_id": "unexpected/model-id"})

    harness = _harness(corpus)
    harness.judge = InvalidCompletionMetadata(harness.clock)
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert outcome.row.judge is None
    assert outcome.row.judge_cost_usd == Decimal("0.0007")
    assert outcome.judge_accounting is not None
    assert outcome.judge_accounting.cost.actual_cost_usd == Decimal("0.0007")
    assert "unexpected/model-id" not in harness.metadata.getvalue() + harness.raw.getvalue()


@pytest.mark.parametrize("missing_usage", [False, True])
def test_typed_failed_judge_keeps_billed_accounting_without_a_judgment(
    corpus: Corpus, missing_usage: bool
) -> None:
    from rent_navigator.eval.runner import JudgeAccounting, JudgeFailure

    class FailedJudge(SyntheticJudge):
        async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation:
            evaluated = await super().evaluate(value, context=context)
            accounting = JudgeAccounting.model_validate_json(
                evaluated.model_dump_json(exclude={"judgment"})
            )
            raise JudgeFailure("invalid_generated_output", accounting)

    harness = _harness(corpus, "Q01")
    failed = FailedJudge(harness.clock)
    failed.missing_usage = missing_usage
    harness.judge = failed
    outcome = harness.run()
    assert outcome.row.classification == "error"
    assert outcome.row.judge is None and outcome.judge_evaluation is None
    assert outcome.judge_accounting is not None
    assert outcome.row.judge_cost_usd == (None if missing_usage else Decimal("0.0007"))
    assert outcome.judge_accounting.cost.reserved_cost_usd == Decimal("0.024")
    assert "judge_invalid" in outcome.reasons
    assert ("judge_usage_missing" in outcome.reasons) == missing_usage
    assert outcome.row.serving_cost_usd == Decimal("0.0015")
    assert all(record.phase != "judge" for record in outcome.records)
