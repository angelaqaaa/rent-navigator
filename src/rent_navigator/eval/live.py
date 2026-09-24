"""One funded 32-case/seven-safety gate and independently verified artifacts."""

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from rent_navigator.corpus import Corpus
from rent_navigator.eval.critical import critical_gold
from rent_navigator.eval.data import GoldDataset
from rent_navigator.eval.evidence import verify_gold_observations, verify_raw_links
from rent_navigator.eval.judge import RealJudge, judge_config_hash
from rent_navigator.eval.live_budget import (
    BudgetEvent,
    BudgetReceipt,
    FundedMessages,
    LiveBudget,
    receipt_from_events,
)
from rent_navigator.eval.metrics import classify
from rent_navigator.eval.models import CASE_IDS, Activation, ResultRow
from rent_navigator.eval.offline import (
    Baseline,
    OfflineManifest,
    OfflineReport,
    _manifest,
    _report,
    canonical_hash,
    configuration_identity_hash,
    file_hash,
)
from rent_navigator.eval.permit import LivePermit
from rent_navigator.eval.recording import RawProviderRecord, SyntheticAllowlist
from rent_navigator.eval.runner import (
    AttemptIdentity,
    JudgeAccounting,
    JudgeEvaluation,
    PlanEntry,
    config_map,
    protocol_hash,
    run_attempt,
)
from rent_navigator.eval.safety_execution import (
    SecurityOutcome,
    SecurityResultRow,
    run_security_attempt,
    verify_security_outcome,
)
from rent_navigator.eval.security import parse_security_report
from rent_navigator.index import SearchHit
from rent_navigator.models import CanonicalUUID, ErrorResponse, Sha256, SourceCommit, StrictModel
from rent_navigator.provider import MessagesPort
from rent_navigator.security_cases import load_security_cases, security_cases_hash
from rent_navigator.trace import PRICING_HASH, TraceRecord, provider_cost_totals

LIVE_SECURITY_IDS = ("S01", "S02", "S03", "S04", "S05", "S07", "S08")
PAYLOAD_FILES = (
    "plan.json",
    "results.jsonl",
    "security-results.jsonl",
    "metadata.jsonl",
    "raw-provider.jsonl",
    "judge-metadata.jsonl",
    "judge-raw.jsonl",
    "summary.json",
    "budget.jsonl",
    "receipt.json",
    "offline/manifest.json",
    "offline/report.json",
    "offline/security.xml",
    "offline-activation.json",
    "offline-baseline.json",
)


class LiveGatePlan(StrictModel):
    schema_version: Literal[1]
    purpose: Literal["live_gate"]
    batch_uuid: CanonicalUUID
    gold: list[PlanEntry]
    security: list[str]

    @model_validator(mode="after")
    def fixed_inventory(self) -> "LiveGatePlan":
        expected = [
            PlanEntry(case_id=case, arm="production", repeat=repeat)
            for repeat in (0, 1)
            for case in CASE_IDS
        ]
        if self.gold != expected or self.security != list(LIVE_SECURITY_IDS):
            raise ValueError("Live plan must preserve the fixed 32+7 serial order")
        return self


def build_live_plan(batch_uuid: UUID | None = None) -> LiveGatePlan:
    return LiveGatePlan(
        schema_version=1,
        purpose="live_gate",
        batch_uuid=batch_uuid or uuid4(),
        gold=[
            PlanEntry(case_id=case, arm="production", repeat=repeat)
            for repeat in (0, 1)
            for case in CASE_IDS
        ],
        security=list(LIVE_SECURITY_IDS),
    )


class StartedLiveAttempt(StrictModel):
    position: Annotated[int, Field(ge=0, le=38)]
    attempt_id: CanonicalUUID


class LiveSummary(StrictModel):
    purpose: Literal["live_gate"]
    reportable: Literal[False]
    gold_count: Annotated[int, Field(ge=0, le=32)]
    security_count: Annotated[int, Field(ge=0, le=7)]
    successes: Annotated[int, Field(ge=0, le=32)]
    required_successes: Annotated[int, Field(ge=28, le=32)]
    critical_flags: dict[str, list[str]]
    security_passes: dict[str, bool]
    complete: bool
    passed: bool


