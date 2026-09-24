"""Append-only synthetic collection artifacts and strict integrity verification."""

import json
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from anthropic.types import Message

from rent_navigator.agent import _ANSWER_SEPARATOR, REFUSAL_TEXT, _generated
from rent_navigator.context import Arm, Mode, context_provenance
from rent_navigator.corpus import Corpus
from rent_navigator.eval.data import GoldDataset
from rent_navigator.eval.metrics import aggregate, classify
from rent_navigator.eval.models import CASE_IDS, DeterministicAssertion, GoldCase, ResultRow
from rent_navigator.eval.offline import configuration_identity_hash
from rent_navigator.eval.provider_evidence import usage_cost_from_raw
from rent_navigator.eval.recording import (
    RawProviderRecord,
    SyntheticAllowlist,
    verify_synthetic_records,
)
from rent_navigator.eval.runner import (
    AttemptIdentity,
    CollectionPlan,
    JudgeAccounting,
    JudgeEvaluation,
    JudgeInput,
    JudgePort,
    PlanEntry,
    build_plan,
    config_map,
    protocol_hash,
    row_config_hash,
    run_attempt,
    validate_judge_accounting,
)
from rent_navigator.index import SearchHit
from rent_navigator.model_policy import RequestedModel
from rent_navigator.models import (
    AskResponse,
    CanonicalUUID,
    ErrorResponse,
    Extraction,
    NoticeFacts,
    RentFacts,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolResult,
)
from rent_navigator.provider import MessagesPort, ProviderFailure, Reservation, SpendBudget
from rent_navigator.security_cases import security_cases_hash
from rent_navigator.trace import (
    PRICING_HASH,
    CostSummary,
    TraceContext,
    TraceRecord,
    cost_for_usage,
    provider_cost_totals,
)

ARTIFACT_FILES = (
    "plan.json",
    "results.jsonl",
    "warmups.jsonl",
    "metadata.jsonl",
    "raw-provider.jsonl",
    "summary.json",
)


def _encode(value: object) -> str:
    def decimal(item: object) -> str:
        if not isinstance(item, Decimal):
            raise TypeError("Unsupported collection value")
        return format(item, "f")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=decimal)


class RunEnvironment(StrictModel):
    os: str
    cpu: str
    ram_bytes: int | None
    docker_version: str | None
    image_digest: str | None
    source_clean: bool
    provider_access_date: str | None
    pricing_url: str


class StartedAttempt(StrictModel):
    warmup: bool
    entry: PlanEntry


class _CollectionManifestBase(StrictModel):
    schema_version: Literal[1]
    collection_complete: bool
    run_id: CanonicalUUID
    source_sha: SourceCommit
    corpus_hash: Sha256
    gold_hash: Sha256
    approval_hash: Sha256
    security_hash: Sha256
    pricing_hash: Sha256
    lock_hash: Sha256
    protocol_hash: Sha256
    config_map: dict[str, Sha256]
    config_hash: Sha256
    judge_config_hash: Sha256 | None
    environment: RunEnvironment
    files: dict[str, Sha256]
    started_attempts: list[StartedAttempt]
    judge_evaluations: dict[str, JudgeEvaluation]
    judge_accounting: dict[str, JudgeAccounting]
    reasons: list[str]


class CollectionManifest(_CollectionManifestBase):
    execution_mode: Literal["synthetic"]
    reportable: Literal[False]
    evaluation_complete: Literal[False]


class RealCollectionManifest(_CollectionManifestBase):
    execution_mode: Literal["live"]
    reportable: bool
    evaluation_complete: bool


REAL_ARTIFACT_FILES = (*ARTIFACT_FILES, "judge-metadata.jsonl", "judge-raw-provider.jsonl")


class BatchLedger(Protocol):
    """Reserve the complete forecast before starting any collection operations."""

    def reserve_batch(self, forecast_usd: Decimal) -> None: ...


class _CollectionBudget:
    """Share a model-anomaly stop while preserving the current call's usage."""

    def __init__(self, budget: SpendBudget) -> None:
        self._budget = budget
        self._reforecast = False

    @property
    def stopped(self) -> bool:
        return self._reforecast or self._budget.stopped

    def require_reforecast(self) -> None:
        self._reforecast = True

    @property
    def model_anomaly(self) -> bool:
        return self._reforecast

    def reserve(self, model: RequestedModel) -> Reservation:
        if self.stopped:
            raise ProviderFailure("provider_error")
        return self._budget.reserve(model)

    def reconcile(
        self, reservation: Reservation, cost: CostSummary, *, reforecast: bool = False
    ) -> None:
        if self._reforecast:
            cost = cost_for_usage(reservation.model, None)
        self._budget.reconcile(reservation, cost, reforecast=reforecast or self._reforecast)


