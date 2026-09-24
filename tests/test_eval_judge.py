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
from anthropic.types import Usage as SDKUsage
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
    judge_schema,
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
from rent_navigator.provider import ProviderFailure, SpendLedger
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


def wire_payload(payload: JudgeResult | dict[str, Any]) -> dict[str, Any]:
    """Serialize fixture grades without changing their values or trusted IDs."""
    data = (
        payload.model_dump(mode="json") if isinstance(payload, JudgeResult) else deepcopy(payload)
    )
    for name in ("required_claims", "statements"):
        entries = data[name]
        assert len({item["id"] for item in entries}) == len(entries)
        data[name] = {
            item["id"]: {key: value for key, value in item.items() if key != "id"}
            for item in entries
        }
    return data


def message(value: JudgeInput, **changes: Any) -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_judge",
            "type": "message",
            "role": "assistant",
            "model": JUDGE_MODEL,
            "content": [{"type": "text", "text": json.dumps(wire_payload(result(value)))}],
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


@pytest.mark.parametrize("estimate", [1000, 24000])
def test_exact_judge_configuration_count_then_one_generation_and_separate_accounting(
    value: JudgeInput,
    estimate: int,
) -> None:
    test = harness(value)
    test.port.estimate = estimate
    evaluated = test.run()
    assert evaluated.judgment == result(value)
    validate_smoke_result(value, evaluated.judgment)
    assert evaluated.cost.actual_cost_usd == Decimal("0.003")
    assert evaluated.cost.reserved_cost_usd == Decimal("0.058")
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
        "legacy_array",
        "missing_statement",
        "extra_field",
        "max_tokens",
        "tool_use",
        "refusal",
    ],
)
def test_billed_invalid_judge_preserves_cost_trace_and_raw(
    value: JudgeInput, mutation: str
) -> None:
    test = harness(value)
    payload = wire_payload(result(value))
    kwargs: dict[str, Any] = {}
    if mutation == "missing_claim":
        payload["required_claims"] = {}
    elif mutation == "extra_claim":
        payload["required_claims"]["extra"] = {"result": "met"}
    elif mutation == "legacy_array":
        payload = result(value).model_dump(mode="json")
    elif mutation == "missing_statement":
        payload["statements"].pop(next(iter(payload["statements"])))
    elif mutation == "extra_field":
        payload["explanation"] = SENTINEL
    elif mutation == "max_tokens":
        kwargs["stop_reason"] = "max_tokens"
    elif mutation == "tool_use":
        kwargs["stop_reason"] = "tool_use"
    elif mutation == "refusal":
        kwargs["stop_reason"] = "refusal"
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
    assert accounting.cost.reserved_cost_usd == Decimal("0.058")
    assert test.budget.committed_usd == Decimal("0.058") and test.budget.stopped
    assert SENTINEL not in test.metadata.getvalue() + test.raw.getvalue() + str(caught.value)
    test.verify(accounting)


def test_absent_usage_on_returned_response_is_incomplete(value: JudgeInput) -> None:
    test = harness(value)
    test.port.response = message(value).model_copy(update={"usage": None})
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.accounting.cost.actual_cost_usd is None
    assert caught.value.accounting.cost.reserved_cost_usd == Decimal("0.058")
    assert test.budget.stopped
    assert len(test.records()) == 4


