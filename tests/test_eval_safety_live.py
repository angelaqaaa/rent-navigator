"""Synthetic packets exercise actual safety execution and pure artifact rechecks."""

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from uuid import UUID

import pytest
from anthropic.types import Message
from eval_fixtures import TOY_HASH, TOY_RUN_ID, TOY_SOURCE_SHA
from test_agent import final_message, message, selection
from test_eval_runner import JUDGE_CONFIG, Clock, ScriptedMessages, SyntheticJudge, _harness

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.critical import critical_gold
from rent_navigator.eval.recording import (
    RawProviderRecord,
    SyntheticAllowlist,
    SyntheticRecorder,
    verify_synthetic_records,
)
from rent_navigator.eval.runner import AttemptIdentity
from rent_navigator.eval.safety_execution import (
    SECURITY_IDS,
    SecurityOutcome,
    run_security_attempt,
    verify_security_outcome,
)
from rent_navigator.index import SearchHit
from rent_navigator.models import ExtractRequest, NoticeRequest, RentRequest
from rent_navigator.provider import ProviderFailure, SpendLedger
from rent_navigator.security_cases import SecurityCase, load_security_cases


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@dataclass
class SafetyHarness:
    corpus: Corpus
    case: SecurityCase
    clock: Clock
    fake: ScriptedMessages
    judge: SyntheticJudge
    raw: StringIO
    metadata: StringIO

    def run(self) -> SecurityOutcome:
        return asyncio.run(
            run_security_attempt(
                self.case,
                identity=AttemptIdentity(
                    run_id=UUID(TOY_RUN_ID), source_sha=TOY_SOURCE_SHA, gold_hash=TOY_HASH
                ),
                corpus=self.corpus,
                messages=self.fake,
                budget=SpendLedger(Decimal("1")),
                allowlist=SyntheticAllowlist.from_fixtures((self.case,)),
                retrieve=lambda _: tuple(
                    SearchHit(chunk, -float(i + 1))
                    for i, chunk in enumerate(self.corpus.chunks[:5])
                ),
                metadata=self.metadata,
                raw_provider=self.raw,
                judge=self.judge,
                judge_config_hash=JUDGE_CONFIG,
                clock=self.clock,
            )
        )

    def verify(self, outcome: SecurityOutcome) -> None:
        decision = verify_security_outcome(
            self.case,
            outcome.row,
            outcome.records,
            tuple(
                RawProviderRecord.model_validate_json(line)
                for line in self.raw.getvalue().splitlines()
            ),
            self.corpus,
            outcome.judge_accounting,
        )
        assert decision.safe_pass == outcome.row.safe_pass


def safety(corpus: Corpus, case_id: str) -> SafetyHarness:
    case = next(item for item in load_security_cases(corpus=corpus) if item.id == case_id)
    assert not isinstance(case.request, ExtractRequest)
    replies = []
    if isinstance(case.request, NoticeRequest | RentRequest):
        replies.append(selection(case.request))
    replies.append(final_message([corpus.chunks[0].id]))
    clock = Clock()
    return SafetyHarness(
        corpus,
        case,
        clock,
        ScriptedMessages(replies, clock),
        SyntheticJudge(clock),
        StringIO(),
        StringIO(),
    )


@pytest.mark.parametrize("case_id", SECURITY_IDS)
def test_seven_attempts_use_actual_boundaries_and_rederive(corpus: Corpus, case_id: str) -> None:
    harness = safety(corpus, case_id)
    outcome = harness.run()
    assert outcome.row.safe_pass
    assert outcome.row.disposition == "answered_safe"
    assert outcome.row.usage_complete
    assert len(harness.judge.inputs) == 1
    assert outcome.row.latency_ms == pytest.approx(600 if case_id in {"S04", "S07"} else 300)
    packet = harness.judge.inputs[0]
    assert packet.expected_tool_result is None
    assert packet.safety_context is not None
    assert packet.safety_context.policy_claims
    assert outcome.row.blocked_native_proposals == []
    if case_id in {"S04", "S07"}:
        assert isinstance(harness.case.request, RentRequest)
        assert outcome.row.actual_tool_args == harness.case.request.facts
        assert outcome.row.actual_tool_result is not None
        expected = "fails_checked_rules" if case_id == "S04" else "unsupported"
        assert outcome.row.actual_tool_result.status == expected
        assert packet.safety_context.expected_tool_status == expected
        assert packet.safety_context.confirmed_facts == harness.case.request.facts
    for request in harness.fake.counts + harness.fake.creates:
        initial = json.loads(request["messages"][0]["content"])
        assert initial["evidence"] == [
            {"id": chunk.id, "heading": chunk.heading, "text": chunk.text}
            for chunk in corpus.chunks[:5]
        ]
        if case_id in {"S04", "S05"}:
            assert initial["untrusted_retrieved_text"] == harness.case.injected_retrieved_text
            assert harness.case.injected_retrieved_text not in request["system"]
        else:
            assert "untrusted_retrieved_text" not in initial
    harness.verify(outcome)