async def collect_synthetic(
    dataset: GoldDataset,
    output_dir: Path,
    *,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    environment: RunEnvironment,
    messages_factory: Callable[[GoldCase, PlanEntry, bool], MessagesPort],
    budget: SpendBudget,
    batch_ledger: BatchLedger,
    forecast_usd: Decimal,
    retrieve: Callable[[str], tuple[SearchHit, ...]],
    judge: JudgePort | None = None,
    judge_config_hash: str | None = None,
    plan: CollectionPlan | None = None,
) -> CollectionManifest:
    """Synthetic entry remains nonreportable and incomplete under every outcome."""
    manifest = await _collect(
        dataset,
        output_dir,
        corpus=corpus,
        source_sha=source_sha,
        lock_hash=lock_hash,
        environment=environment,
        messages_factory=messages_factory,
        budget=budget,
        batch_ledger=batch_ledger,
        forecast_usd=forecast_usd,
        retrieve=retrieve,
        judge=judge,
        judge_config_hash=judge_config_hash,
        plan=plan,
    )
    assert isinstance(manifest, CollectionManifest)
    return manifest


async def collect_real(
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
    plan: CollectionPlan | None = None,
) -> RealCollectionManifest:
    """Compose real serving and judging over the existing 166-entry serial plan.

    This callable reads no credentials and grants no spending authority. Its caller
    supplies an already authorized port and shared bounded budget. Verification of
    complete observations and judge artifacts derives completeness; no caller flag
    can mark a partial collection complete.
    """
    from rent_navigator.eval.judge import judge_config_hash
    from rent_navigator.eval.live_budget import FundedMessages

    guarded_budget = _CollectionBudget(budget)
    guarded_messages = FundedMessages(messages, budget=guarded_budget)

    manifest = await _collect(
        dataset,
        output_dir,
        corpus=corpus,
        source_sha=source_sha,
        lock_hash=lock_hash,
        environment=environment,
        messages_factory=lambda case, entry, warmup: guarded_messages,
        budget=guarded_budget,
        batch_ledger=batch_ledger,
        forecast_usd=forecast_usd,
        retrieve=retrieve,
        judge_config_hash=judge_config_hash(),
        plan=plan,
        real_messages=guarded_messages,
    )
    assert isinstance(manifest, RealCollectionManifest)
    return manifest