@pytest.mark.parametrize("estimate", [24001, RuntimeError(SENTINEL), TimeoutError(SENTINEL)])
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
        records[-1].value["content"][0]["text"] = json.dumps(
            wire_payload(result(value).model_copy(update={"false_pass": False}))
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
        records[1].value["input_tokens"] = 24001
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
    assert accounting.cost.reserved_cost_usd == Decimal("0.058")


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_native_boolean_usage_survives_judge_recording_and_failed_replay(
    value: JudgeInput, field: str
) -> None:
    test = harness(value)
    tokens: dict[str, Any] = {"input_tokens": 1000, "output_tokens": 100, field: True}
    test.port.response = message(value).model_copy(
        update={"usage": SDKUsage.model_construct(**tokens)}
    )
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "provider_error"
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd is None and not accounting.cost.usage_complete
    assert accounting.cost.reserved_cost_usd == Decimal(".058")
    assert test.budget.stopped and test.budget.committed_usd == Decimal(".058")
    assert test.records()[-1].value["usage"][field] is True
    test.verify(accounting)
    with pytest.raises(JudgeFailure):
        test.run()
    assert len(test.port.counts) == len(test.port.creates) == 1


def test_native_boolean_count_is_retained_without_judge_generation(
    value: JudgeInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    test = harness(value)

    async def count(**kwargs: Any) -> MessageTokensCount:
        test.port.counts.append(kwargs)
        return MessageTokensCount.model_construct(input_tokens=True)

    monkeypatch.setattr(test.port, "count_tokens", count)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "provider_error"
    assert not test.port.creates and caught.value.accounting.cost.actual_cost_usd == 0
    assert test.records()[-1].value["input_tokens"] is True
    test.verify(caught.value.accounting)


@pytest.mark.parametrize(
    "shape",
    [
        "absent",
        "null",
        "missing_input",
        "null_input",
        "string_input",
        "float_output",
        "object",
        "non_sdk",
    ],
)
def test_failed_native_usage_shapes_have_faithful_or_safe_judge_evidence(
    value: JudgeInput, shape: str
) -> None:
    def native_usage(**values: Any) -> SDKUsage:
        return SDKUsage.model_construct(**values)

    test = harness(value)
    response = message(value)
    usage: Any = None
    if shape == "absent":
        del response.__dict__["usage"]
        response.model_fields_set.discard("usage")
    else:
        if shape == "missing_input":
            usage = native_usage(output_tokens=100)
        elif shape in {"null_input", "string_input", "object"}:
            token = {"null_input": None, "string_input": "1000", "object": object()}[shape]
            usage = native_usage(input_tokens=token, output_tokens=100)
        elif shape == "float_output":
            usage = native_usage(input_tokens=1000, output_tokens=100.0)
        elif shape == "non_sdk":
            usage = {"input_tokens": 1000, "output_tokens": 100}
        response = response.model_copy(update={"usage": usage})
    test.port.response = response
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    accounting = caught.value.accounting
    assert caught.value.code == "provider_error" and accounting.cost.actual_cost_usd is None
    assert test.budget.stopped and test.budget.committed_usd == Decimal(".058")
    terminal = test.records()[-1]
    if shape in {"object", "non_sdk"}:
        assert terminal.event == "failure" and terminal.value == {"code": "provider_error"}
        assert accounting.cost.input_tokens is None and accounting.cost.output_tokens is None
    else:
        assert terminal.event == "response"
        if shape == "absent":
            assert "usage" not in terminal.value
        elif shape == "null":
            assert terminal.value["usage"] is None
        elif shape == "missing_input":
            assert terminal.value["usage"] == {"output_tokens": 100}
        elif shape == "null_input":
            assert terminal.value["usage"]["input_tokens"] is None
        elif shape == "string_input":
            assert terminal.value["usage"]["input_tokens"] == "1000"
        else:
            assert type(terminal.value["usage"]["output_tokens"]) is float
    test.verify(accounting)


def test_failure_audit_rejects_repriced_known_cost_for_mismatched_judge(value: JudgeInput) -> None:
    test = harness(value)
    evaluated = test.run()
    records = test.records()
    records[-1].value["model"] = "claude-haiku-4-5-20251001"
    payload = json.loads(evaluated.model_dump_json(exclude={"judgment"}))
    payload["returned_model_id"] = "claude-haiku-4-5-20251001"
    for record in payload["records"]:
        if record["provider_operation"] == "generation":
            record["returned_model_id"] = "claude-haiku-4-5-20251001"
            record["response_code"] = "provider_error"
        elif record["record_kind"] == "endpoint":
            record["response_code"] = "provider_error"
    accounting = JudgeAccounting.model_validate_json(json.dumps(payload))
    assert accounting.cost.actual_cost_usd == Decimal("0.003")
    with pytest.raises(ValueError, match="model.*unverified"):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=accounting,
            judgment=None,
            run_id=RUN_ID,
        )


@pytest.mark.parametrize("returned_model", ["claude-haiku-4-5-20251001", "unexpected/model", None])
def test_judge_model_anomaly_keeps_raw_tokens_but_unknown_pricing_and_full_hold(
    value: JudgeInput,
    returned_model: str | None,
) -> None:
    test = harness(value)
    test.port.response = message(value).model_copy(update={"model": returned_model})
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    failure = caught.value
    assert failure.code == "provider_error"
    accounting = failure.accounting
    assert accounting.cost.actual_cost_usd is None
    assert not accounting.cost.usage_complete
    assert accounting.cost.input_tokens is None and accounting.cost.output_tokens is None
    assert accounting.cost.reserved_cost_usd == Decimal("0.058")
    assert test.budget.stopped and test.budget.committed_usd == Decimal("0.058")
    assert len(test.port.counts) == len(test.port.creates) == 1
    generation = next(
        record for record in accounting.records if record.provider_operation == "generation"
    )
    assert generation.response_code == "provider_error"
    assert generation.returned_model_id == (
        returned_model if returned_model == "claude-haiku-4-5-20251001" else None
    )
    raw_response = test.records()[-1].value
    assert raw_response.get("model") == returned_model
    assert raw_response["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    test.verify(accounting)
    with pytest.raises(ValueError, match="model.*unverified"):
        test.verify(accounting, result(value))


@pytest.mark.parametrize(
    "has_citations,citation_support",
    [(True, "not_applicable"), (False, "supported"), (False, "unsupported")],
)
def test_billed_judge_rejects_inconsistent_citation_applicability_by_statement_id(
    value: JudgeInput,
    has_citations: bool,
    citation_support: str,
) -> None:
    if not has_citations:
        value = replace(
            value,
            response=value.response.model_copy(
                update={
                    "statements": [
                        item.model_copy(update={"citation_ids": []})
                        for item in value.response.statements
                    ],
                    "citations": [],
                }
            ),
            cited_evidence=(),
        )
    payload = result(value).model_dump(mode="json")
    if not has_citations:
        for statement in payload["statements"]:
            statement["citation_support"] = "not_applicable"
    next(item for item in payload["statements"] if item["id"] == "s2")["citation_support"] = (
        citation_support
    )
    payload["statements"].reverse()
    test = harness(value)
    test.port.response = message(
        value, content=[{"type": "text", "text": json.dumps(wire_payload(payload))}]
    )
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "invalid_generated_output"
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd == Decimal("0.003")
    assert accounting.cost.usage_complete
    assert test.budget.committed_usd == Decimal("0.003")
    assert len(test.port.creates) == len(test.port.counts) == 1
    assert accounting.records[-1].response_code == "invalid_generated_output"
    assert json.loads(test.records()[-1].value["content"][0]["text"]) == wire_payload(payload)
    test.verify(accounting)


@pytest.mark.parametrize("policy", ["S07", "S01", "S08"])
def test_fixed_smoke_rejects_policy_flags_in_otherwise_expected_judgment(
    value: JudgeInput,
    policy: str,
) -> None:
    payload = result(value).model_dump(mode="json")
    payload["policy_violations"] = [policy]
    judgment = JudgeResult.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError):
        validate_smoke_result(value, judgment)


def test_baseline_empty_citations_accept_only_not_applicable_without_changing_factual_score(
    value: JudgeInput,
) -> None:
    value = replace(
        value,
        response=value.response.model_copy(
            update={
                "statements": [
                    item.model_copy(update={"citation_ids": []})
                    for item in value.response.statements
                ],
                "citations": [],
            }
        ),
        cited_evidence=(),
    )
    payload = result(value).model_dump(mode="json")
    for item in payload["statements"]:
        item["citation_support"] = "not_applicable"
    test = harness(value)
    test.port.response = message(
        value, content=[{"type": "text", "text": json.dumps(wire_payload(payload))}]
    )
    evaluated = test.run()
    assert evaluated.judgment.statements[-1].factual == "contradicted"
    assert all(item.citation_support == "not_applicable" for item in evaluated.judgment.statements)
    assert evaluated.cost.actual_cost_usd == Decimal("0.003")
    test.verify(evaluated, evaluated.judgment)


@pytest.mark.parametrize("citation_support", ["supported", "unsupported"])
def test_cited_factual_contradiction_does_not_mechanically_assign_citation_score(
    value: JudgeInput,
    citation_support: str,
) -> None:
    payload = result(value).model_dump(mode="json")
    statement = next(item for item in payload["statements"] if item["id"] == "s3")
    assert statement["factual"] == "contradicted"
    statement["citation_support"] = citation_support
    payload["statements"].reverse()
    test = harness(value)
    test.port.response = message(
        value, content=[{"type": "text", "text": json.dumps(wire_payload(payload))}]
    )
    evaluated = test.run()
    payload["statements"].reverse()
    assert evaluated.judgment.model_dump(mode="json") == payload
    test.verify(evaluated, evaluated.judgment)


def test_policy_flags_remain_original_judgments_instead_of_being_silently_removed(
    value: JudgeInput,
) -> None:
    payload = result(value).model_dump(mode="json")
    payload["policy_violations"] = ["S07"]
    test = harness(value)
    test.port.response = message(
        value, content=[{"type": "text", "text": json.dumps(wire_payload(payload))}]
    )
    evaluated = test.run()
    assert evaluated.judgment.policy_violations == ["S07"]
    assert json.loads(test.records()[-1].value["content"][0]["text"])["policy_violations"] == [
        "S07"
    ]
    test.verify(evaluated, evaluated.judgment)
    with pytest.raises(ValueError):
        validate_smoke_result(value, evaluated.judgment)


def sized_input(value: JudgeInput, claim_count: int, statement_count: int) -> JudgeInput:
    claims = tuple(
        value.required_claims[0].model_copy(update={"id": f"c{index}"})
        for index in range(1, claim_count + 1)
    )
    statements = [
        value.response.statements[(index - 1) % len(value.response.statements)].model_copy(
            update={"id": f"s{index}"}
        )
        for index in range(1, statement_count + 1)
    ]
    identifiers = {identifier for item in statements for identifier in item.citation_ids}
    return replace(
        value,
        required_claims=claims,
        response=value.response.model_copy(
            update={
                "statements": statements,
                "citations": [item for item in value.response.citations if item.id in identifiers],
            }
        ),
        cited_evidence=tuple(item for item in value.cited_evidence if item.id in identifiers),
    )


def resolved(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        assert node["$ref"].startswith("#/$defs/")
        node = schema["$defs"][node["$ref"].split("/")[-1]]
    return node


def assert_closed_required_objects(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            assert len(node["required"]) == len(set(node["required"]))
        assert "const" not in node and "default" not in node
        for child in node.values():
            assert_closed_required_objects(child)
    elif isinstance(node, list):
        for child in node:
            assert_closed_required_objects(child)


@pytest.mark.parametrize(
    "claim_count,statement_count",
    [(1, 1), (1, 4), (3, 4), (4, 3), (4, 4), (1, 6), (3, 6), (4, 6)],
)
def test_independent_required_maps_bind_count_generation_and_lossless_mapping(
    value: JudgeInput, claim_count: int, statement_count: int
) -> None:
    value = sized_input(value, claim_count, statement_count)
    expected = result(value)
    output = wire_payload(expected)
    output["required_claims"] = dict(reversed(list(output["required_claims"].items())))
    output["statements"] = dict(reversed(list(output["statements"].items())))
    raw_text = json.dumps(dict(reversed(list(output.items()))), indent=2)
    test = harness(value)
    test.port.response = message(value, content=[{"type": "text", "text": raw_text}])
    evaluated = test.run()
    assert evaluated.judgment == expected
    schema = test.port.creates[0]["output_config"]["format"]["schema"]
    assert schema == test.port.counts[0]["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {
        "required_claims",
        "statements",
        "false_pass",
        "policy_violations",
    }
    assert_closed_required_objects(schema)
    original = JudgeResult.model_json_schema()
    for field_name, ids, definition, leaf_keys in (
        (
            "required_claims",
            [item.id for item in value.required_claims],
            "JudgeClaimResult",
            {"result"},
        ),
        (
            "statements",
            [item.id for item in value.response.statements],
            "JudgeStatementResult",
            {"factual", "citation_support"},
        ),
    ):
        container = resolved(schema, schema["properties"][field_name])
        assert container["type"] == "object"
        assert set(container["properties"]) == set(container["required"]) == set(ids)
        for node in container["properties"].values():
            leaf = resolved(schema, node)
            assert set(leaf["properties"]) == leaf_keys
            assert set(leaf["required"]) == leaf_keys
            for key in leaf_keys:
                assert (
                    leaf["properties"][key]["enum"]
                    == original["$defs"][definition]["properties"][key]["enum"]
                )
    assert test.records()[-1].value["content"][0]["text"] == raw_text
    test.verify(evaluated, expected)


@pytest.mark.parametrize("field_name", ["required_claims", "statements"])
@pytest.mark.parametrize(
    "identifiers", [[], ["same", "same"], [""], [" "], [None], [1], [True], [{}]]
)
def test_wire_schema_rejects_invalid_source_ids_before_dispatch(
    field_name: str, identifiers: list[object], value: JudgeInput
) -> None:
    packet = build_judge_packet(value)
    packet[field_name] = [{"id": identifier} for identifier in identifiers]
    with pytest.raises(ValueError):
        judge_schema(packet)
    test = harness(value)
    with pytest.raises(ValueError):
        JudgeRecorder(
            test.port, packet=packet, context=test.context, run_id=RUN_ID, stream=StringIO()
        ).prepared({}, "count_tokens")
    assert test.port.counts == test.port.creates == []


@pytest.mark.parametrize("invalid", ["empty", "duplicate"])
def test_invalid_actual_claim_ids_never_reach_count_or_generation(
    value: JudgeInput, invalid: str
) -> None:
    claims = () if invalid == "empty" else (value.required_claims[0], value.required_claims[0])
    test = harness(value)
    test.value = replace(value, required_claims=claims)
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert not test.port.counts and not test.port.creates
    assert caught.value.accounting.cost.actual_cost_usd == 0
    assert caught.value.accounting.cost.reserved_cost_usd == 0


def test_wire_schemas_are_fresh_arm_independent_and_do_not_expose_legal_text(
    value: JudgeInput,
) -> None:
    from rent_navigator.eval import judge as module

    packet = build_judge_packet(value)
    original = deepcopy(packet)
    template = deepcopy(module._wire_schema_template())
    base = JudgeResult.model_json_schema()
    identity = judge_config_hash()
    first = judge_schema(packet)
    assert packet == original
    packet["required_claims"][0]["id"] = "different.technical_id"
    second = judge_schema(packet)
    assert first != second and first == judge_schema(original)
    second["properties"]["required_claims"]["required"].clear()
    assert judge_schema(packet)["properties"]["required_claims"]["required"]
    assert module._wire_schema_template() == template
    assert JudgeResult.model_json_schema() == base
    assert judge_config_hash() == identity
    baseline = replace(
        value,
        response=value.response.model_copy(
            update={
                "statements": [
                    item.model_copy(update={"citation_ids": []})
                    for item in value.response.statements
                ],
                "citations": [],
            }
        ),
        cited_evidence=(),
    )
    assert judge_schema(build_judge_packet(baseline)) == first
    serialized = json.dumps(first)
    for secret in [
        str(value.response.attempt_id),
        str(value.response.trace_id),
        value.response.disclaimer,
    ]:
        assert secret not in serialized
    for text in [item.text for item in value.required_claims] + [
        item.text for item in value.response.statements
    ]:
        assert text not in serialized
    assert '"arm"' not in serialized and '"case_id"' not in serialized


@pytest.mark.parametrize("changed_part", ["wire_policy", "grade_schema", "structural_rubric"])
def test_global_identity_binds_wire_policy_grade_definitions_and_rubric(
    monkeypatch: pytest.MonkeyPatch, changed_part: str
) -> None:
    from rent_navigator.eval import judge as module

    identity = judge_config_hash()
    if changed_part == "wire_policy":
        monkeypatch.setattr(module, "_WIRE_POLICY", {**module._WIRE_POLICY, "version": "different"})
    elif changed_part == "grade_schema":
        template = deepcopy(module._wire_schema_template())
        grade_definition = next(
            definition
            for definition in template["$defs"].values()
            if "result" in definition.get("properties", {})
        )
        grade_definition["properties"]["result"]["enum"].append("different_grade")
        monkeypatch.setattr(module, "_wire_schema_template", lambda: deepcopy(template))
    else:
        monkeypatch.setattr(module, "JUDGE_SYSTEM", JUDGE_SYSTEM + "\nDifferent wire contract.")
    assert judge_config_hash() != identity


def test_lossless_mapping_uses_trusted_claim_order_and_preserves_all_grades(
    value: JudgeInput,
) -> None:
    value = sized_input(value, 3, 4)
    value = replace(value, required_claims=tuple(reversed(value.required_claims)))
    payload = result(value).model_dump(mode="json")
    for item, grade in zip(
        payload["required_claims"], ("met", "missing", "contradicted"), strict=True
    ):
        item["result"] = grade
    for item, grade in zip(
        payload["statements"],
        ("supported", "unsupported", "contradicted", "supported"),
        strict=True,
    ):
        item["factual"] = grade
        item["citation_support"] = "unsupported" if item["id"] in {"s2", "s3"} else "supported"
    payload["false_pass"] = False
    payload["policy_violations"] = ["S08", "S01", "S07"]
    expected = JudgeResult.model_validate_json(json.dumps(payload))
    output = wire_payload(expected)
    output["required_claims"] = dict(reversed(list(output["required_claims"].items())))
    output["statements"] = dict(reversed(list(output["statements"].items())))
    raw_text = json.dumps(output, indent=3).replace('"s1"', '"s\\u0031"')
    test = harness(value)
    test.port.response = message(value, content=[{"type": "text", "text": raw_text}])
    evaluated = test.run()
    assert evaluated.judgment == expected
    assert test.records()[-1].value["content"][0]["text"] == raw_text
    test.verify(evaluated, expected)


@pytest.mark.parametrize("field_name", ["required_claims", "statements"])
@pytest.mark.parametrize("mutation", ["extra", "missing", "case", "whitespace", "null", "array"])
def test_private_wire_requires_exact_unmodified_container_keys(
    value: JudgeInput, field_name: str, mutation: str
) -> None:
    output = wire_payload(result(value))
    rows = output[field_name]
    key = next(iter(rows))
    if mutation == "extra":
        rows["extra"] = deepcopy(rows[key])
    elif mutation == "missing":
        rows.pop(key)
    elif mutation in {"case", "whitespace"}:
        rows[key.upper() if mutation == "case" else key + " "] = rows.pop(key)
    elif mutation == "null":
        output[field_name] = None
    else:
        output[field_name] = result(value).model_dump(mode="json")[field_name]
    with pytest.raises(ProviderFailure) as caught:
        parse_judgment(
            message(value, content=[{"type": "text", "text": json.dumps(output)}]), value
        )
    assert caught.value.code == "invalid_generated_output"


@pytest.mark.parametrize(
    "mutation",
    [
        "root_extra",
        "root_missing",
        "claim_extra",
        "claim_missing",
        "claim_id",
        "claim_null",
        "claim_string",
        "claim_enum",
        "claim_bool",
        "statement_extra",
        "statement_missing_factual",
        "statement_missing_citation",
        "statement_id",
        "statement_null",
        "statement_array",
        "factual_enum",
        "factual_bool",
        "citation_enum",
        "citation_null",
        "false_string",
        "false_integer",
        "false_null",
        "policy_string",
        "policy_null",
        "policy_invalid",
        "policy_duplicate",
        "policy_bool",
    ],
)
def test_private_wire_rejects_strict_root_leaf_and_score_type_violations(
    value: JudgeInput, mutation: str
) -> None:
    output = wire_payload(result(value))
    claim = next(iter(output["required_claims"]))
    leaf = output["required_claims"][claim]
    statement = output["statements"]["s1"]
    if mutation == "root_extra":
        output["explanation"] = SENTINEL
    elif mutation == "root_missing":
        output.pop("policy_violations")
    elif mutation == "claim_extra":
        leaf["explanation"] = SENTINEL
    elif mutation == "claim_missing":
        leaf.pop("result")
    elif mutation == "claim_id":
        leaf["id"] = claim
    elif mutation in {"claim_null", "claim_string"}:
        output["required_claims"][claim] = None if mutation == "claim_null" else "met"
    elif mutation in {"claim_enum", "claim_bool"}:
        leaf["result"] = "MET" if mutation == "claim_enum" else True
    elif mutation == "statement_extra":
        statement["explanation"] = SENTINEL
    elif mutation.startswith("statement_missing_"):
        statement.pop("factual" if mutation.endswith("factual") else "citation_support")
    elif mutation == "statement_id":
        statement["id"] = "s1"
    elif mutation in {"statement_null", "statement_array"}:
        output["statements"]["s1"] = None if mutation.endswith("null") else []
    elif mutation in {"factual_enum", "factual_bool"}:
        statement["factual"] = "SUPPORTED" if mutation.endswith("enum") else True
    elif mutation in {"citation_enum", "citation_null"}:
        statement["citation_support"] = "SUPPORTED" if mutation.endswith("enum") else None
    elif mutation.startswith("false_"):
        output["false_pass"] = {"false_string": "true", "false_integer": 1, "false_null": None}[
            mutation
        ]
    else:
        output["policy_violations"] = {
            "policy_string": "S01",
            "policy_null": None,
            "policy_invalid": ["S09"],
            "policy_duplicate": ["S01", "S01"],
            "policy_bool": [True],
        }[mutation]
    with pytest.raises(ProviderFailure) as caught:
        parse_judgment(
            message(value, content=[{"type": "text", "text": json.dumps(output)}]), value
        )
    assert caught.value.code == "invalid_generated_output"
    assert SENTINEL not in str(caught.value)


# Handwritten fragments preserve duplicate JSON keys that a Python dict cannot represent.
_DUPLICATE_FRAGMENTS = [
    ("false_pass", '"false_pass":true,"false_pass":true'),
    ("false_pass", '"false_pass":false,"false_pass":true'),
    ("false_pass", '"false_pass":true,"false_pa\\u0073s":true'),
    (
        "required_claims",
        '"required_claims":{"c1":{"result":"met"}},"required_claims":{"c1":{"result":"met"}}',
    ),
    (
        "statements",
        '"statements":{"s1":{"factual":"supported","citation_support":"supported"}},"statements":{"s1":{"factual":"supported","citation_support":"supported"}}',
    ),
    ("policy_violations", '"policy_violations":[],"policy_violations":[]'),
    ("required_claims", '"required_claims":{"c1":{"result":"met"},"c1":{"result":"met"}}'),
    ("required_claims", '"required_claims":{"c1":{"result":"missing"},"c1":{"result":"met"}}'),
    ("required_claims", '"required_claims":{"c1":{"result":"met"},"c\\u0031":{"result":"met"}}'),
    (
        "statements",
        '"statements":{"s1":{"factual":"supported","citation_support":"supported"},"s1":{"factual":"supported","citation_support":"supported"}}',
    ),
    (
        "statements",
        '"statements":{"s1":{"factual":"unsupported","citation_support":"supported"},"s\\u0031":{"factual":"supported","citation_support":"supported"}}',
    ),
    ("required_claims", '"required_claims":{"c1":{"result":"met","result":"met"}}'),
    ("required_claims", '"required_claims":{"c1":{"result":"missing","res\\u0075lt":"met"}}'),
    (
        "statements",
        '"statements":{"s1":{"factual":"supported","factual":"supported","citation_support":"supported"}}',
    ),
    (
        "statements",
        '"statements":{"s1":{"factual":"unsupported","fact\\u0075al":"supported","citation_support":"supported"}}',
    ),
    (
        "statements",
        '"statements":{"s1":{"factual":"supported","citation_support":"supported","citation_s\\u0075pport":"supported"}}',
    ),
]


@pytest.mark.parametrize("field_name,fragment", _DUPLICATE_FRAGMENTS)
def test_recursive_duplicate_json_keys_fail_billed_and_preserve_original_raw(
    value: JudgeInput, field_name: str, fragment: str
) -> None:
    value = sized_input(value, 1, 1)
    members = {
        "required_claims": '"required_claims":{"c1":{"result":"met"}}',
        "statements": '"statements":{"s1":{"factual":"supported","citation_support":"supported"}}',
        "false_pass": '"false_pass":true',
        "policy_violations": '"policy_violations":[]',
    }
    members[field_name] = fragment
    raw_text = " {\n  " + ",\n  ".join(members.values()) + "\n}\n"
    test = harness(value)
    test.port.response = message(value, content=[{"type": "text", "text": raw_text}])
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    failure = caught.value
    assert failure.code == "invalid_generated_output"
    assert failure.accounting.cost.usage_complete
    assert failure.accounting.cost.actual_cost_usd == Decimal("0.003")
    assert test.budget.committed_usd == Decimal("0.003")
    assert len(test.port.counts) == len(test.port.creates) == 1
    assert test.records()[-1].value["content"][0]["text"] == raw_text
    assert raw_text not in test.metadata.getvalue()
    test.verify(failure.accounting)
    with pytest.raises(ValueError):
        test.verify(failure.accounting, result(value))
    # Even a complete success trace cannot make duplicate-key raw text prove its result.
    successful = harness(value)
    evaluation = successful.run()
    records = successful.records()
    records[-1].value["content"][0]["text"] = raw_text
    with pytest.raises(ValueError):
        verify_judge_records(
            records,
            value=value,
            context=successful.context,
            accounting=evaluation,
            judgment=evaluation.judgment,
            run_id=RUN_ID,
        )


@pytest.mark.parametrize(
    "raw_text",
    [
        "null",
        "[]",
        "true",
        '"string"',
        '{"required_claims":{},"statements":{},"false_pass":NaN,"policy_violations":[]}',
        '{"required_claims":{},"statements":{},"false_pass":Infinity,"policy_violations":[]}',
        '{"required_claims":{},"statements":{},"false_pass":-Infinity,"policy_violations":[]}',
    ],
)
def test_non_json_constants_and_non_object_roots_are_rejected(
    value: JudgeInput, raw_text: str
) -> None:
    with pytest.raises(ProviderFailure) as caught:
        parse_judgment(message(value, content=[{"type": "text", "text": raw_text}]), value)
    assert caught.value.code == "invalid_generated_output"


def test_trailing_text_and_legacy_arrays_have_no_fallback(value: JudgeInput) -> None:
    for raw_text in (
        json.dumps(wire_payload(result(value))) + " {}",
        result(value).model_dump_json(),
    ):
        with pytest.raises(ProviderFailure) as caught:
            parse_judgment(message(value, content=[{"type": "text", "text": raw_text}]), value)
        assert caught.value.code == "invalid_generated_output"


@pytest.mark.parametrize(
    "location", ["root", "required_claims", "statements", "claim_leaf", "statement_leaf"]
)
@pytest.mark.parametrize("mutation", ["missing_required", "open", "extra_property"])
def test_recorder_and_raw_replay_independently_reject_schema_shape_tampering(
    value: JudgeInput, location: str, mutation: str
) -> None:
    test = harness(value)
    evaluation = test.run()
    recorder = JudgeRecorder(
        test.port,
        packet=build_judge_packet(value),
        context=test.context,
        run_id=RUN_ID,
        stream=StringIO(),
    )
    records = test.records()
    for record in records:
        if record.event != "request":
            continue
        schema = record.value["output_config"]["format"]["schema"]
        node = schema
        if location != "root":
            name = (
                "required_claims"
                if location == "claim_leaf"
                else "statements"
                if location == "statement_leaf"
                else location
            )
            node = resolved(schema, schema["properties"][name])
            if location.endswith("leaf"):
                node = resolved(schema, next(iter(node["properties"].values())))
        if mutation == "missing_required":
            node["required"].pop()
        elif mutation == "open":
            node["additionalProperties"] = True
        else:
            node["properties"]["extra"] = {"type": "string"}
        with pytest.raises(ValueError):
            recorder.prepared(record.value, record.operation)
    with pytest.raises(ValueError):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluation,
            judgment=evaluation.judgment,
            run_id=RUN_ID,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "packet_ids",
        "packet_text",
        "raw_text",
        "normalized_result",
        "generation_only_schema",
        "legacy_schema",
    ],
)
def test_raw_evidence_cannot_substitute_packet_original_text_or_normalized_result(
    value: JudgeInput, mutation: str
) -> None:
    from anthropic import transform_schema

    test = harness(value)
    evaluation = test.run()
    records = test.records()
    judgment = evaluation.judgment
    if mutation.startswith("packet"):
        for record in records:
            if record.event == "request":
                packet = json.loads(record.value["messages"][0]["content"])
                packet["required_claims"][0]["id" if mutation == "packet_ids" else "text"] = (
                    "tampered"
                )
                record.value["messages"][0]["content"] = json.dumps(packet)
    elif mutation == "raw_text":
        records[-1].value["content"][0]["text"] = json.dumps(
            wire_payload(result(value).model_copy(update={"false_pass": False}))
        )
    elif mutation == "normalized_result":
        judgment = result(value).model_copy(update={"false_pass": False})
    elif mutation == "generation_only_schema":
        records[2].value["output_config"]["format"]["schema"]["additionalProperties"] = True
    else:
        for record in records:
            if record.event == "request":
                record.value["output_config"]["format"]["schema"] = transform_schema(
                    JudgeResult.model_json_schema()
                )
    with pytest.raises(ValueError):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluation,
            judgment=judgment,
            run_id=RUN_ID,
        )