@pytest.mark.parametrize("case_id", ["S01", "S05", "S08"])
def test_blocked_question_tool_proposal_is_safe_but_never_executed(
    corpus: Corpus, case_id: str
) -> None:
    harness = safety(corpus, case_id)
    other = safety(corpus, "S04").case
    assert isinstance(other.request, RentRequest)
    harness.fake.responses = [selection(other.request)]
    outcome = harness.run()
    assert outcome.row.safe_pass and outcome.row.disposition == "blocked_output"
    assert len(outcome.row.blocked_native_proposals) == 1
    assert outcome.row.actual_tool_args is None and outcome.row.actual_tool_result is None
    assert not harness.judge.inputs
    harness.verify(outcome)


def test_blocked_changed_confirmed_fact_is_safe_and_separate(corpus: Corpus) -> None:
    harness = safety(corpus, "S04")
    first_response = harness.fake.responses[0]
    assert isinstance(first_response, Message)
    block = first_response.model_dump(mode="json")["content"][0]
    block["input"]["proposed_cents"] = 204200
    harness.fake.responses = [message([block], stop="tool_use")]
    outcome = harness.run()
    assert outcome.row.safe_pass and outcome.row.disposition == "blocked_output"
    assert outcome.row.actual_tool_args is None and outcome.row.actual_tool_result is None
    assert outcome.row.blocked_native_proposals[0].block["input"]["proposed_cents"] == 204200
    harness.verify(outcome)


@pytest.mark.parametrize("case_id", ["S02", "S05"])
def test_forged_citation_safely_rejected_without_judge(corpus: Corpus, case_id: str) -> None:
    harness = safety(corpus, case_id)
    harness.fake.responses = [final_message(["0" * 64])]
    outcome = harness.run()
    assert outcome.row.safe_pass and outcome.row.disposition == "blocked_output"
    assert not harness.judge.inputs
    harness.verify(outcome)


def test_fixed_application_refusal_has_no_judge(corpus: Corpus) -> None:
    harness = safety(corpus, "S08")
    harness.fake.responses = [final_message([], reason="out_of_scope")]
    outcome = harness.run()
    assert outcome.row.safe_pass and outcome.row.disposition == "fixed_refusal"
    assert not harness.judge.inputs
    harness.verify(outcome)


@pytest.mark.parametrize(
    "error",
    [ProviderFailure("provider_error"), TimeoutError(), ProviderFailure("deadline_exceeded")],
)
def test_infrastructure_is_never_safe(corpus: Corpus, error: Exception) -> None:
    harness = safety(corpus, "S01")
    harness.fake.responses = [error]
    outcome = harness.run()
    assert not outcome.row.safe_pass
    assert outcome.row.disposition == "incomplete"
    assert "infrastructure_failure" in outcome.reasons
    assert outcome.row.serving_cost_usd is None
    harness.verify(outcome)


def test_preflight_failure_after_no_actor_response_is_not_safe(corpus: Corpus) -> None:
    harness = safety(corpus, "S01")
    harness.fake.estimates = [99999]
    outcome = harness.run()
    assert not outcome.row.safe_pass
    assert not harness.fake.creates
    assert outcome.row.serving_cost_usd == Decimal(0)
    harness.verify(outcome)


@pytest.mark.parametrize("failure", ["missing_usage", "invalid_coverage", "error"])
def test_incomplete_judge_blocks_safety(corpus: Corpus, failure: str) -> None:
    harness = safety(corpus, "S01")
    if failure == "error":
        harness.judge.error = RuntimeError("synthetic private exception")
    else:
        setattr(harness.judge, failure, True)
    outcome = harness.run()
    assert not outcome.row.safe_pass and outcome.row.disposition == "incomplete"
    assert "synthetic private exception" not in harness.metadata.getvalue()
    harness.verify(outcome)


def test_changed_security_fixture_cannot_enable_recording(corpus: Corpus) -> None:
    harness = safety(corpus, "S05")
    changed = harness.case.model_copy(update={"injected_retrieved_text": "undeclared content"})
    with pytest.raises(ValueError, match="unchanged packaged"):
        SyntheticRecorder(
            harness.fake,
            case=changed,
            allowlist=SyntheticAllowlist.from_fixtures([changed]),
            corpus=corpus,
            stream=StringIO(),
            run_id=UUID(TOY_RUN_ID),
            attempt_id=UUID(TOY_RUN_ID),
        )


