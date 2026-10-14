"""Three judged production attempts, kept separate from the fixed collection plan."""

import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from rent_navigator.corpus import Corpus
from rent_navigator.eval.collection import BatchLedger, RunEnvironment, _CollectionBudget
from rent_navigator.eval.data import GoldDataset
from rent_navigator.eval.judge import RawJudgeRecord, RealJudge, judge_config_hash
from rent_navigator.eval.live_budget import FundedMessages
from rent_navigator.eval.models import ResultRow
from rent_navigator.eval.recording import RawProviderRecord, SyntheticAllowlist
from rent_navigator.eval.runner import (
    AttemptIdentity,
    JudgeEvaluation,
    JudgeInput,
    PlanEntry,
    run_attempt,
)
from rent_navigator.index import SearchHit
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.models import CanonicalUUID, ErrorResponse, Sha256, SourceCommit, StrictModel
from rent_navigator.provider import MessagesPort, SpendBudget
from rent_navigator.trace import TraceContext, TraceRecord, provider_cost_totals


def dry_run_forecast() -> Decimal:
    """Reserve eight actor calls conservatively; the fixed attempts use five."""
    return (
        8 * MODEL_POLICIES[ACTOR_MODEL].reservation_usd
        + 3 * MODEL_POLICIES[JUDGE_MODEL].reservation_usd
    )


class DryRunManifest(StrictModel):
    schema_version: Literal[1]
    dry_run: Literal[True]
    reportable: Literal[False]
    evaluation_complete: Literal[False]
    dry_run_complete: bool
    run_id: CanonicalUUID
    source_sha: SourceCommit
    lock_hash: Sha256
    corpus_hash: Sha256
    gold_hash: Sha256
    judge_config_hash: Sha256
    environment: RunEnvironment
    attempts: list[ResultRow]
    failures: list[str]


class _TimedJudge:
    def __init__(self, delegate: RealJudge, clock: Callable[[], float]) -> None:
        self.delegate, self.clock = delegate, clock
        self.durations: dict[UUID, float] = {}

    async def evaluate(self, value: JudgeInput, *, context: TraceContext) -> JudgeEvaluation:
        started = self.clock()
        try:
            return await self.delegate.evaluate(value, context=context)
        finally:
            assert context.attempt_id is not None
            self.durations[context.attempt_id] = self.clock() - started


def _call_details(
    directory: Path, records: list[TraceRecord], attempt_id: UUID
) -> list[dict[str, Any]]:
    stops: dict[tuple[UUID, int], str | None] = {}
    for filename, kind in (
        ("raw-provider.jsonl", RawProviderRecord),
        ("judge-raw-provider.jsonl", RawJudgeRecord),
    ):
        for line in (directory / filename).read_text().splitlines():
            record = kind.model_validate_json(line)
            if (
                record.attempt_id == attempt_id
                and record.operation == "generation"
                and record.event == "response"
            ):
                stops[(record.trace_id, record.operation_index)] = record.value.get("stop_reason")
    return [
        {
            "phase": record.phase,
            "stop_reason": stops.get((record.trace_id, record.provider_call_index or 0)),
            "requested_model_id": record.requested_model_id,
            "returned_model_id": record.returned_model_id,
            "usage": {"input_tokens": record.input_tokens, "output_tokens": record.output_tokens},
            "usage_complete": record.usage_complete,
            "actual_cost_usd": record.model_dump(mode="json")["actual_cost_usd"],
            "reserved_cost_usd": record.model_dump(mode="json")["reserved_cost_usd"],
        }
        for record in records
        if record.provider_operation == "generation"
    ]


