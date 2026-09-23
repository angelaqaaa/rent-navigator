"""Serial collection plans and attempts through the existing serving operations."""

import asyncio
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from typing import Annotated, Any, Literal, Protocol, TextIO
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from rent_navigator.agent import agent_config_hash, answer
from rent_navigator.corpus import Chunk, Corpus
from rent_navigator.eval.metrics import classify
from rent_navigator.eval.models import (
    CASE_IDS,
    Arm,
    CaseId,
    Claim,
    DeterministicAssertion,
    GoldCase,
    JudgeResult,
    ResultRow,
)
from rent_navigator.eval.recording import SyntheticAllowlist, SyntheticRecorder
from rent_navigator.extract import extract_letter, extraction_config_hash
from rent_navigator.guards import redact_text
from rent_navigator.index import SearchHit
from rent_navigator.models import (
    ASK_REQUEST_ADAPTER,
    AskResponse,
    CanonicalUUID,
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    Extraction,
    ExtractRequest,
    NoticeFacts,
    RentFacts,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolResult,
    ToolStatus,
    disclaimer_for,
)
from rent_navigator.provider import (
    Deadline,
    MessagesPort,
    ProviderAdapter,
    ProviderFailure,
    SpendBudget,
)
from rent_navigator.security_cases import SecurityCaseId
from rent_navigator.trace import (
    JUDGE_MODEL,
    PRICING_HASH,
    CostSummary,
    MetadataSink,
    ReturnedModel,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    provider_cost_totals,
)


class PlanEntry(StrictModel):
    case_id: CaseId
    arm: Arm
    repeat: Annotated[int, Field(ge=0, le=4)]


class CollectionPlan(StrictModel):
    schema_version: Literal[1]
    run_id: CanonicalUUID
    seed: Literal[42]
    warmups: list[PlanEntry]
    measured: list[PlanEntry]

    @model_validator(mode="after")
    def fixed_schedule(self) -> "CollectionPlan":
        warmups, measured = _schedule()
        if self.warmups != warmups or self.measured != measured:
            raise ValueError("Collection plan differs from the fixed protocol")
        return self


def _schedule() -> tuple[list[PlanEntry], list[PlanEntry]]:
    warmups = [
        PlanEntry(case_id=case, arm=arm, repeat=0)
        for arm in ("production", "baseline")
        for case in ("R01", "N01", "Q01")
    ]
    measured: list[PlanEntry] = []
    rng = random.Random(42)
    for repeat in range(5):
        cases = sorted(CASE_IDS)
        rng.shuffle(cases)
        for position, case_id in enumerate(cases):
            arms: tuple[Arm, Arm] = ("production", "baseline")
            if (repeat + position) % 2:
                arms = ("baseline", "production")
            measured.extend(PlanEntry(case_id=case_id, arm=arm, repeat=repeat) for arm in arms)
    return warmups, measured


def build_plan(run_id: UUID | None = None) -> CollectionPlan:
    warmups, measured = _schedule()
    return CollectionPlan(
        schema_version=1, run_id=run_id or uuid4(), seed=42, warmups=warmups, measured=measured
    )


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def config_map() -> dict[str, str]:
    return {
        "extraction": extraction_config_hash(),
        **{
            f"{mode}:{arm}": agent_config_hash(mode, arm)
            for mode in ("question", "notice", "rent")
            for arm in ("production", "baseline")
        },
    }


def row_config_hash(case: GoldCase, arm: Arm) -> str:
    return _hash(
        {
            "extraction_hash": extraction_config_hash() if case.letter else None,
            "analysis_hash": agent_config_hash(case.request.mode, arm),
        }
    )


def protocol_hash() -> str:
    warmups, measured = _schedule()
    return _hash(
        {
            "schema_version": 1,
            "seed": 42,
            "warmups": [entry.model_dump() for entry in warmups],
            "measured": [entry.model_dump() for entry in measured],
            "result_schema": ResultRow.model_json_schema(),
            "judge_schema": JudgeResult.model_json_schema(),
            "rules": {
                "latency": "sum of serving calls through trace completion; no judge",
                "percentile": "nearest rank",
                "retries": 0,
                "cache": False,
                "extraction_mismatch": "incorrect; stop",
                "denominator_per_arm": 80,
                "classification": "exact, claims, factual, false_pass, policy",
                "citation_gate": "separate production condition",
                "retrieval": "macro MRR@5 and gain(2**grade-1) NDCG@5 over six QA",
            },
        }
    )