@pytest.mark.parametrize("tamper", ["sidecar", "evidence", "system"])
def test_sidecar_and_canonical_evidence_tampering_rejected(corpus: Corpus, tamper: str) -> None:
    harness = safety(corpus, "S05")
    outcome = harness.run()
    records = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    packet = records[0].value
    initial = json.loads(packet["messages"][0]["content"])
    if tamper == "sidecar":
        initial["untrusted_retrieved_text"] += "changed"
    elif tamper == "evidence":
        initial["evidence"].pop()
    else:
        assert harness.case.injected_retrieved_text is not None
        packet["system"] += harness.case.injected_retrieved_text
    packet["messages"][0]["content"] = json.dumps(initial)
    with pytest.raises(ValueError):
        verify_synthetic_records(
            records,
            case=harness.case,
            corpus=corpus,
            arm="production",
            retrieved_ids=tuple(outcome.row.retrieved_ids),
        )


@pytest.mark.parametrize("citation_ids", [[], ["0" * 64]])
def test_rejected_gold_answer_still_has_critical_citation(
    corpus: Corpus, citation_ids: list[str]
) -> None:
    harness = _harness(corpus)
    harness.fake.responses = [final_message(citation_ids)]
    outcome = harness.run()
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    decision = critical_gold(harness.case, outcome.row, raw, corpus)
    assert decision.complete and decision.flags == ("critical_citation",)


@pytest.mark.parametrize("change", ["name", "fact", "multiplicity", "schema"])
def test_gold_tool_proposal_penalty_even_when_safely_rejected(corpus: Corpus, change: str) -> None:
    harness = _harness(corpus, "R01")
    first_response = harness.fake.responses[0]
    assert isinstance(first_response, Message)
    block = first_response.model_dump(mode="json")["content"][0]
    blocks = [block]
    if change == "name":
        block["name"] = "wrong_tool"
    elif change == "fact":
        block["input"]["current_cents"] = 200000
    elif change == "schema":
        block["input"]["current_cents"] = True
    else:
        blocks.append(dict(block, id="second"))
    harness.fake.responses = [message(blocks, stop="tool_use")]
    outcome = harness.run()
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    decision = critical_gold(harness.case, outcome.row, raw, corpus)
    assert decision.complete and decision.flags == ("critical_tool_protocol",)
    assert outcome.row.actual_tool_args is None


@pytest.mark.parametrize("case_id", ["Q01", "R01"])
def test_no_proposal_or_malformed_output_is_only_task_failure(corpus: Corpus, case_id: str) -> None:
    harness = _harness(corpus, case_id)
    harness.fake.responses = [message([{"type": "text", "text": "not structured"}])]
    outcome = harness.run()
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    decision = critical_gold(harness.case, outcome.row, raw, corpus)
    assert decision.complete and not decision.flags
    assert not critical_gold(harness.case, outcome.row, [], corpus).complete


def test_expected_rule_evidence_cannot_rescue_unexecuted_citation(corpus: Corpus) -> None:
    harness = _harness(corpus)
    # A real canonical chunk absent from actual retrieval is still unavailable.
    harness.fake.responses = [final_message([corpus.chunks[-1].id])]
    outcome = harness.run()
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    assert critical_gold(harness.case, outcome.row, raw, corpus).flags == ("critical_citation",)


def test_repeated_native_id_does_not_hide_blocked_final_proposal(corpus: Corpus) -> None:
    harness = safety(corpus, "S04")
    first = harness.fake.responses[0]
    harness.fake.responses = [first, first]
    outcome = harness.run()
    assert outcome.row.safe_pass and outcome.row.disposition == "blocked_output"
    assert outcome.row.actual_tool_result is not None
    assert len(outcome.row.blocked_native_proposals) == 1
    assert outcome.row.blocked_native_proposals[0].operation_index == 4
    harness.verify(outcome)


def test_billed_invalid_judge_keeps_cost_and_blocks_safety(corpus: Corpus) -> None:
    from rent_navigator.eval.runner import (
        JudgeAccounting,
        JudgeEvaluation,
        JudgeFailure,
        JudgeInput,
    )
    from rent_navigator.trace import TraceContext

    class InvalidBilledJudge(SyntheticJudge):
        async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation:
            result = await super().evaluate(value, context=context)
            accounting = JudgeAccounting.model_validate_json(
                result.model_dump_json(exclude={"judgment"})
            )
            raise JudgeFailure("invalid_generated_output", accounting)

    harness = safety(corpus, "S01")
    harness.judge = InvalidBilledJudge(harness.clock)
    outcome = harness.run()
    assert not outcome.row.safe_pass
    assert outcome.row.judge is None
    assert outcome.row.judge_cost_usd == Decimal("0.0007")
    assert outcome.judge_accounting is not None
    assert outcome.judge_accounting.cost.usage_complete
    harness.verify(outcome)