class LiveManifest(StrictModel):
    schema_version: Literal[1]
    purpose: Literal["live_gate"]
    execution_mode: Literal["live"]
    reportable: Literal[False]
    evaluation_complete: bool
    passed: bool
    repository: str
    workflow: str
    workflow_run_id: str
    workflow_run_attempt: Annotated[int, Field(ge=1)]
    batch_uuid: CanonicalUUID
    source_sha: SourceCommit
    gold_hash: Sha256
    approval_hash: Sha256
    corpus_hash: Sha256
    security_hash: Sha256
    pricing_hash: Sha256
    lock_hash: Sha256
    serving_config: dict[str, Sha256]
    judge_config_hash: Sha256
    protocol_hash: Sha256
    config_hash: Sha256
    offline_manifest_sha256: Sha256
    permit: LivePermit
    started_attempts: list[StartedLiveAttempt]
    judge_accounting: dict[str, JudgeAccounting]
    judge_evaluations: dict[str, JudgeEvaluation]
    reasons: list[str]
    files: dict[str, Sha256]


def live_protocol_hash() -> str:
    return canonical_hash(
        {
            "collection_protocol": protocol_hash(),
            "live_order": [(case, repeat) for repeat in (0, 1) for case in CASE_IDS],
            "security_order": LIVE_SECURITY_IDS,
            "critical": "native-gold-v1",
            "threshold": "max(28,B-2)",
            "policy": "safety-rejection-v1",
        }
    )


def live_config_hash() -> str:
    return configuration_identity_hash(live_protocol_hash(), config_map(), judge_config_hash())


def summarize_gate(
    rows: list[ResultRow],
    security: list[SecurityResultRow],
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    raw: list[RawProviderRecord],
    baseline: Baseline | None,
    accounting_complete: bool,
) -> LiveSummary:
    expected = [(case, repeat) for repeat in (0, 1) for case in CASE_IDS]
    if [(row.case_id, row.repeat) for row in rows] != expected[: len(rows)] or any(
        row.arm != "production" for row in rows
    ):
        raise ValueError("Gold gate rows are missing, duplicated or reordered")
    if [row.case_id for row in security] != list(LIVE_SECURITY_IDS[: len(security)]):
        raise ValueError("Safety gate rows are missing, duplicated or reordered")
    attempts = [row.attempt_id for row in rows] + [row.attempt_id for row in security]
    if len(set(attempts)) != len(attempts):
        raise ValueError("Duplicate gate attempt identity")
    complete = accounting_complete and len(rows) == 32 and len(security) == 7
    flags: dict[str, list[str]] = {}
    successes = 0
    cases = {case.id: case for case in dataset.cases}
    for row in rows:
        case = cases[row.case_id]
        decision = classify(
            row.response,
            row.deterministic_assertions,
            row.judge,
            expected_claim_ids=[claim.id for claim in case.required_claims],
            arm="production",
        )
        if row.classification != decision.classification:
            raise ValueError("Stored classification differs from observations")
        observed = critical_gold(
            case, row, [r for r in raw if r.attempt_id == row.attempt_id], corpus
        )
        current = list(observed.flags)
        if decision.citation_failure:
            current.append("judge_citation_failure")
        if row.judge is not None and row.judge.false_pass:
            current.append("false_pass")
        if row.judge is not None and row.judge.policy_violations:
            current.append("policy_violation")
        if current:
            flags[str(row.attempt_id)] = sorted(set(current))
        complete = (
            complete and decision.evaluation_complete and observed.complete and row.usage_complete
        )
        if isinstance(row.response, ErrorResponse) and row.response.error.code not in (
            "invalid_generated_output",
            "tool_protocol_error",
        ):
            complete = False
        successes += decision.classification == "success"
    safe: dict[str, bool] = {row.case_id: row.safe_pass for row in security}
    complete = complete and all(row.disposition != "incomplete" for row in security)
    required = max(28, baseline.B - 2) if baseline is not None else 28
    return LiveSummary(
        purpose="live_gate",
        reportable=False,
        gold_count=len(rows),
        security_count=len(security),
        successes=successes,
        required_successes=required,
        critical_flags=flags,
        security_passes=safe,
        complete=complete,
        passed=complete and successes >= required and not flags and all(safe.values()),
    )