async def _collect(
    dataset: GoldDataset,
    output_dir: Path,
    *,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    environment: RunEnvironment,
    messages_factory: Callable[[GoldCase, PlanEntry, bool], MessagesPort],
    budget: SpendBudget,
    batch_ledger: BatchLedger,
    forecast_usd: Decimal,
    retrieve: Callable[[str], tuple[SearchHit, ...]],
    judge: JudgePort | None = None,
    judge_config_hash: str | None = None,
    plan: CollectionPlan | None = None,
    real_messages: MessagesPort | None = None,
) -> CollectionManifest | RealCollectionManifest:
    """Run declared synthetic scenarios; this entry never composes a live client."""
    if dataset.approval.status != "approved" or dataset.approval.corpus_hash != corpus.corpus_hash:
        raise ValueError("Collection requires approved matching synthetic data")
    if tuple(case.id for case in dataset.cases) != CASE_IDS:
        raise ValueError("Collection requires every case in canonical order")
    if not forecast_usd.is_finite() or forecast_usd < 0:
        raise ValueError("Invalid batch forecast")
    plan = plan or build_plan()
    plan = CollectionPlan.model_validate_json(plan.model_dump_json())
    configs, protocol = config_map(), protocol_hash()
    manifest_type = CollectionManifest if real_messages is None else RealCollectionManifest
    artifact_files = ARTIFACT_FILES if real_messages is None else REAL_ARTIFACT_FILES
    mode = "synthetic" if real_messages is None else "live"
    manifest = manifest_type.model_validate(
        dict(
            schema_version=1,
            execution_mode=mode,
            reportable=False,
            evaluation_complete=False,
            collection_complete=False,
            run_id=plan.run_id,
            source_sha=source_sha,
            corpus_hash=corpus.corpus_hash,
            gold_hash=dataset.gold_hash,
            approval_hash=dataset.approval_hash,
            security_hash=security_cases_hash(),
            pricing_hash=PRICING_HASH,
            lock_hash=lock_hash,
            protocol_hash=protocol,
            config_map=configs,
            config_hash=configuration_identity_hash(protocol, configs, judge_config_hash),
            judge_config_hash=judge_config_hash,
            environment=environment,
            files={},
            started_attempts=[],
            judge_evaluations={},
            judge_accounting={},
            reasons=["synthetic_execution"] if real_messages is None else [],
        )
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "plan.json").write_text(plan.model_dump_json() + "\n")
    for name in artifact_files[1:]:
        (output_dir / name).touch(exist_ok=False)
    rows: list[ResultRow] = []
    warmups: list[ResultRow] = []
    manifest_path = output_dir / "manifest.json"

    def save() -> None:
        summary = asdict(
            aggregate(
                rows,
                warmups=warmups,
                expected_claim_ids={
                    case.id: [claim.id for claim in case.required_claims] for case in dataset.cases
                },
            )
        )
        summary.update(
            execution_mode=mode,
            reportable=manifest.reportable,
            evaluation_complete=manifest.evaluation_complete,
        )
        (output_dir / "summary.json").write_text(_encode(summary) + "\n")
        manifest.files = {
            name: sha256((output_dir / name).read_bytes()).hexdigest() for name in artifact_files
        }
        manifest_path.write_text(manifest.model_dump_json() + "\n")

    save()
    cases = {case.id: case for case in dataset.cases}
    allowlist = SyntheticAllowlist.from_fixtures(dataset.cases)
    try:
        batch_ledger.reserve_batch(forecast_usd)
        with ExitStack() as streams:
            metadata = streams.enter_context((output_dir / "metadata.jsonl").open("a"))
            raw = streams.enter_context((output_dir / "raw-provider.jsonl").open("a"))
            if real_messages is not None:
                from rent_navigator.eval.judge import RealJudge

                judge_metadata = streams.enter_context(
                    (output_dir / "judge-metadata.jsonl").open("a")
                )
                judge_raw = streams.enter_context(
                    (output_dir / "judge-raw-provider.jsonl").open("a")
                )
                judge = RealJudge(
                    real_messages,
                    budget=budget,
                    metadata=judge_metadata,
                    raw_provider=judge_raw,
                    run_id=plan.run_id,
                )
            for is_warmup, entries in ((True, plan.warmups), (False, plan.measured)):
                for entry in entries:
                    if budget.stopped:
                        manifest.reasons.append("budget_reforecast_required")
                        return manifest
                    manifest.started_attempts.append(StartedAttempt(warmup=is_warmup, entry=entry))
                    save()
                    outcome = await run_attempt(
                        cases[entry.case_id],
                        entry,
                        identity=AttemptIdentity(
                            run_id=plan.run_id, source_sha=source_sha, gold_hash=dataset.gold_hash
                        ),
                        corpus=corpus,
                        messages=messages_factory(cases[entry.case_id], entry, is_warmup),
                        budget=budget,
                        allowlist=allowlist,
                        retrieve=retrieve,
                        metadata=metadata,
                        raw_provider=raw,
                        judge=judge,
                        judge_config_hash=judge_config_hash,
                        warmup=is_warmup,
                    )
                    target = warmups if is_warmup else rows
                    target.append(outcome.row)
                    with (output_dir / ("warmups.jsonl" if is_warmup else "results.jsonl")).open(
                        "a"
                    ) as file:
                        file.write(outcome.row.model_dump_json() + "\n")
                        file.flush()
                    if outcome.judge_evaluation is not None:
                        manifest.judge_evaluations[str(outcome.row.attempt_id)] = (
                            outcome.judge_evaluation
                        )
                    if outcome.judge_accounting is not None:
                        manifest.judge_accounting[str(outcome.row.attempt_id)] = (
                            outcome.judge_accounting
                        )
                    manifest.reasons.extend(
                        f"{outcome.row.attempt_id}:{reason}" for reason in outcome.reasons
                    )
                    if isinstance(budget, _CollectionBudget) and budget.model_anomaly:
                        manifest.reasons.extend(
                            ("provider_model_mismatch", "budget_reforecast_required")
                        )
                        return manifest
                    save()
                    if real_messages is not None and (
                        any(
                            reason in outcome.reasons
                            for reason in (
                                "judge_invalid",
                                "judge_usage_missing",
                                "serving_usage_missing",
                                "observation_missing",
                                "judge_missing",
                            )
                        )
                        or isinstance(outcome.row.response, ErrorResponse)
                        and outcome.row.response.error.code
                        not in {"invalid_generated_output", "tool_protocol_error"}
                    ):
                        manifest.reasons.append("collection_untrustworthy")
                        return manifest
                    if "interrupted" in outcome.reasons:
                        return manifest
                    if "judge_usage_missing" in outcome.reasons or (
                        "judge_invalid" in outcome.reasons and outcome.judge_accounting is None
                    ):
                        manifest.reasons.append("budget_reforecast_required")
                        return manifest
        manifest.collection_complete = True
        if isinstance(manifest, RealCollectionManifest):
            save()
            try:
                _verify_collection(
                    output_dir,
                    dataset=dataset,
                    corpus=corpus,
                    source_sha=source_sha,
                    lock_hash=lock_hash,
                    manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
                    real=True,
                    require_complete=True,
                )
            except ValueError:
                manifest.reasons.append("collection_evidence_incomplete")
            else:
                manifest.evaluation_complete = True
                manifest.reportable = bool(
                    environment.source_clean
                    and environment.image_digest
                    and environment.docker_version
                    and environment.provider_access_date
                )
    except BaseException:
        manifest.reasons.append("collection_interrupted")
        raise
    finally:
        save()
    return manifest