@pytest.mark.parametrize("field", ["assertions", "cost", "blocked", "request", "retrieved"])
def test_safety_artifact_cannot_forge_a_pass(corpus: Corpus, field: str) -> None:
    from rent_navigator.eval.models import DeterministicAssertion

    harness = safety(corpus, "S01")
    outcome = harness.run()
    row = outcome.row
    updates: dict[str, object]
    if field == "assertions":
        updates = {"deterministic_assertions": [DeterministicAssertion(id="pretend", passed=True)]}
    elif field == "cost":
        updates = {"serving_cost_usd": Decimal(0)}
    elif field == "blocked":
        updates = {
            "blocked_native_proposals": [
                {
                    "trace_id": str(row.trace_ids[0]),
                    "operation_index": 2,
                    "block": {"id": "pretend"},
                }
            ]
        }
    elif field == "request":
        updates = {"request": row.request.model_copy(update={"question": "undeclared"})}
    else:
        updates = {"retrieved_ids": []}
    changed = row.model_copy(update=updates)
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    with pytest.raises(ValueError):
        verify_security_outcome(
            harness.case, changed, outcome.records, raw, corpus, outcome.judge_accounting
        )


@pytest.mark.parametrize("violation", ["false_pass", "policy", "citation"])
def test_gold_judgment_critical_flags_are_independent(corpus: Corpus, violation: str) -> None:
    harness = _harness(corpus)
    outcome = harness.run()
    assert outcome.row.judge is not None
    judgment = outcome.row.judge
    if violation == "false_pass":
        changed = judgment.model_copy(update={"false_pass": True})
    elif violation == "policy":
        changed = judgment.model_copy(update={"policy_violations": ["S08"]})
    else:
        changed = judgment.model_copy(
            update={
                "statements": [
                    item.model_copy(update={"citation_support": "unsupported"})
                    for item in judgment.statements
                ]
            }
        )
    row = outcome.row.model_copy(update={"judge": changed})
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    assert critical_gold(harness.case, row, raw, corpus).flags == (f"critical_{violation}",)


def test_actual_rule_evidence_from_executed_result_is_available(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01")
    assert harness.case.expected_tool_result is not None
    rule_chunk = next(
        identifier
        for rule_id in harness.case.expected_tool_result.rule_ids
        for identifier in corpus.rule(rule_id).evidence_ids
        if identifier != corpus.chunks[0].id
    )
    harness.fake.responses[-1] = final_message([rule_chunk])
    outcome = harness.run()
    assert outcome.row.actual_tool_result is not None
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    check = critical_gold(harness.case, outcome.row, raw, corpus)
    assert check.complete and not check.flags


def test_preflight_error_after_actor_selection_is_not_safe_rejection(corpus: Corpus) -> None:
    harness = safety(corpus, "S04")
    harness.fake.estimates = [1000, ProviderFailure("tool_protocol_error")]
    outcome = harness.run()
    assert not outcome.row.safe_pass and outcome.row.disposition == "incomplete"
    assert "infrastructure_failure" in outcome.reasons
    assert outcome.row.actual_tool_result is not None
    harness.verify(outcome)


def test_gold_preflight_failure_cannot_masquerade_as_native_rejection(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01")
    harness.fake.estimates = [1000, ProviderFailure("tool_protocol_error")]
    outcome = harness.run()
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    decision = critical_gold(harness.case, outcome.row, raw, corpus)
    assert not decision.complete
    assert not decision.flags


@pytest.mark.parametrize("case_id", ["R02", "R04"])
def test_observed_malformed_extraction_is_complete_task_failure(
    corpus: Corpus, case_id: str
) -> None:
    from rent_navigator.models import ErrorResponse

    harness = _harness(corpus, case_id)
    harness.fake.responses = [message([{"type": "text", "text": "not structured extraction"}])]
    outcome = harness.run()
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "invalid_generated_output"
    assert outcome.row.classification == "error"
    assert outcome.row.actual_extract is None
    assert outcome.row.actual_tool_args is None
    assert outcome.row.actual_tool_result is None
    assert harness.judge is not None and not harness.judge.inputs
    raw = [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]
    assert {record.phase for record in raw} == {"extraction"}
    decision = critical_gold(harness.case, outcome.row, raw, corpus)
    assert decision.complete and not decision.flags
    assert not critical_gold(harness.case, outcome.row, raw[:-1], corpus).complete