class JudgeAccounting(StrictModel):
    config_hash: Sha256
    requested_model_id: Literal["claude-sonnet-5"]
    returned_model_id: ReturnedModel | None
    cost: CostSummary
    records: list[TraceRecord]


class JudgeEvaluation(JudgeAccounting):
    judgment: JudgeResult


class JudgeFailure(Exception):
    """A safe failure retaining all known judge accounting, never a judgment."""

    def __init__(self, code: ErrorCode, accounting: JudgeAccounting) -> None:
        self.code = code
        self.accounting = accounting
        super().__init__(ProviderFailure(code).message)


def validate_judge_accounting(value: JudgeAccounting, context: TraceContext) -> None:
    if value.config_hash != context.config_hash or value.requested_model_id != JUDGE_MODEL:
        raise ValueError("Judge identity mismatch")
    if (
        any(
            record.phase != "judge"
            or record.attempt_id != context.attempt_id
            or record.trace_id != context.trace_id
            or record.config_hash != context.config_hash
            or record.source_commit != context.source_commit
            or record.corpus_hash != context.corpus_hash
            or record.pricing_hash != context.pricing_hash
            or (record.record_kind == "provider_call" and record.requested_model_id != JUDGE_MODEL)
            for record in value.records
        )
        or sum(record.record_kind == "endpoint" for record in value.records) != 1
        or sum(record.provider_operation == "generation" for record in value.records) > 1
    ):
        raise ValueError("Judge trace mismatch")
    if provider_cost_totals(value.records) != value.cost:
        raise ValueError("Judge accounting mismatch")
    if value.returned_model_id is not None and any(
        record.returned_model_id != value.returned_model_id
        for record in value.records
        if record.provider_operation == "generation"
    ):
        raise ValueError("Judge returned model differs from call metadata")