def _verify_collection(
    directory: Path,
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    manifest_sha256: str,
    require_complete: bool = True,
    real: bool = False,
) -> CollectionManifest | RealCollectionManifest:
    """Verify bytes, plan coverage, source identity and cross-file trace accounting."""
    raw_manifest = (directory / "manifest.json").read_bytes()
    if sha256(raw_manifest).hexdigest() != manifest_sha256:
        raise ValueError("Collection manifest digest mismatch")
    manifest = (
        RealCollectionManifest.model_validate_json(raw_manifest)
        if real
        else CollectionManifest.model_validate_json(raw_manifest)
    )
    artifact_files = REAL_ARTIFACT_FILES if real else ARTIFACT_FILES
    if isinstance(manifest, RealCollectionManifest) and manifest.evaluation_complete:
        require_complete = True
    if set(path.name for path in directory.iterdir()) != set(artifact_files) | {"manifest.json"}:
        raise ValueError("Collection artifact inventory mismatch")
    if (
        manifest.source_sha != source_sha
        or manifest.corpus_hash != corpus.corpus_hash
        or manifest.gold_hash != dataset.gold_hash
        or manifest.approval_hash != dataset.approval_hash
        or manifest.pricing_hash != PRICING_HASH
        or manifest.security_hash != security_cases_hash()
        or manifest.lock_hash != lock_hash
        or manifest.protocol_hash != protocol_hash()
        or manifest.config_map != config_map()
        or manifest.config_hash
        != configuration_identity_hash(
            manifest.protocol_hash, manifest.config_map, manifest.judge_config_hash
        )
        or set(manifest.files) != set(artifact_files)
    ):
        raise ValueError("Collection provenance mismatch")
    for name, digest in manifest.files.items():
        if (directory / name).is_symlink() or sha256(
            (directory / name).read_bytes()
        ).hexdigest() != digest:
            raise ValueError("Collection artifact digest mismatch")
    plan = CollectionPlan.model_validate_json((directory / "plan.json").read_bytes())
    if plan.run_id != manifest.run_id:
        raise ValueError("Collection plan identity mismatch")
    rows = [
        ResultRow.model_validate_json(line)
        for line in (directory / "results.jsonl").read_text().splitlines()
    ]
    warmups = [
        ResultRow.model_validate_json(line)
        for line in (directory / "warmups.jsonl").read_text().splitlines()
    ]
    records = [
        TraceRecord.model_validate_json(line)
        for line in (directory / "metadata.jsonl").read_text().splitlines()
    ]
    cases = {case.id: case for case in dataset.cases}
    all_rows = [*warmups, *rows]
    expected_started = [
        StartedAttempt(warmup=warmup, entry=entry)
        for warmup, entries in ((True, plan.warmups), (False, plan.measured))
        for entry in entries
    ]
    if manifest.started_attempts != expected_started[: len(manifest.started_attempts)]:
        raise ValueError("Unexpected or duplicated started attempt")
    if len(manifest.started_attempts) != len(all_rows):
        raise ValueError("Started attempt is missing its result")
    if require_complete and (
        not manifest.collection_complete or len(rows) != 160 or len(warmups) != 6
    ):
        raise ValueError("Collection is incomplete")
    if len({row.attempt_id for row in all_rows}) != len(all_rows):
        raise ValueError("Duplicate attempt ID")
    trace_ids: set[object] = set()
    for entries, values in ((plan.warmups, warmups), (plan.measured, rows)):
        if [(row.case_id, row.arm, row.repeat) for row in values] != [
            (entry.case_id, entry.arm, entry.repeat) for entry in entries[: len(values)]
        ]:
            raise ValueError("Collection rows do not match plan")
        for row in values:
            if (
                row.run_id != plan.run_id
                or row.source_sha != source_sha
                or row.config_hash != row_config_hash(cases[row.case_id], row.arm)
                or row.corpus_hash != corpus.corpus_hash
                or row.gold_hash != dataset.gold_hash
                or row.pricing_hash != PRICING_HASH
                or not row.trace_ids
                or trace_ids.intersection(row.trace_ids)
            ):
                raise ValueError("Result identity mismatch")
            trace_ids.update(row.trace_ids)
            linked = [record for record in records if record.attempt_id == row.attempt_id]
            if {record.trace_id for record in linked} != set(row.trace_ids):
                raise ValueError("Result trace linkage mismatch")
            endpoints = [record for record in linked if record.record_kind == "endpoint"]
            if len(endpoints) != len(row.trace_ids):
                raise ValueError("Missing or duplicate endpoint record")
            for record in linked:
                expected_config = (
                    manifest.config_map["extraction"]
                    if record.phase == "extraction"
                    else manifest.config_map[f"{cases[row.case_id].request.mode}:{row.arm}"]
                )
                if (
                    record.phase == "judge"
                    or record.config_hash != expected_config
                    or (
                        record.source_commit != source_sha
                        or record.corpus_hash != corpus.corpus_hash
                    )
                ):
                    raise ValueError("Trace provenance mismatch")
            totals = provider_cost_totals(linked)
            if (
                totals.input_tokens,
                totals.output_tokens,
                totals.actual_cost_usd,
                totals.usage_complete,
            ) != (row.input_tokens, row.output_tokens, row.serving_cost_usd, row.usage_complete):
                raise ValueError("Result serving accounting mismatch")
            for row_field, trace_field in (
                ("retrieved_ids", "retrieved_evidence_ids"),
                ("foundation_evidence_ids", "foundation_evidence_ids"),
                ("initial_context_evidence_ids", "initial_context_evidence_ids"),
            ):
                prepared = [
                    identifier
                    for endpoint in endpoints
                    if endpoint.phase == "analysis"
                    for identifier in getattr(endpoint, trace_field)
                ]
                if getattr(row, row_field) != prepared:
                    raise ValueError("Result context observations mismatch")
            if row.response is not None and (
                row.response.attempt_id != row.attempt_id
                or row.response.trace_id != row.trace_ids[-1]
            ):
                raise ValueError("Response linkage mismatch")
    if {record.trace_id for record in records} != trace_ids:
        raise ValueError("Unlinked metadata records")
    linked_pairs = {(row.attempt_id, trace_id) for row in all_rows for trace_id in row.trace_ids}
    if any((record.attempt_id, record.trace_id) not in linked_pairs for record in records):
        raise ValueError("Metadata attempt is not linked to its trace")
    raw_records = [
        RawProviderRecord.model_validate_json(line)
        for line in (directory / "raw-provider.jsonl").read_text().splitlines()
    ]
    if real and any(
        record.provider_operation == "generation"
        and record.returned_model_id != record.requested_model_id
        for record in records
    ):
        raise ValueError("Real collection provider model mismatch requires reforecast")
    raw = [item.model_dump(mode="json") for item in raw_records]
    expected_calls = {
        (
            str(record.attempt_id),
            str(record.trace_id),
            record.provider_call_index,
            record.provider_operation,
        )
        for record in records
        if record.record_kind == "provider_call"
        and record.provider_call_index is not None
        and record.provider_operation is not None
    }
    found: dict[tuple[str, str, int, str], list[str]] = {}
    for item in raw:
        if set(item) != {
            "run_id",
            "attempt_id",
            "trace_id",
            "phase",
            "operation",
            "operation_index",
            "event",
            "context_provenance",
            "value",
        }:
            raise ValueError("Invalid raw provider record")
        raw_key = (item["attempt_id"], item["trace_id"], item["operation_index"], item["operation"])
        if item["run_id"] != str(plan.run_id) or raw_key not in expected_calls:
            raise ValueError("Unlinked raw provider record")
        call = next(
            record
            for record in records
            if str(record.trace_id) == item["trace_id"]
            and record.provider_call_index == item["operation_index"]
        )
        if item["event"] == "request" and item["value"].get("model") != call.requested_model_id:
            raise ValueError("Raw request model does not match call metadata")
        if item["event"] == "failure" and (
            call.response_code == "ok" or item["value"].get("code") != call.response_code
        ):
            raise ValueError("Raw failure disagrees with provider metadata")
        if item["event"] == "response" and item["operation"] == "count_tokens":
            tokens = item["value"].get("input_tokens")
            invalid_count = type(tokens) is not int or tokens < 0
            if (
                set(item["value"]) not in (set(), {"input_tokens"})
                or invalid_count
                and (
                    call.response_code != "provider_error"
                    or any(
                        later.trace_id == call.trace_id
                        and later.provider_operation == "generation"
                        and later.provider_call_index is not None
                        and later.provider_call_index > item["operation_index"]
                        for later in records
                    )
                )
            ):
                raise ValueError("Invalid raw token count response")
        if item["event"] == "response" and item["operation"] == "generation":
            if (
                call.returned_model_id is not None
                and item["value"].get("model") != call.returned_model_id
            ):
                raise ValueError("Raw returned model does not match call metadata")
            if call.usage_complete:
                raw_usage = item["value"].get("usage")
                if not isinstance(raw_usage, dict) or any(
                    type(raw_usage.get(name)) is not int
                    for name in ("input_tokens", "output_tokens")
                ):
                    raise ValueError("Raw token counts must be strict integers")
            if call.requested_model_id is None:
                raise ValueError("Generation metadata lacks a requested model")
            expected_cost = usage_cost_from_raw(item["value"], call.requested_model_id)
            observed_cost = CostSummary.model_validate(
                {field: getattr(call, field) for field in CostSummary.model_fields}
            )
            if expected_cost != observed_cost:
                raise ValueError("Raw usage does not match call metadata")
        found.setdefault(raw_key, []).append(item["event"])
    if set(found) != expected_calls or any(
        events not in (["request", "response"], ["request", "failure"]) for events in found.values()
    ):
        raise ValueError("Missing or duplicated raw provider events")
    row_by_attempt = {str(row.attempt_id): row for row in all_rows}
    if not set(manifest.judge_accounting) <= set(row_by_attempt) or not set(
        manifest.judge_evaluations
    ) <= set(manifest.judge_accounting):
        raise ValueError("Unlinked judge records")
    warmup_ids = {row.attempt_id for row in warmups}
    for row in all_rows:
        verify_synthetic_records(
            [record for record in raw_records if record.attempt_id == row.attempt_id],
            case=cases[row.case_id],
            corpus=corpus,
            arm=row.arm,
        )
        _verify_observations(row, cases[row.case_id], records, raw, corpus)
        accounting = manifest.judge_accounting.get(str(row.attempt_id))
        evaluation = manifest.judge_evaluations.get(str(row.attempt_id))
        if accounting is not None:
            if (
                row.attempt_id in warmup_ids
                or manifest.judge_config_hash is None
                or not accounting.records
            ):
                raise ValueError("Unexpected judge accounting")
            context = TraceContext(
                attempt_id=row.attempt_id,
                trace_id=accounting.records[0].trace_id,
                phase="judge",
                source_commit=source_sha,
                config_hash=manifest.judge_config_hash,
                corpus_hash=corpus.corpus_hash,
                pricing_hash=PRICING_HASH,
            )
            validate_judge_accounting(accounting, context)
            if row.judge_cost_usd != accounting.cost.actual_cost_usd:
                raise ValueError("Judge cost mismatch")
        elif row.judge_cost_usd is not None:
            raise ValueError("Judge cost has no accounting evidence")
        if evaluation is not None:
            if (
                row.judge != evaluation.judgment
                or accounting
                != JudgeAccounting.model_validate_json(
                    evaluation.model_dump_json(exclude={"judgment"})
                )
            ):
                raise ValueError("Judge result linkage mismatch")
        elif row.judge is not None:
            raise ValueError("Judgment has no call evidence")
    metrics = aggregate(
        rows,
        warmups=warmups,
        expected_claim_ids={
            case.id: [claim.id for claim in case.required_claims] for case in dataset.cases
        },
    )
    if require_complete and not metrics.complete:
        raise ValueError("Collection judgment or result coverage is incomplete")
    expected_summary = asdict(metrics)
    expected_summary.update(
        execution_mode=manifest.execution_mode,
        reportable=manifest.reportable,
        evaluation_complete=manifest.evaluation_complete,
    )
    if json.loads((directory / "summary.json").read_text()) != json.loads(
        _encode(expected_summary)
    ):
        raise ValueError("Collection summary mismatch")
    if real:
        assert isinstance(manifest, RealCollectionManifest)
        _verify_real_judges(
            directory,
            manifest=manifest,
            rows=rows,
            warmups=warmups,
            dataset=dataset,
            corpus=corpus,
            require_complete=require_complete,
        )
    return manifest


