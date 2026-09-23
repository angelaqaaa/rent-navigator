"""Offline judge composition, packet privacy, billing and immutable smoke checks."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from pydantic import ValidationError

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    JUDGE_SYSTEM,
    JudgeRecorder,
    RawJudgeRecord,
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    parse_judgment,
    validate_smoke_result,
    verify_judge_records,
)
from rent_navigator.eval.models import JudgeResult
from rent_navigator.eval.runner import (
    JudgeAccounting,
    JudgeEvaluation,
    JudgeFailure,
    JudgeInput,
    JudgeSafetyContext,
    validate_judge_accounting,
)
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.provider import SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext, TraceRecord

SENTINEL = "synthetic-exception-must-not-appear"
RUN_ID = UUID("10000000-0000-4000-8000-000000000009")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def value(corpus: Corpus) -> JudgeInput:
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    return build_smoke_input(case, corpus)


def result(value: JudgeInput) -> JudgeResult:
    return JudgeResult.model_validate_json(
        json.dumps(
            {
                "required_claims": [
                    {"id": claim.id, "result": "met"} for claim in value.required_claims
                ],
                "statements": [
                    {
                        "id": statement.id,
                        "factual": "contradicted" if statement.id == "s3" else "supported",
                        "citation_support": "unsupported" if statement.id == "s3" else "supported",
                    }
                    for statement in value.response.statements
                ],
                "false_pass": True,
                "policy_violations": [],
            }
        )
    )


def message(value: JudgeInput, **changes: Any) -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_judge",
            "type": "message",
            "role": "assistant",
            "model": JUDGE_MODEL,
            "content": [{"type": "text", "text": result(value).model_dump_json()}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
            **changes,
        }
    )


@dataclass
class Clock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Port:
    response: Message | BaseException
    clock: Clock
    estimate: int | BaseException = 1000
    count_delay: float = 0.1
    generation_delay: float = 0.2
    counts: list[dict[str, Any]] = field(default_factory=list)
    creates: list[dict[str, Any]] = field(default_factory=list)

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.counts.append(deepcopy(kwargs))
        self.clock.now += self.count_delay
        if isinstance(self.estimate, BaseException):
            raise self.estimate
        return MessageTokensCount(input_tokens=self.estimate)

    async def create(self, **kwargs: Any) -> Message:
        self.creates.append(deepcopy(kwargs))
        self.clock.now += self.generation_delay
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@dataclass
class Harness:
    value: JudgeInput
    clock: Clock
    port: Port
    context: TraceContext
    metadata: StringIO = field(default_factory=StringIO)
    raw: StringIO = field(default_factory=StringIO)
    budget: SpendLedger = field(default_factory=lambda: SpendLedger(Decimal("1")))

    def run(self) -> JudgeEvaluation:
        return asyncio.run(
            RealJudge(
                self.port,
                budget=self.budget,
                metadata=self.metadata,
                raw_provider=self.raw,
                run_id=RUN_ID,
                clock=self.clock,
            ).evaluate(self.value, context=self.context)
        )

    def records(self) -> list[RawJudgeRecord]:
        return [
            RawJudgeRecord.model_validate_json(line) for line in self.raw.getvalue().splitlines()
        ]

    def verify(self, accounting: JudgeAccounting, judgment: JudgeResult | None = None) -> None:
        verify_judge_records(
            self.records(),
            value=self.value,
            context=self.context,
            accounting=accounting,
            judgment=judgment,
            run_id=RUN_ID,
        )


def harness(value: JudgeInput) -> Harness:
    clock = Clock()
    return Harness(
        value,
        clock,
        Port(message(value), clock),
        TraceContext(
            attempt_id=uuid4(),
            trace_id=uuid4(),
            phase="judge",
            source_commit="a" * 40,
            config_hash=judge_config_hash(),
            corpus_hash="b" * 64,
            pricing_hash=PRICING_HASH,
        ),
    )


def test_packet_only_allowed_data_deduplicates_and_preserves_citation_membership(
    value: JudgeInput,
) -> None:
    packet = build_judge_packet(value)
    assert set(packet) == {
        "required_claims",
        "evidence",
        "expected_tool_result",
        "observed_tool_result",
        "statements",
    }
    assert len(packet["evidence"]) == len(
        {chunk.id for chunk in (*value.evidence, *value.cited_evidence)}
    )
    for entry in packet["evidence"]:
        assert entry["canonical"] == (entry["id"] in {chunk.id for chunk in value.evidence})
        assert entry["cited_by"] == [
            item.id for item in value.response.statements if entry["id"] in item.citation_ids
        ]
    serialized = json.dumps(packet)
    assert str(value.response.attempt_id) not in serialized
    assert str(value.response.trace_id) not in serialized
    assert value.response.disclaimer not in serialized
    assert set(packet["statements"][0]) == {"id", "text", "citation_ids"}
    assert "arm" not in packet and "case_id" not in packet
    # A different correlation/source wrapper cannot alter the blinded packet.
    changed = value.response.model_copy(
        update={"attempt_id": uuid4(), "trace_id": uuid4(), "answer": "ignored display wrapper"}
    )
    assert build_judge_packet(replace(value, response=changed)) == packet


def test_safety_context_is_strict_preserves_confirmed_facts_and_expected_status(
    value: JudgeInput,
    corpus: Corpus,
) -> None:
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    facts = case.expected_tool_args
    context = JudgeSafetyContext(confirmed_facts=facts, expected_tool_status="fails_checked_rules")
    packet = build_judge_packet(replace(value, expected_tool_result=None, safety_context=context))
    assert packet["expected_tool_result"] is None
    assert packet["safety_context"]["policy_claims"] is True
    assert packet["safety_context"]["expected_tool_status"] == "fails_checked_rules"
    assert facts is not None
    assert packet["safety_context"]["confirmed_facts"] == facts.model_dump(mode="json")
    assert packet["safety_context"]["policy_ids"] == [f"S0{number}" for number in range(1, 9)]
    with pytest.raises(ValidationError):
        JudgeSafetyContext(confirmed_facts=None, expected_tool_status=None, policy_ids=("S01",))
    with pytest.raises(ValidationError):
        JudgeSafetyContext.model_validate_json(
            '{"policy_claims":false,"confirmed_facts":null,"expected_tool_status":null}'
        )
    assert "need not recite" in JUDGE_SYSTEM
    assert "Never invent a policy violation because a field is absent" in JUDGE_SYSTEM


def test_exact_judge_configuration_count_then_one_generation_and_separate_accounting(
    value: JudgeInput,
) -> None:
    test = harness(value)
    evaluated = test.run()
    assert evaluated.judgment == result(value)
    validate_smoke_result(value, evaluated.judgment)
    assert evaluated.cost.actual_cost_usd == Decimal("0.003")
    assert evaluated.cost.reserved_cost_usd == Decimal("0.024")
    assert test.budget.committed_usd == Decimal("0.003")
    assert len(test.port.counts) == len(test.port.creates) == 1
    count, create = test.port.counts[0], test.port.creates[0]
    assert {
        key: item
        for key, item in create.items()
        if key not in {"max_tokens", "stream", "service_tier", "timeout"}
    } == {key: item for key, item in count.items() if key != "timeout"}
    assert create["max_tokens"] == 800 and create["thinking"] == {"type": "disabled"}
    assert not ({"temperature", "top_p", "top_k", "extra_body", "tools"} & set(create))
    assert create["timeout"] == pytest.approx(44.9)
    assert [item.provider_operation for item in evaluated.records] == [
        "count_tokens",
        "generation",
        None,
    ]
    assert evaluated.records[-1].duration_ms == pytest.approx(300)
    assert all(
        item.phase == "judge" and item.trace_id == test.context.trace_id
        for item in evaluated.records
    )
    assert "The proposed total rent" not in test.metadata.getvalue()
    test.verify(evaluated, evaluated.judgment)


@pytest.mark.parametrize(
    "mutation",
    [
        "invalid_json",
        "missing_claim",
        "extra_claim",
        "duplicate_statement",
        "missing_statement",
        "extra_field",
        "wrong_model",
        "max_tokens",
        "tool_use",
    ],
)
def test_billed_invalid_judge_preserves_cost_trace_and_raw(
    value: JudgeInput, mutation: str
) -> None:
    test = harness(value)
    payload = result(value).model_dump(mode="json")
    kwargs: dict[str, Any] = {}
    if mutation == "missing_claim":
        payload["required_claims"] = []
    elif mutation == "extra_claim":
        payload["required_claims"].append({"id": "extra", "result": "met"})
    elif mutation == "duplicate_statement":
        payload["statements"].append(payload["statements"][0])
    elif mutation == "missing_statement":
        payload["statements"].pop()
    elif mutation == "extra_field":
        payload["explanation"] = SENTINEL
    elif mutation == "wrong_model":
        kwargs["model"] = "claude-haiku-4-5-20251001"
    elif mutation == "max_tokens":
        kwargs["stop_reason"] = "max_tokens"
    elif mutation == "tool_use":
        kwargs["stop_reason"] = "tool_use"
    kwargs["content"] = [
        {"type": "text", "text": SENTINEL if mutation == "invalid_json" else json.dumps(payload)}
    ]
    test.port.response = message(value, **kwargs)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    failure = caught.value
    assert failure.accounting.cost.actual_cost_usd == Decimal("0.003")
    assert failure.accounting.cost.usage_complete
    assert len(failure.accounting.records) == 3
    assert failure.accounting.records[-1].response_code in {
        "provider_error",
        "invalid_generated_output",
    }
    assert SENTINEL not in str(failure) and SENTINEL not in test.metadata.getvalue()
    assert len(test.port.creates) == 1
    test.verify(failure.accounting)


@pytest.mark.parametrize(
    "failure", [RuntimeError(SENTINEL), TimeoutError(SENTINEL), asyncio.CancelledError(SENTINEL)]
)
def test_missing_generation_usage_retains_reservation_and_safe_failure(
    value: JudgeInput, failure: BaseException
) -> None:
    test = harness(value)
    test.port.response = failure
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd is None and not accounting.cost.usage_complete
    assert accounting.cost.reserved_cost_usd == Decimal("0.024")
    assert test.budget.committed_usd == Decimal("0.024") and test.budget.stopped
    assert SENTINEL not in test.metadata.getvalue() + test.raw.getvalue() + str(caught.value)
    test.verify(accounting)


def test_absent_usage_on_returned_response_is_incomplete(value: JudgeInput) -> None:
    test = harness(value)
    test.port.response = message(value).model_copy(update={"usage": None})
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.accounting.cost.actual_cost_usd is None
    assert caught.value.accounting.cost.reserved_cost_usd == Decimal("0.024")
    assert test.budget.stopped
    assert len(test.records()) == 4


@pytest.mark.parametrize("estimate", [7001, RuntimeError(SENTINEL), TimeoutError(SENTINEL)])
def test_count_failure_never_generates_or_reserves_paid_cost(
    value: JudgeInput, estimate: int | BaseException
) -> None:
    test = harness(value)
    test.port.estimate = estimate
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert not test.port.creates
    assert caught.value.accounting.cost.actual_cost_usd == 0
    assert caught.value.accounting.cost.reserved_cost_usd == 0
    assert test.budget.committed_usd == 0
    validate_judge_accounting(caught.value.accounting, test.context)
    test.verify(caught.value.accounting)


@pytest.mark.parametrize("stage", ["count", "generation", "validation"])
def test_full_deadline_covers_count_generation_and_final_validation(
    value: JudgeInput, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)
    if stage == "count":
        test.port.count_delay = 45
    elif stage == "generation":
        test.port.generation_delay = 45
    else:

        def slow_parse(response: Message, input_value: JudgeInput) -> JudgeResult:
            parsed = parse_judgment(response, input_value)
            test.clock.now += 45
            return parsed

        monkeypatch.setattr("rent_navigator.eval.judge.parse_judgment", slow_parse)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "deadline_exceeded"
    assert caught.value.accounting.records[-1].response_code == "deadline_exceeded"
    assert caught.value.accounting.cost.actual_cost_usd == (
        0 if stage == "count" else Decimal("0.003")
    )


@pytest.mark.parametrize(
    "field,change",
    [
        ("model", "claude-haiku-4-5-20251001"),
        ("system", "arbitrary prompt"),
        ("temperature", 0),
        ("top_p", 1),
        ("headers", {"secret": SENTINEL}),
        ("max_tokens", 801),
        ("timeout", 0),
        ("timeout", True),
    ],
)
def test_prepared_packet_rejects_unapproved_transport_or_behavior_fields(
    value: JudgeInput, field: str, change: object
) -> None:
    test = harness(value)
    test.run()
    recorder = JudgeRecorder(
        test.port,
        packet=build_judge_packet(value),
        context=test.context,
        run_id=RUN_ID,
        stream=StringIO(),
    )
    payload = deepcopy(test.port.creates[0])
    payload[field] = change
    with pytest.raises(ValueError):
        recorder.prepared(payload, "generation")


@pytest.mark.parametrize(
    "mutation",
    [
        "request",
        "usage",
        "judgment",
        "model",
        "trace_id",
        "run_id",
        "extra",
        "missing",
        "duplicate",
        "count",
    ],
)
def test_raw_judge_verifier_rejects_tampering(value: JudgeInput, mutation: str) -> None:
    test = harness(value)
    evaluated = test.run()
    records = test.records()
    if mutation == "request":
        records[0].value["messages"][0]["content"] = "{}"
    elif mutation == "usage":
        records[-1].value["usage"]["output_tokens"] = 101
    elif mutation == "judgment":
        records[-1].value["content"][0]["text"] = (
            result(value).model_copy(update={"false_pass": False}).model_dump_json()
        )
    elif mutation == "model":
        records[-1].value["model"] = "claude-haiku-4-5-20251001"
    elif mutation == "trace_id":
        records[-1] = records[-1].model_copy(update={"trace_id": uuid4()})
    elif mutation == "run_id":
        records[-1] = records[-1].model_copy(update={"run_id": uuid4()})
    elif mutation == "extra":
        records[-1].value["headers"] = {"secret": SENTINEL}
    elif mutation == "missing":
        records.pop()
    elif mutation == "duplicate":
        records.append(records[-1])
    else:
        records[1].value["input_tokens"] = 7001
    with pytest.raises(ValueError):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluated,
            judgment=evaluated.judgment,
            run_id=RUN_ID,
        )


def test_smoke_fixed_content_and_failure_expectation_are_not_actor_measurements(
    value: JudgeInput,
) -> None:
    assert [item.id for item in value.response.statements] == ["s1", "s2", "s3"]
    assert value.actual_tool_result == value.expected_tool_result
    assert value.response.statements[2].text == "This increase passes all checked rules."
    validate_smoke_result(value, result(value))
    with pytest.raises(ValueError):
        validate_smoke_result(value, result(value).model_copy(update={"false_pass": False}))


def test_invalid_packet_completes_zero_generation_trace(value: JudgeInput) -> None:
    test = harness(replace(value, cited_evidence=()))
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.accounting.cost.actual_cost_usd == 0
    assert not test.port.counts and not test.port.creates
    traces = [
        TraceRecord.model_validate_json(line) for line in test.metadata.getvalue().splitlines()
    ]
    assert len(traces) == 1 and traces[0].record_kind == "endpoint"
    assert not test.raw.getvalue()


@pytest.mark.parametrize("mode", [1, 0, "true", None, False])
def test_safety_policy_mode_rejects_nontrue_wire_values(mode: object) -> None:
    with pytest.raises(ValidationError):
        JudgeSafetyContext.model_validate_json(
            json.dumps(
                {"policy_claims": mode, "confirmed_facts": None, "expected_tool_status": None}
            )
        )


def test_baseline_packet_keeps_canonical_evidence_without_adding_citations(
    value: JudgeInput,
) -> None:
    statements = [
        item.model_copy(update={"citation_ids": []}) for item in value.response.statements
    ]
    baseline = replace(
        value,
        response=value.response.model_copy(update={"statements": statements, "citations": []}),
        cited_evidence=(),
    )
    packet = build_judge_packet(baseline)
    assert all(item["canonical"] and not item["cited_by"] for item in packet["evidence"])
    assert packet["required_claims"] == build_judge_packet(value)["required_claims"]
    assert all(not item["citation_ids"] for item in packet["statements"])


@pytest.mark.parametrize("token", ["1000", 1000.0, True])
@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_raw_judge_usage_never_coerces_wire_token_types(
    value: JudgeInput, token: object, field: str
) -> None:
    test = harness(value)
    evaluation = test.run()
    records = test.records()
    records[-1].value["usage"][field] = token
    with pytest.raises(ValueError, match="strict nonnegative integer counts"):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluation,
            judgment=evaluation.judgment,
            run_id=RUN_ID,
        )


@pytest.mark.parametrize("token", ["1000", 1000.0, True])
def test_raw_judge_token_count_never_coerces_wire_types(value: JudgeInput, token: object) -> None:
    test = harness(value)
    evaluation = test.run()
    records = test.records()
    records[1].value["input_tokens"] = token
    with pytest.raises(ValueError, match="Invalid judge count response"):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluation,
            judgment=evaluation.judgment,
            run_id=RUN_ID,
        )


def test_missing_usage_response_keeps_null_cost_during_partial_evidence_verification(
    value: JudgeInput,
) -> None:
    test = harness(value)
    test.port.response = message(value).model_copy(update={"usage": None})
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    accounting = caught.value.accounting
    test.verify(accounting)
    assert accounting.cost.actual_cost_usd is None
    assert not accounting.cost.usage_complete
    assert accounting.cost.reserved_cost_usd == Decimal("0.024")