async def collect_dry_run(
    dataset: GoldDataset,
    output_dir: Path,
    *,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    environment: RunEnvironment,
    messages: MessagesPort,
    budget: SpendBudget,
    batch_ledger: BatchLedger,
    forecast_usd: Decimal,
    retrieve: Callable[[str], tuple[SearchHit, ...]],
    clock: Callable[[], float] = time.monotonic,
) -> DryRunManifest:
    """Reuse PlanEntry and run_attempt with judging, without constructing CollectionPlan."""
    if dataset.approval.status != "approved" or dataset.approval.corpus_hash != corpus.corpus_hash:
        raise ValueError("Dry run requires matching approved data")
    if not forecast_usd.is_finite() or forecast_usd < 0:
        raise ValueError("Dry-run forecast must be finite and nonnegative")
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = DryRunManifest(
        schema_version=1,
        dry_run=True,
        reportable=False,
        evaluation_complete=False,
        dry_run_complete=False,
        run_id=uuid4(),
        source_sha=source_sha,
        lock_hash=lock_hash,
        corpus_hash=corpus.corpus_hash,
        gold_hash=dataset.gold_hash,
        judge_config_hash=judge_config_hash(),
        environment=environment,
        attempts=[],
        failures=[],
    )

    def save() -> None:
        (output_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")

    save()
    cases = {case.id: case for case in dataset.cases}
    allowlist = SyntheticAllowlist.from_fixtures(dataset.cases)
    guarded_budget = _CollectionBudget(budget)
    guarded_messages = FundedMessages(messages, budget=guarded_budget)
    try:
        batch_ledger.reserve_batch(forecast_usd)
        with ExitStack() as stack:
            metadata = stack.enter_context((output_dir / "metadata.jsonl").open("x"))
            raw = stack.enter_context((output_dir / "raw-provider.jsonl").open("x"))
            judge_metadata = stack.enter_context((output_dir / "judge-metadata.jsonl").open("x"))
            judge_raw = stack.enter_context((output_dir / "judge-raw-provider.jsonl").open("x"))
            attempts = stack.enter_context((output_dir / "attempts.jsonl").open("x"))
            timed = _TimedJudge(
                RealJudge(
                    guarded_messages,
                    budget=guarded_budget,
                    metadata=judge_metadata,
                    raw_provider=judge_raw,
                    run_id=manifest.run_id,
                    clock=clock,
                ),
                clock,
            )
            for case_id in ("R01", "N01", "Q01"):
                if guarded_budget.stopped:
                    manifest.failures.append("budget_reforecast_required")
                    break
                entry = PlanEntry(case_id=case_id, arm="production", repeat=0)
                outcome = await run_attempt(
                    cases[case_id],
                    entry,
                    identity=AttemptIdentity(
                        run_id=manifest.run_id, source_sha=source_sha, gold_hash=dataset.gold_hash
                    ),
                    corpus=corpus,
                    messages=guarded_messages,
                    budget=guarded_budget,
                    allowlist=allowlist,
                    retrieve=retrieve,
                    metadata=metadata,
                    raw_provider=raw,
                    judge=timed,
                    judge_config_hash=manifest.judge_config_hash,
                    warmup=False,
                    clock=clock,
                )
                manifest.attempts.append(outcome.row)
                structural_failures = {
                    "judge_invalid",
                    "judge_usage_missing",
                    "serving_usage_missing",
                    "observation_missing",
                    "judge_missing",
                    "interrupted",
                }.intersection(outcome.reasons)
                manifest.failures.extend(sorted(structural_failures))
                if isinstance(outcome.row.response, ErrorResponse):
                    manifest.failures.append(outcome.row.response.error.code)
                for stream in (metadata, raw, judge_metadata, judge_raw):
                    stream.flush()
                judge_records = (
                    list(outcome.judge_accounting.records) if outcome.judge_accounting else []
                )
                records = [*outcome.records, *judge_records]
                detail = {
                    "case_id": case_id,
                    "arm": "production",
                    "repeat": 0,
                    "attempt_id": str(outcome.row.attempt_id),
                    "serving_duration_ms": outcome.row.latency_ms,
                    "judge_wall_seconds": timed.durations.get(outcome.row.attempt_id),
                    "cost": provider_cost_totals(records).model_dump(mode="json"),
                    "provider_calls": _call_details(output_dir, records, outcome.row.attempt_id),
                    "outcome_flags": list(outcome.reasons),
                    "row": outcome.row.model_dump(mode="json"),
                }
                attempts.write(json.dumps(detail) + "\n")
                attempts.flush()
                save()
                if outcome.judge_evaluation is None:
                    manifest.failures.append("judged_attempt_incomplete")
                if structural_failures or guarded_budget.stopped:
                    break
            manifest.dry_run_complete = len(manifest.attempts) == 3 and not manifest.failures
    except BaseException:
        manifest.failures.append("dry_run_interrupted")
        raise
    finally:
        save()
    return manifest