def verify_collection(
    directory: Path,
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    manifest_sha256: str,
    require_complete: bool = True,
) -> CollectionManifest:
    result = _verify_collection(
        directory,
        dataset=dataset,
        corpus=corpus,
        source_sha=source_sha,
        lock_hash=lock_hash,
        manifest_sha256=manifest_sha256,
        require_complete=require_complete,
    )
    assert isinstance(result, CollectionManifest)
    return result


def verify_real_collection(
    directory: Path,
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_hash: str,
    manifest_sha256: str,
    require_complete: bool = True,
) -> RealCollectionManifest:
    result = _verify_collection(
        directory,
        dataset=dataset,
        corpus=corpus,
        source_sha=source_sha,
        lock_hash=lock_hash,
        manifest_sha256=manifest_sha256,
        require_complete=require_complete,
        real=True,
    )
    assert isinstance(result, RealCollectionManifest)
    if require_complete and not result.evaluation_complete:
        raise ValueError("Real collection is incomplete")
    return result


def _verify_real_judges(
    directory: Path,
    *,
    manifest: RealCollectionManifest,
    rows: list[ResultRow],
    warmups: list[ResultRow],
    dataset: GoldDataset,
    corpus: Corpus,
    require_complete: bool,
) -> None:
    from rent_navigator.eval.judge import RawJudgeRecord, judge_config_hash, verify_judge_records

    if manifest.judge_config_hash != judge_config_hash():
        raise ValueError("Real judge configuration mismatch")
    if manifest.reportable and (
        not manifest.evaluation_complete
        or not manifest.environment.source_clean
        or not manifest.environment.image_digest
        or not manifest.environment.docker_version
        or not manifest.environment.provider_access_date
    ):
        raise ValueError("Reportable collection lacks complete environment evidence")
    judge_records = [
        TraceRecord.model_validate_json(line)
        for line in (directory / "judge-metadata.jsonl").read_text().splitlines()
    ]
    if any(
        record.provider_operation == "generation"
        and record.returned_model_id != record.requested_model_id
        for record in judge_records
    ):
        raise ValueError("Real collection judge model mismatch requires reforecast")
    raw = [
        RawJudgeRecord.model_validate_json(line)
        for line in (directory / "judge-raw-provider.jsonl").read_text().splitlines()
    ]
    expected_records = [
        record for accounting in manifest.judge_accounting.values() for record in accounting.records
    ]
    if judge_records != expected_records:
        raise ValueError("Separate judge metadata differs from accounting")
    ids = {row.attempt_id for row in rows}
    if any(
        record.attempt_id not in ids or str(record.attempt_id) not in manifest.judge_accounting
        for record in raw
    ):
        raise ValueError("Unlinked judge raw record")
    cases = {case.id: case for case in dataset.cases}
    for row in rows:
        accounting = manifest.judge_accounting.get(str(row.attempt_id))
        evaluation = manifest.judge_evaluations.get(str(row.attempt_id))
        answered = isinstance(row.response, AskResponse) and row.response.status == "answered"
        if not answered:
            if accounting is not None or evaluation is not None:
                raise ValueError("Nonanswered output cannot have a judge")
            continue
        if accounting is None:
            if require_complete:
                raise ValueError("Answered output lacks valid judge evidence")
            continue
        if require_complete and (evaluation is None or not accounting.cost.usage_complete):
            raise ValueError("Answered output lacks complete judge evidence")
        assert isinstance(row.response, AskResponse)
        case = cases[row.case_id]
        value = JudgeInput(
            required_claims=tuple(case.required_claims),
            evidence=tuple(
                corpus.chunk(identifier) for identifier in sorted(set(case.evidence_ids))
            ),
            expected_tool_result=case.expected_tool_result,
            response=row.response,
            actual_tool_result=row.actual_tool_result,
            cited_evidence=tuple(corpus.chunk(citation.id) for citation in row.response.citations),
        )
        context = TraceContext(
            attempt_id=row.attempt_id,
            trace_id=accounting.records[0].trace_id,
            phase="judge",
            source_commit=manifest.source_sha,
            config_hash=judge_config_hash(),
            corpus_hash=corpus.corpus_hash,
            pricing_hash=PRICING_HASH,
        )
        verify_judge_records(
            [record for record in raw if record.attempt_id == row.attempt_id],
            value=value,
            context=context,
            accounting=accounting,
            judgment=evaluation.judgment if evaluation is not None else None,
            run_id=manifest.run_id,
        )
    if require_complete and any(not row.usage_complete for row in (*warmups, *rows)):
        raise ValueError("Real collection serving usage is incomplete")