def _json_rows(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _save_json(path: Path, value: StrictModel) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value.model_dump_json(indent=2) + "\n")
    temporary.replace(path)


def _offline_evidence(
    data_dir: Path,
    directory: Path,
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_path: Path,
) -> OfflineReport:
    """Reproduce historical offline evidence while preserving its activation bytes."""
    actual = OfflineReport.model_validate_json((directory / "offline/report.json").read_bytes())
    manifest = OfflineManifest.model_validate_json(
        (directory / "offline/manifest.json").read_bytes()
    )
    activation = Activation.model_validate_json(
        (directory / "offline-activation.json").read_bytes()
    )
    with TemporaryDirectory(prefix="rent-live-offline-") as temporary:
        location = Path(temporary)
        (location / "activation.json").write_bytes(
            (directory / "offline-activation.json").read_bytes()
        )
        baseline_bytes = (directory / "offline-baseline.json").read_bytes()
        if activation.baseline_phase == "active":
            Baseline.model_validate_json(baseline_bytes)
            (location / "baseline.json").write_bytes(baseline_bytes)
        elif json.loads(baseline_bytes) is not None:
            raise ValueError("Pending historical activation cannot contain a baseline")
        security = parse_security_report(directory / "offline/security.xml")
        expected = _report(
            replace(dataset, activation=activation),
            location,
            corpus,
            source_sha,
            security,
            lock_path,
        )
    if (
        actual != expected
        or not actual.offline_complete
        or manifest != _manifest(expected, directory / "offline")
    ):
        raise ValueError("Historical offline evidence differs from actual deterministic checks")
    return actual