class JudgeSafetyContext(StrictModel):
    """Trusted policy context; invariants need not appear as repeated answer text."""

    policy_claims: Literal[True] = True
    confirmed_facts: NoticeFacts | RentFacts | None
    expected_tool_status: ToolStatus | None
    policy_ids: tuple[SecurityCaseId, ...] = (
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
        "S06",
        "S07",
        "S08",
    )

    @field_validator("policy_claims", mode="before")
    @classmethod
    def strict_policy_mode(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("Safety policy mode must be boolean true")
        return True

    @model_validator(mode="after")
    def fixed_policy_ids(self) -> "JudgeSafetyContext":
        if self.policy_ids != ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"):
            raise ValueError("Safety context requires the fixed policy inventory")
        return self


@dataclass(frozen=True)
class JudgeInput:
    """Equal canonical evidence and expectations in both arms; no arm label."""

    required_claims: tuple[Claim, ...]
    evidence: tuple[Chunk, ...]
    expected_tool_result: ToolResult | None
    response: AskResponse
    actual_tool_result: ToolResult | None
    cited_evidence: tuple[Chunk, ...]
    safety_context: JudgeSafetyContext | None = None


class JudgePort(Protocol):
    async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation: ...


class AttemptIdentity(StrictModel):
    run_id: CanonicalUUID
    source_sha: SourceCommit
    gold_hash: Sha256


@dataclass(frozen=True)
class AttemptOutcome:
    row: ResultRow
    records: tuple[TraceRecord, ...]
    judge_evaluation: JudgeEvaluation | None
    reasons: tuple[str, ...]
    judge_accounting: JudgeAccounting | None = None


class _Tee(StringIO):
    def __init__(self, target: TextIO) -> None:
        super().__init__()
        self.target = target

    def write(self, text: str) -> int:
        self.target.write(text)
        self.target.flush()
        return super().write(text)


async def run_attempt(
    case: GoldCase,
    entry: PlanEntry,
    *,
    identity: AttemptIdentity,
    corpus: Corpus,
    messages: MessagesPort,
    budget: SpendBudget,
    allowlist: SyntheticAllowlist,
    retrieve: Callable[[str], tuple[SearchHit, ...]],
    metadata: TextIO,
    raw_provider: TextIO,
    judge: JudgePort | None = None,
    judge_config_hash: str | None = None,
    warmup: bool = False,
    clock: Callable[[], float] = time.monotonic,
    attempt_id: UUID | None = None,
) -> AttemptOutcome:
    """One attempt; extraction mismatch never feeds corrected facts to analysis."""
    allowlist.require(case)
    if case.id != entry.case_id:
        raise ValueError("Attempt does not match planned case")
    attempt_id = attempt_id or uuid4()
    request_data = case.request.model_dump(mode="json")
    request_data["attempt_id"] = str(attempt_id)
    request = ASK_REQUEST_ADAPTER.validate_json(json.dumps(request_data))
    recorder = SyntheticRecorder(
        messages,
        case=case,
        allowlist=allowlist,
        corpus=corpus,
        stream=raw_provider,
        run_id=identity.run_id,
        attempt_id=attempt_id,
    )
    provider = ProviderAdapter(recorder, budget=budget)
    trace_stream = _Tee(metadata)
    sink = MetadataSink(trace_stream)
    trace_ids: list[UUID] = []
    assertions: list[DeterministicAssertion] = []
    reasons: list[str] = []
    response: AskResponse | ErrorResponse | None = None
    actual_extract: Extraction | None = None
    latency_ms = 0.0
    analysis_started = False

    def context(phase: Literal["extraction", "analysis"]) -> TraceContext:
        trace_id = uuid4()
        trace_ids.append(trace_id)
        recorder.bind(trace_id, phase, request, arm=entry.arm)
        return TraceContext(
            attempt_id=attempt_id,
            trace_id=trace_id,
            phase=phase,
            source_commit=identity.source_sha,
            corpus_hash=corpus.corpus_hash,
            pricing_hash=PRICING_HASH,
            config_hash=extraction_config_hash()
            if phase == "extraction"
            else agent_config_hash(request.mode, entry.arm),
        )

    def failure(error: ProviderFailure, trace_id: UUID) -> ErrorResponse:
        return ErrorResponse(
            attempt_id=attempt_id,
            trace_id=trace_id,
            error=ErrorDetail(code=error.code, message=error.message),
            snapshot_date=corpus.snapshot_date,
            disclaimer=disclaimer_for(corpus.snapshot_date),
        )

    if case.letter is not None:
        ctx = context("extraction")
        started = clock()
        trace = TraceRecorder(ctx, sink, monotonic=clock)
        code: Any = "provider_error"
        try:
            actual_extract = await extract_letter(
                ExtractRequest(attempt_id=attempt_id, letter=case.letter),
                provider=provider,
                trace=trace,
                redact=redact_text,
                deadline=Deadline.start(clock=clock),
            )
            code = "ok"
            matches = actual_extract == case.expected_extract
            assertions.append(DeterministicAssertion(id="extraction_exact", passed=matches))
            if matches:
                request_data["facts"].update(actual_extract.model_dump(mode="json"))
                request = ASK_REQUEST_ADAPTER.validate_json(json.dumps(request_data))
        except asyncio.CancelledError:
            code = "deadline_exceeded"
            reasons.append("interrupted")
            response = failure(ProviderFailure(code), ctx.trace_id)
        except ProviderFailure as error:
            code = error.code
            response = failure(error, ctx.trace_id)
        except Exception:
            response = failure(ProviderFailure("provider_error"), ctx.trace_id)
        finally:
            trace.finish(code)
            latency_ms += (clock() - started) * 1000

    if response is None and all(item.passed for item in assertions):
        ctx = context("analysis")
        analysis_started = True
        started = clock()
        try:
            response = await answer(
                request,
                provider=provider,
                corpus=corpus,
                retrieve=retrieve,
                redact=redact_text,
                deadline=Deadline.start(clock=clock),
                context=ctx,
                sink=sink,
                arm=entry.arm,
            )
        except asyncio.CancelledError:
            reasons.append("interrupted")
            response = failure(ProviderFailure("deadline_exceeded"), ctx.trace_id)
        except ProviderFailure as error:
            response = failure(error, ctx.trace_id)
        except Exception:
            response = failure(ProviderFailure("provider_error"), ctx.trace_id)
        finally:
            latency_ms += (clock() - started) * 1000

    records = tuple(
        TraceRecord.model_validate_json(line) for line in trace_stream.getvalue().splitlines()
    )
    actual_args, actual_result = recorder.actual_tool_args, recorder.actual_tool_result
    if isinstance(response, AskResponse) and response.tool_result is not None:
        actual_result = response.tool_result
        if request.mode != "question":
            actual_args = request.facts
    if analysis_started and request.mode != "question":
        assertions.extend(
            [
                DeterministicAssertion(
                    id="tool_name_exact",
                    passed=actual_result is not None and actual_result.tool == case.expected_tool,
                ),
                DeterministicAssertion(
                    id="tool_args_exact", passed=actual_args == case.expected_tool_args
                ),
                DeterministicAssertion(
                    id="tool_result_exact", passed=actual_result == case.expected_tool_result
                ),
            ]
        )
        endpoints = [
            record
            for record in records
            if record.record_kind == "endpoint" and record.phase == "analysis"
        ]
        if actual_result is None and any(record.tool_name is not None for record in endpoints):
            reasons.append("observation_missing")
    retrieved_ids = [
        identifier
        for record in records
        if record.record_kind == "endpoint" and record.phase == "analysis"
        for identifier in record.retrieved_evidence_ids
    ]
    cost = provider_cost_totals(records)
    if not cost.usage_complete:
        reasons.append("serving_usage_missing")
    judged: JudgeEvaluation | None = None
    judge_accounting: JudgeAccounting | None = None
    if isinstance(response, AskResponse) and response.status == "answered" and not warmup:
        if judge is None or judge_config_hash is None:
            reasons.append("judge_missing")
        else:
            try:
                judge_context = TraceContext(
                    attempt_id=attempt_id,
                    trace_id=uuid4(),
                    phase="judge",
                    source_commit=identity.source_sha,
                    config_hash=judge_config_hash,
                    corpus_hash=corpus.corpus_hash,
                    pricing_hash=PRICING_HASH,
                )
                judged = await judge.evaluate(
                    JudgeInput(
                        required_claims=tuple(case.required_claims),
                        evidence=tuple(
                            corpus.chunk(identifier)
                            for identifier in sorted(set(case.evidence_ids))
                        ),
                        expected_tool_result=case.expected_tool_result,
                        response=response,
                        actual_tool_result=actual_result,
                        cited_evidence=tuple(
                            corpus.chunk(citation.id) for citation in response.citations
                        ),
                    ),
                    context=judge_context,
                )
                accounting_payload = json.loads(
                    judged.model_dump_json(exclude={"judgment"}, warnings=False)
                )
                # Optional completion identity cannot discard already known usage.
                accounting_payload["returned_model_id"] = None
                accounting = JudgeAccounting.model_validate_json(json.dumps(accounting_payload))
                validate_judge_accounting(accounting, judge_context)
                judge_accounting = accounting
                if not accounting.cost.usage_complete:
                    reasons.append("judge_usage_missing")
                judged = JudgeEvaluation.model_validate_json(judged.model_dump_json(warnings=False))
                accounting = JudgeAccounting.model_validate_json(
                    judged.model_dump_json(exclude={"judgment"})
                )
                validate_judge_accounting(accounting, judge_context)
                judge_accounting = accounting
                judged.judgment.validate_coverage(
                    (claim.id for claim in case.required_claims),
                    (statement.id for statement in response.statements),
                )
            except JudgeFailure as error:
                judged = None
                try:
                    validate_judge_accounting(error.accounting, judge_context)
                    judge_accounting = error.accounting
                    if not error.accounting.cost.usage_complete:
                        reasons.append("judge_usage_missing")
                except ValueError:
                    reasons.append("judge_accounting_invalid")
                reasons.append("judge_invalid")
            except asyncio.CancelledError:
                judged = None
                reasons.extend(("interrupted", "judge_invalid"))
            except Exception:
                judged = None
                reasons.append("judge_invalid")
    judgment = judged.judgment if judged else None
    decision = classify(
        response,
        assertions,
        judgment,
        expected_claim_ids=[claim.id for claim in case.required_claims],
        arm=entry.arm,
    )
    if not warmup:
        reasons.extend(decision.reasons)
    row = ResultRow(
        run_id=identity.run_id,
        case_id=case.id,
        arm=entry.arm,
        repeat=entry.repeat,
        source_sha=identity.source_sha,
        config_hash=row_config_hash(case, entry.arm),
        corpus_hash=corpus.corpus_hash,
        gold_hash=identity.gold_hash,
        pricing_hash=PRICING_HASH,
        attempt_id=attempt_id,
        trace_ids=trace_ids,
        retrieved_ids=retrieved_ids,
        response=response,
        actual_extract=actual_extract,
        actual_tool_args=actual_args,
        actual_tool_result=actual_result,
        deterministic_assertions=assertions,
        judge=judgment,
        classification=decision.classification,
        latency_ms=latency_ms,
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        serving_cost_usd=cost.actual_cost_usd,
        judge_cost_usd=judge_accounting.cost.actual_cost_usd if judge_accounting else None,
        usage_complete=cost.usage_complete,
    )
    return AttemptOutcome(row, records, judged, tuple(dict.fromkeys(reasons)), judge_accounting)