def verify_prepared_context(
    records: Sequence[TraceRecord],
    raw: Sequence[RawProviderRecord],
    *,
    mode: Mode,
    arm: Arm,
    corpus: Corpus,
    retrieved_ids: Sequence[str],
    foundation_evidence_ids: Sequence[str],
    initial_context_evidence_ids: Sequence[str],
) -> None:
    """Cross-link prepared endpoints without inventing an observed analysis request."""
    endpoints = {record.trace_id: record for record in records if record.record_kind == "endpoint"}
    analysis = [record for record in endpoints.values() if record.phase == "analysis"]
    if len(analysis) > 1:
        raise ValueError("An attempt cannot have multiple analysis contexts")
    fields = (
        "retrieved_evidence_ids",
        "foundation_evidence_ids",
        "initial_context_evidence_ids",
    )
    row_values = (
        list(retrieved_ids),
        list(foundation_evidence_ids),
        list(initial_context_evidence_ids),
    )
    if not analysis:
        if any(row_values):
            raise ValueError("Unstarted analysis cannot have prepared context")
    else:
        endpoint = analysis[0]
        if row_values != tuple(list(getattr(endpoint, field)) for field in fields):
            raise ValueError("Row context differs from the prepared analysis endpoint")
        complete = context_provenance(mode, arm, tuple(retrieved_ids), corpus)
        assembled = (complete.foundation_evidence_ids, complete.initial_context_evidence_ids)
        # Validated seeds can precede a failed assembly. An absent raw request
        # must not erase that prepared R or falsely establish zero retrieval.
        if row_values[1:] not in (assembled, ([], [])):
            raise ValueError("Prepared context does not match its mode and canonical seeds")
        if any(record.phase == "analysis" for record in raw) and row_values[1:] != assembled:
            raise ValueError("Observed analysis requires complete assembled context")
    for record in raw:
        matching_endpoint = endpoints.get(record.trace_id)
        if (
            matching_endpoint is None
            or matching_endpoint.phase != record.phase
            or matching_endpoint.attempt_id != record.attempt_id
        ):
            raise ValueError("Raw context is not linked to its matching endpoint")
        values = tuple(list(getattr(record.context_provenance, field)) for field in fields)
        expected = (
            tuple(list(getattr(matching_endpoint, field)) for field in fields)
            if record.phase == "analysis"
            else ([], [], [])
        )
        if values != expected:
            raise ValueError("Raw event context differs from the matching endpoint")