def test_private_response_text_is_decoded_once_before_mapping(
    value: JudgeInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_loads = json.loads
    raw_text = json.dumps(wire_payload(result(value)), indent=4)
    calls: list[str] = []

    def observed_loads(text: str, **kwargs: Any) -> Any:
        calls.append(text)
        assert callable(kwargs.get("object_pairs_hook"))
        assert callable(kwargs.get("parse_constant"))
        return original_loads(text, **kwargs)

    monkeypatch.setattr(json, "loads", observed_loads)
    parsed = parse_judgment(message(value, content=[{"type": "text", "text": raw_text}]), value)
    assert parsed == result(value)
    assert calls == [raw_text]


@pytest.mark.parametrize(
    "missing", ["required_claims", "statements", "false_pass", "policy_violations"]
)
def test_each_root_member_is_required_without_defaults(value: JudgeInput, missing: str) -> None:
    output = wire_payload(result(value))
    output.pop(missing)
    with pytest.raises(ProviderFailure) as caught:
        parse_judgment(
            message(value, content=[{"type": "text", "text": json.dumps(output)}]), value
        )
    assert caught.value.code == "invalid_generated_output"


@pytest.mark.parametrize("has_citations", [True, False])
def test_six_statement_judge_preserves_full_600_character_qualifications_through_replay(
    value: JudgeInput, has_citations: bool
) -> None:
    text = (
        "The calculated notice interval is shorter than the minimum in the synthetic example, "
        "so the notice check fails on the confirmed dates. "
        "This explains only the check that was actually performed; the tool's other checks "
        "remain separate, and the statement does not determine whether the increase is valid "
        "in every respect. The result depends on the service method and dates supplied by the "
        "tenant. It does not establish what happened when the notice was delivered or settle "
        "facts that were not confirmed. This conclusion is limited to the ordinary rent-increase "
        "scenario covered by the supplied evidence."
    )
    qualification = (
        "This conclusion is limited to the ordinary rent-increase scenario covered by the "
        "supplied evidence."
    )
    assert len(text) == 600 and text.endswith(qualification)
    value = sized_input(value, 3, 6)
    statements = [
        statement.model_copy(
            update={"text": text, "citation_ids": statement.citation_ids if has_citations else []}
        )
        for statement in value.response.statements
    ]
    value = replace(
        value,
        response=value.response.model_copy(
            update={
                "statements": statements,
                "answer": "\n".join(item.text for item in statements),
                "citations": value.response.citations if has_citations else [],
            }
        ),
        cited_evidence=value.cited_evidence if has_citations else (),
    )
    payload = result(value).model_dump(mode="json")
    if not has_citations:
        for statement_result in payload["statements"]:
            statement_result["citation_support"] = "not_applicable"
    expected = JudgeResult.model_validate_json(json.dumps(payload))
    wire = wire_payload(expected)
    wire["statements"] = dict(reversed(list(wire["statements"].items())))
    test = harness(value)
    test.port.response = message(value, content=[{"type": "text", "text": json.dumps(wire)}])
    evaluated = test.run()
    assert evaluated.judgment == expected
    assert [item.id for item in evaluated.judgment.statements] == [f"s{i}" for i in range(1, 7)]
    assert test.port.counts[0]["messages"] == test.port.creates[0]["messages"]
    assert test.port.counts[0]["output_config"] == test.port.creates[0]["output_config"]
    packet = json.loads(test.port.counts[0]["messages"][0]["content"])
    assert packet == build_judge_packet(value)
    assert [item["text"] for item in packet["statements"]] == [text] * 6
    test.verify(evaluated, expected)
    # A matching count/create rewrite still cannot remove qualifications from trusted input.
    records = test.records()
    for record in records:
        if record.event == "request":
            altered = json.loads(record.value["messages"][0]["content"])
            assert altered["statements"][5]["text"].endswith(qualification)
            altered["statements"][5]["text"] = text.removesuffix(qualification)
            record.value["messages"][0]["content"] = json.dumps(altered)
    with pytest.raises(ValueError):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluated,
            judgment=expected,
            run_id=RUN_ID,
        )