async def run_live_gate(
    data_dir: Path,
    output_dir: Path,
    *,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_path: Path,
    offline_dir: Path,
    permit: LivePermit,
    messages: MessagesPort,
    retrieve: Callable[[str], tuple[SearchHit, ...]],
) -> LiveManifest:
    if permit.source_sha != source_sha or dataset.approval.status != "approved":
        raise ValueError("Live execution requires a matching paid grant and approved dataset")
    baseline_path = data_dir / "baseline.json"
    baseline_hash = (
        file_hash(baseline_path) if dataset.activation.baseline_phase == "active" else None
    )
    permit.validate_phase(
        baseline_phase=dataset.activation.baseline_phase, baseline_sha256=baseline_hash
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    plan = build_live_plan(permit.batch_uuid)
    (output_dir / "plan.json").write_text(plan.model_dump_json() + "\n")
    shutil.copytree(offline_dir, output_dir / "offline")
    (output_dir / "offline-activation.json").write_bytes(
        (data_dir / "activation.json").read_bytes()
    )
    if baseline_hash is None:
        (output_dir / "offline-baseline.json").write_text("null\n")
    else:
        (output_dir / "offline-baseline.json").write_bytes(baseline_path.read_bytes())
    for name in PAYLOAD_FILES:
        if not (output_dir / name).exists():
            (output_dir / name).touch(exist_ok=False)
    offline = _offline_evidence(
        data_dir,
        output_dir,
        dataset=dataset,
        corpus=corpus,
        source_sha=source_sha,
        lock_path=lock_path,
    )
    manifest = LiveManifest(
        schema_version=1,
        purpose="live_gate",
        execution_mode="live",
        reportable=False,
        evaluation_complete=False,
        passed=False,
        repository=permit.repository,
        workflow=permit.workflow,
        workflow_run_id=permit.run_id,
        workflow_run_attempt=permit.run_attempt,
        batch_uuid=permit.batch_uuid,
        source_sha=source_sha,
        gold_hash=dataset.gold_hash,
        approval_hash=dataset.approval_hash,
        corpus_hash=corpus.corpus_hash,
        security_hash=security_cases_hash(),
        pricing_hash=PRICING_HASH,
        lock_hash=file_hash(lock_path),
        serving_config=config_map(),
        judge_config_hash=judge_config_hash(),
        protocol_hash=live_protocol_hash(),
        config_hash=live_config_hash(),
        offline_manifest_sha256=file_hash(output_dir / "offline/manifest.json"),
        permit=permit,
        started_attempts=[],
        judge_accounting={},
        judge_evaluations={},
        reasons=[],
        files={},
    )
    rows: list[ResultRow] = []
    safety_rows: list[SecurityResultRow] = []
    security_cases = {str(c.id): c for c in load_security_cases(corpus=corpus)}
    cases = {c.id: c for c in dataset.cases}
    allowlist = SyntheticAllowlist.from_fixtures([*dataset.cases, *security_cases.values()])
    baseline = (
        Baseline.model_validate_json(baseline_path.read_bytes())
        if baseline_hash is not None
        else None
    )
    del offline
    with (
        (output_dir / "metadata.jsonl").open("a") as metadata,
        (output_dir / "raw-provider.jsonl").open("a") as raw_stream,
        (output_dir / "judge-metadata.jsonl").open("a") as judge_metadata,
        (output_dir / "judge-raw.jsonl").open("a") as judge_raw,
        (output_dir / "budget.jsonl").open("a") as budget_stream,
    ):
        budget = LiveBudget(permit, budget_stream, config_hash=manifest.config_hash)
        guarded_messages = FundedMessages(messages, budget=budget)
        judge = RealJudge(
            guarded_messages,
            budget=budget,
            metadata=judge_metadata,
            raw_provider=judge_raw,
            run_id=permit.batch_uuid,
        )

        def save() -> None:
            raw = [
                RawProviderRecord.model_validate_json(line)
                for line in _json_rows(output_dir / "raw-provider.jsonl")
            ]
            receipt = budget.receipt()
            summary = summarize_gate(
                rows,
                safety_rows,
                dataset=dataset,
                corpus=corpus,
                raw=raw,
                baseline=baseline,
                accounting_complete=receipt.complete and not manifest.reasons,
            )
            manifest.evaluation_complete, manifest.passed = summary.complete, summary.passed
            _save_json(output_dir / "summary.json", summary)
            _save_json(output_dir / "receipt.json", receipt)
            manifest.files = {name: file_hash(output_dir / name) for name in PAYLOAD_FILES}
            _save_json(output_dir / "manifest.json", manifest)

        save()
        try:
            for position in range(39):
                if budget.stopped:
                    manifest.reasons.append("budget_reforecast_required")
                    break
                attempt_id = uuid4()
                manifest.started_attempts.append(
                    StartedLiveAttempt(position=position, attempt_id=attempt_id)
                )
                budget.bind(attempt_id)
                save()
                identity = AttemptIdentity(
                    run_id=plan.batch_uuid, source_sha=source_sha, gold_hash=dataset.gold_hash
                )
                if position < 32:
                    entry = plan.gold[position]
                    outcome = await run_attempt(
                        cases[entry.case_id],
                        entry,
                        identity=identity,
                        corpus=corpus,
                        messages=guarded_messages,
                        budget=budget,
                        allowlist=allowlist,
                        retrieve=retrieve,
                        metadata=metadata,
                        raw_provider=raw_stream,
                        judge=judge,
                        judge_config_hash=manifest.judge_config_hash,
                        attempt_id=attempt_id,
                    )
                    rows.append(outcome.row)
                    row: ResultRow | SecurityResultRow = outcome.row
                    accounting, judgment, reasons = (
                        outcome.judge_accounting,
                        outcome.judge_evaluation,
                        outcome.reasons,
                    )
                    destination = "results.jsonl"
                else:
                    safety_outcome: SecurityOutcome = await run_security_attempt(
                        security_cases[plan.security[position - 32]],
                        identity=identity,
                        corpus=corpus,
                        messages=guarded_messages,
                        budget=budget,
                        allowlist=allowlist,
                        retrieve=retrieve,
                        metadata=metadata,
                        raw_provider=raw_stream,
                        judge=judge,
                        judge_config_hash=manifest.judge_config_hash,
                        attempt_id=attempt_id,
                    )
                    safety_rows.append(safety_outcome.row)
                    row = safety_outcome.row
                    accounting, judgment, reasons = (
                        safety_outcome.judge_accounting,
                        safety_outcome.judge_evaluation,
                        safety_outcome.reasons,
                    )
                    destination = "security-results.jsonl"
                with (output_dir / destination).open("a") as stream:
                    stream.write(row.model_dump_json() + "\n")
                    stream.flush()
                if accounting is not None:
                    manifest.judge_accounting[str(attempt_id)] = accounting
                if judgment is not None:
                    manifest.judge_evaluations[str(attempt_id)] = judgment
                stop_reasons = {
                    "interrupted",
                    "judge_invalid",
                    "judge_missing",
                    "judge_usage_missing",
                    "serving_usage_missing",
                    "observation_missing",
                }
                if stop_reasons.intersection(reasons):
                    manifest.reasons.extend(sorted(stop_reasons.intersection(reasons)))
                if isinstance(row.response, ErrorResponse) and row.response.error.code not in (
                    "invalid_generated_output",
                    "tool_protocol_error",
                ):
                    manifest.reasons.append("infrastructure_failure")
                if position >= 32 and safety_outcome.row.disposition == "incomplete":
                    manifest.reasons.append("security_observation_incomplete")
                if position < 32:
                    observed_raw = [
                        RawProviderRecord.model_validate_json(line)
                        for line in _json_rows(output_dir / "raw-provider.jsonl")
                    ]
                    if not critical_gold(
                        cases[entry.case_id],
                        outcome.row,
                        [r for r in observed_raw if r.attempt_id == attempt_id],
                        corpus,
                    ).complete:
                        manifest.reasons.append("observation_missing")
                attempt_records = outcome.records if position < 32 else safety_outcome.records
                if any(
                    r.provider_operation == "generation"
                    and r.returned_model_id != r.requested_model_id
                    for r in attempt_records
                ):
                    manifest.reasons.append("provider_model_mismatch")
                save()
                if manifest.reasons:
                    break
        except BaseException:
            manifest.reasons.append("interrupted")
            raise
        finally:
            save()
    return manifest


def verify_live_gate(
    directory: Path,
    *,
    data_dir: Path,
    dataset: GoldDataset,
    corpus: Corpus,
    source_sha: str,
    lock_path: Path,
    manifest_sha256: str,
    require_pass: bool = True,
    expected_run_id: str | None = None,
    expected_run_attempt: int | None = None,
    expected_batch_uuid: UUID | None = None,
) -> LiveManifest:
    """Verify payloads, native observations, real judging and billing independently."""
    from rent_navigator.eval.judge import RawJudgeRecord, verify_judge_records
    from rent_navigator.eval.runner import JudgeInput
    from rent_navigator.eval.safety_execution import safety_judge_input
    from rent_navigator.models import AskResponse
    from rent_navigator.trace import TraceContext

    if file_hash(directory / "manifest.json") != manifest_sha256:
        raise ValueError("Live producer manifest digest mismatch")
    manifest = LiveManifest.model_validate_json((directory / "manifest.json").read_bytes())
    names = {str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file()}
    if names != set(PAYLOAD_FILES) | {"manifest.json"} or any(
        path.is_symlink() for path in directory.rglob("*")
    ):
        raise ValueError("Live artifact inventory mismatch")
    if set(manifest.files) != set(PAYLOAD_FILES) or any(
        file_hash(directory / name) != digest for name, digest in manifest.files.items()
    ):
        raise ValueError("Live payload digest mismatch")
    if (
        manifest.source_sha != source_sha
        or manifest.gold_hash != dataset.gold_hash
        or manifest.approval_hash != dataset.approval_hash
        or manifest.corpus_hash != corpus.corpus_hash
        or manifest.security_hash != security_cases_hash()
        or manifest.pricing_hash != PRICING_HASH
        or manifest.lock_hash != file_hash(lock_path)
        or manifest.serving_config != config_map()
        or manifest.judge_config_hash != judge_config_hash()
        or manifest.protocol_hash != live_protocol_hash()
        or manifest.config_hash != live_config_hash()
        or manifest.offline_manifest_sha256 != file_hash(directory / "offline/manifest.json")
    ):
        raise ValueError("Live evidence source or configuration mismatch")
    if (
        (expected_run_id is not None and manifest.workflow_run_id != expected_run_id)
        or (
            expected_run_attempt is not None
            and manifest.workflow_run_attempt != expected_run_attempt
        )
        or (expected_batch_uuid is not None and manifest.batch_uuid != expected_batch_uuid)
    ):
        raise ValueError("Live manifest differs from the verified artifact producer")
    permit = manifest.permit
    if (
        manifest.repository,
        manifest.workflow,
        manifest.workflow_run_id,
        manifest.workflow_run_attempt,
        manifest.batch_uuid,
        manifest.source_sha,
    ) != (
        permit.repository,
        permit.workflow,
        permit.run_id,
        permit.run_attempt,
        permit.batch_uuid,
        permit.source_sha,
    ):
        raise ValueError("Live evidence differs from the funded grant")
    offline_report = _offline_evidence(
        data_dir,
        directory,
        dataset=dataset,
        corpus=corpus,
        source_sha=source_sha,
        lock_path=lock_path,
    )
    plan = LiveGatePlan.model_validate_json((directory / "plan.json").read_bytes())
    if plan.batch_uuid != manifest.batch_uuid:
        raise ValueError("Live plan batch mismatch")
    rows = [ResultRow.model_validate_json(line) for line in _json_rows(directory / "results.jsonl")]
    safety = [
        SecurityResultRow.model_validate_json(line)
        for line in _json_rows(directory / "security-results.jsonl")
    ]
    all_rows: list[ResultRow | SecurityResultRow] = [*rows, *safety]
    if [s.position for s in manifest.started_attempts] != list(
        range(len(manifest.started_attempts))
    ):
        raise ValueError("Started live attempts differ from serial plan")
    if [s.attempt_id for s in manifest.started_attempts] != [row.attempt_id for row in all_rows]:
        raise ValueError("Started live attempt is missing or has duplicate evidence")
    records = [
        TraceRecord.model_validate_json(line) for line in _json_rows(directory / "metadata.jsonl")
    ]
    raw = [
        RawProviderRecord.model_validate_json(line)
        for line in _json_rows(directory / "raw-provider.jsonl")
    ]
    judge_records = [
        TraceRecord.model_validate_json(line)
        for line in _json_rows(directory / "judge-metadata.jsonl")
    ]
    judge_raw = [
        RawJudgeRecord.model_validate_json(line)
        for line in _json_rows(directory / "judge-raw.jsonl")
    ]
    verify_raw_links(raw, records, manifest.batch_uuid)
    cases = {c.id: c for c in dataset.cases}
    security_cases = {str(c.id): c for c in load_security_cases(corpus=corpus)}
    traces: set[UUID] = set()
    for row in all_rows:
        if traces.intersection(row.trace_ids):
            raise ValueError("Reused serving trace identifier")
        traces.update(row.trace_ids)
        linked = [r for r in records if r.attempt_id == row.attempt_id]
        observed = [r for r in raw if r.attempt_id == row.attempt_id]
        if isinstance(row, ResultRow):
            verify_gold_observations(
                row,
                cases[row.case_id],
                records=linked,
                raw=observed,
                corpus=corpus,
                run_id=manifest.batch_uuid,
                source_sha=source_sha,
                gold_hash=dataset.gold_hash,
            )
        else:
            if (
                row.source_sha != source_sha
                or row.run_id != manifest.batch_uuid
                or row.gold_hash != dataset.gold_hash
            ):
                raise ValueError("Safety identity mismatch")
            verify_security_outcome(
                security_cases[row.case_id],
                row,
                linked,
                observed,
                corpus,
                manifest.judge_accounting.get(str(row.attempt_id)),
            )
        accounting = manifest.judge_accounting.get(str(row.attempt_id))
        evaluation = manifest.judge_evaluations.get(str(row.attempt_id))
        answered = isinstance(row.response, AskResponse) and row.response.status == "answered"
        if not answered and (
            accounting is not None
            or evaluation is not None
            or row.judge is not None
            or row.judge_cost_usd is not None
        ):
            raise ValueError("Pre-answer outcome unexpectedly contains judge work")
        if answered:
            if accounting is None or not isinstance(row.response, AskResponse):
                raise ValueError("Answered output lacks judge accounting")
            if row.judge_cost_usd != accounting.cost.actual_cost_usd:
                raise ValueError("Judge cost linkage mismatch")
            if evaluation is not None:
                if (
                    row.judge != evaluation.judgment
                    or accounting
                    != JudgeAccounting.model_validate_json(
                        evaluation.model_dump_json(exclude={"judgment"})
                    )
                ):
                    raise ValueError("Judge evaluation linkage mismatch")
            elif row.judge is not None:
                raise ValueError("Parsed judge result lacks generation evidence")
            if isinstance(row, ResultRow):
                case = cases[row.case_id]
                value = JudgeInput(
                    required_claims=tuple(case.required_claims),
                    evidence=tuple(corpus.chunk(i) for i in sorted(set(case.evidence_ids))),
                    expected_tool_result=case.expected_tool_result,
                    response=row.response,
                    actual_tool_result=row.actual_tool_result,
                    cited_evidence=tuple(corpus.chunk(c.id) for c in row.response.citations),
                )
            else:
                value = safety_judge_input(
                    security_cases[row.case_id], row.response, row.actual_tool_result, corpus
                )
            if not accounting.records:
                raise ValueError("Missing judge trace")
            context = TraceContext(
                attempt_id=row.attempt_id,
                trace_id=accounting.records[0].trace_id,
                phase="judge",
                source_commit=source_sha,
                config_hash=manifest.judge_config_hash,
                corpus_hash=corpus.corpus_hash,
                pricing_hash=PRICING_HASH,
            )
            if context.trace_id in traces:
                raise ValueError("Reused judge trace identifier")
            traces.add(context.trace_id)
            linked_judge = [r for r in judge_records if r.attempt_id == row.attempt_id]
            if linked_judge != accounting.records:
                raise ValueError("Separate judge metadata mismatch")
            verify_judge_records(
                [r for r in judge_raw if r.attempt_id == row.attempt_id],
                value=value,
                context=context,
                accounting=accounting,
                judgment=row.judge,
                run_id=manifest.batch_uuid,
            )
    attempt_ids = {row.attempt_id for row in all_rows}
    if (
        set(manifest.judge_accounting) - {str(a) for a in attempt_ids}
        or set(manifest.judge_evaluations) - set(manifest.judge_accounting)
        or any(r.attempt_id not in attempt_ids for r in [*records, *judge_records])
        or any(str(r.attempt_id) not in manifest.judge_accounting for r in judge_raw)
    ):
        raise ValueError("Unlinked provider or judge evidence")
    if {r.trace_id for r in [*records, *judge_records]} != traces:
        raise ValueError("Unexpected metadata trace inventory")
    budget_lines = _json_rows(directory / "budget.jsonl")
    if not budget_lines:
        raise ValueError("Missing grant and generation reservation receipt")
    grant = json.loads(budget_lines[0])
    if grant != {
        "event": "grant",
        "permit": permit.model_dump(mode="json"),
        "config_hash": manifest.config_hash,
        "pricing_hash": PRICING_HASH,
    }:
        raise ValueError("Budget grant provenance mismatch")
    events = [BudgetEvent.model_validate_json(line) for line in budget_lines[1:]]
    receipt = receipt_from_events(permit, manifest.config_hash, events)
    if BudgetReceipt.model_validate_json((directory / "receipt.json").read_bytes()) != receipt:
        raise ValueError("Budget receipt totals mismatch")
    generations: list[TraceRecord] = []
    for row in all_rows:
        generations.extend(
            r
            for r in [*records, *judge_records]
            if r.attempt_id == row.attempt_id and r.provider_operation == "generation"
        )
    starts = [e for e in events if e.event == "generation_start"]
    completed = {e.call_id: e for e in events if e.event == "reconciled"}
    if len(starts) != len(generations):
        raise ValueError("Unaccounted generation or reservation")
    for reservation, detail in zip(starts, generations, strict=True):
        if (reservation.attempt_id, reservation.model, reservation.reserved_usd) != (
            detail.attempt_id,
            detail.requested_model_id,
            detail.reserved_cost_usd,
        ):
            raise ValueError("Generation reservation differs from actual provider call")
        reconciliation = completed.get(reservation.call_id)
        if reconciliation is None or reconciliation.cost is None:
            if require_pass:
                raise ValueError("Unresolved generation reservation")
        elif any(
            getattr(reconciliation.cost, name) != getattr(detail, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "actual_cost_usd",
                "reserved_cost_usd",
                "usage_complete",
            )
        ):
            raise ValueError("Generation reconciliation differs from actual cost")
        if detail.returned_model_id != detail.requested_model_id and require_pass:
            raise ValueError("Detected provider model mismatch")
    total = provider_cost_totals([*records, *judge_records])
    if receipt.complete and total.actual_cost_usd != receipt.actual_usd:
        raise ValueError("Serving plus judge spend differs from receipt")
    historical_phase = Activation.model_validate_json(
        (directory / "offline-activation.json").read_bytes()
    ).baseline_phase
    permit.validate_phase(
        baseline_phase=historical_phase,
        baseline_sha256=file_hash(directory / "offline-baseline.json")
        if historical_phase == "active"
        else None,
    )
    prior = json.loads((directory / "offline-baseline.json").read_text())
    baseline = Baseline.model_validate_json(json.dumps(prior)) if prior is not None else None
    summary = summarize_gate(
        rows,
        safety,
        dataset=dataset,
        corpus=corpus,
        raw=raw,
        baseline=baseline,
        accounting_complete=receipt.complete and not manifest.reasons,
    )
    if summary != LiveSummary.model_validate_json((directory / "summary.json").read_bytes()) or (
        manifest.evaluation_complete,
        manifest.passed,
    ) != (summary.complete, summary.passed):
        raise ValueError("Live summary cannot be reproduced from actual evidence")
    if dataset.activation.baseline_phase == "active":
        current_baseline = Baseline.model_validate_json((data_dir / "baseline.json").read_bytes())
        if permit.purpose == "bootstrap":
            expected_baseline = Baseline(
                schema_version=1,
                source_sha=source_sha,
                corpus_hash=corpus.corpus_hash,
                gold_hash=dataset.gold_hash,
                config_hash=manifest.config_hash,
                mrr_at_5=offline_report.production_retrieval.mrr_at_5,
                ndcg_at_5=offline_report.production_retrieval.ndcg_at_5,
                B=summary.successes,
                bootstrap_run_id=manifest.batch_uuid,
                bootstrap_manifest_sha256=manifest_sha256,
            )
            if current_baseline != expected_baseline:
                raise ValueError("Baseline attachment does not equal the actual passing bootstrap")
        elif file_hash(data_dir / "baseline.json") != permit.baseline_sha256:
            raise ValueError("Regression changed the historical comparison baseline")
    if require_pass and not summary.passed:
        raise ValueError("Live gate is incomplete or failed")
    return manifest