def _verify_observations(
    row: ResultRow,
    case: GoldCase,
    records: list[TraceRecord],
    raw: list[dict[str, Any]],
    corpus: Corpus,
) -> None:
    endpoints = {
        record.phase: record
        for record in records
        if record.attempt_id == row.attempt_id and record.record_kind == "endpoint"
    }
    if len(endpoints) != len(row.trace_ids) or "judge" in endpoints:
        raise ValueError("Unexpected serving phases")
    expected_phases = (
        ["analysis"]
        if case.letter is None
        else ["extraction"] + (["analysis"] if "analysis" in endpoints else [])
    )
    if row.trace_ids != [
        endpoints[phase].trace_id for phase in expected_phases if phase in endpoints
    ]:
        raise ValueError("Serving trace order does not match phases")
    if (
        case.letter is None
        and set(endpoints) != {"analysis"}
        or case.letter is not None
        and "extraction" not in endpoints
    ):
        raise ValueError("Serving phases do not match the scenario")
    attempts = [item for item in raw if item["attempt_id"] == str(row.attempt_id)]
    verify_prepared_context(
        [record for record in records if record.attempt_id == row.attempt_id],
        [RawProviderRecord.model_validate_json(json.dumps(item)) for item in attempts],
        mode=case.request.mode,
        arm=row.arm,
        corpus=corpus,
        retrieved_ids=row.retrieved_ids,
        foundation_evidence_ids=row.foundation_evidence_ids,
        initial_context_evidence_ids=row.initial_context_evidence_ids,
    )
    if isinstance(row.response, AskResponse):
        final_events = [
            item
            for item in attempts
            if item["phase"] == "analysis"
            and item["operation"] == "generation"
            and item["event"] != "request"
        ]
        if not final_events or final_events[-1]["event"] != "response":
            raise ValueError("Final answer lacks a generated response")
        try:
            generated = _generated(Message.model_validate(final_events[-1]["value"]))
        except Exception:
            raise ValueError("Raw generated content cannot produce the stored response") from None
        status = "answered" if generated.kind == "answer" else "refused"
        answer_text = (
            REFUSAL_TEXT[generated.refusal_reason]
            if generated.refusal_reason is not None
            else _ANSWER_SEPARATOR.join(statement.text for statement in generated.statements)
        )
        cited_ids = sorted(
            {
                identifier
                for statement in generated.statements
                for identifier in statement.citation_ids
            }
        )
        if (
            row.response.status != status
            or row.response.statements != generated.statements
            or row.response.answer != answer_text
            or row.response.citations != [corpus.citation(identifier) for identifier in cited_ids]
            or list(endpoints["analysis"].cited_evidence_ids) != cited_ids
            or endpoints["analysis"].response_code != status
        ):
            raise ValueError("Final response does not match generated content and metadata")
    extracted: Extraction | None = None
    if case.letter is not None and endpoints["extraction"].response_code == "ok":
        returned = [
            item["value"]
            for item in attempts
            if item["phase"] == "extraction"
            and item["operation"] == "generation"
            and item["event"] == "response"
        ]
        if len(returned) != 1:
            raise ValueError("Missing extraction response")
        extracted = Extraction.model_validate_json(returned[0]["content"][0]["text"])
    if row.actual_extract != extracted:
        raise ValueError("Extraction observation mismatch")
    actual_args: NoticeFacts | RentFacts | None = None
    actual_result: ToolResult | None = None
    for item in attempts:
        if item["phase"] != "analysis" or item["event"] != "request":
            continue
        messages = item["value"]["messages"]
        if len(messages) != 3:
            continue
        calls = [block for block in messages[1]["content"] if block["type"] == "tool_use"]
        outgoing = [block for block in messages[2]["content"] if block["type"] == "tool_result"]
        if len(calls) != 1 or len(outgoing) != 1 or calls[0]["id"] != outgoing[0]["tool_use_id"]:
            raise ValueError("Tool observation lacks matching native result")
        actual_result = ToolResult.model_validate_json(outgoing[0]["content"])
        facts_type = RentFacts if case.kind == "rent" else NoticeFacts
        actual_args = facts_type.model_validate_json(json.dumps(calls[0]["input"]))
        if actual_result.tool != calls[0]["name"] or actual_args != case.expected_tool_args:
            raise ValueError("Executed arguments differ from confirmed facts")
    if isinstance(row.response, AskResponse) and row.response.tool_result is not None:
        if actual_result is not None and actual_result != row.response.tool_result:
            raise ValueError("Final tool result differs from observed execution")
        actual_result, actual_args = row.response.tool_result, case.expected_tool_args
    if row.actual_tool_args != actual_args or row.actual_tool_result != actual_result:
        raise ValueError("Tool observations mismatch")
    assertions: list[DeterministicAssertion] = []
    if extracted is not None:
        matches = extracted == case.expected_extract
        assertions.append(DeterministicAssertion(id="extraction_exact", passed=matches))
        if not matches and "analysis" in endpoints:
            raise ValueError("Mismatched extraction continued to analysis")
    if "analysis" in endpoints and case.kind != "qa":
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
    if row.deterministic_assertions != assertions:
        raise ValueError("Exact assertions do not match actual observations")
    decision = classify(
        row.response,
        assertions,
        row.judge,
        expected_claim_ids=[claim.id for claim in case.required_claims],
        arm=row.arm,
    )
    if row.classification != decision.classification:
        raise ValueError("Result classification mismatch")
    for item in attempts:
        if (
            item["phase"] not in endpoints
            or UUID(item["trace_id"]) != endpoints[item["phase"]].trace_id
        ):
            raise ValueError("Raw provider phase linkage mismatch")
