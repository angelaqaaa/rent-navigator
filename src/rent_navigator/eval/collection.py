"""Append-only synthetic collection artifacts and strict integrity verification."""

import json
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from anthropic.types import Message

from rent_navigator.agent import _ANSWER_SEPARATOR, REFUSAL_TEXT, _generated
from rent_navigator.corpus import Corpus
from rent_navigator.eval.data import GoldDataset
from rent_navigator.eval.metrics import aggregate, classify
from rent_navigator.eval.models import CASE_IDS, DeterministicAssertion, GoldCase, ResultRow
from rent_navigator.eval.offline import configuration_identity_hash
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
from rent_navigator.models import (
    AskResponse,
    CanonicalUUID,
    Extraction,
    NoticeFacts,
    RentFacts,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolResult,
)
from rent_navigator.provider import MessagesPort, SpendBudget, _usage
from rent_navigator.security_cases import security_cases_hash
from rent_navigator.trace import PRICING_HASH, TraceContext, TraceRecord, provider_cost_totals

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


class CollectionManifest(StrictModel):
    schema_version: Literal[1]
    execution_mode: Literal["synthetic"]
    reportable: Literal[False]
    evaluation_complete: Literal[False]
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


class BatchLedger(Protocol):
    """Reserve the complete forecast before starting any collection operations."""

    def reserve_batch(self, forecast_usd: Decimal) -> None: ...


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
    manifest = CollectionManifest(
        schema_version=1,
        execution_mode="synthetic",
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
        reasons=["synthetic_execution"],
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "plan.json").write_text(plan.model_dump_json() + "\n")
    for name in ARTIFACT_FILES[1:]:
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
        summary.update(execution_mode="synthetic", reportable=False, evaluation_complete=False)
        (output_dir / "summary.json").write_text(_encode(summary) + "\n")
        manifest.files = {
            name: sha256((output_dir / name).read_bytes()).hexdigest() for name in ARTIFACT_FILES
        }
        manifest_path.write_text(manifest.model_dump_json() + "\n")

    save()
    cases = {case.id: case for case in dataset.cases}
    allowlist = SyntheticAllowlist.from_fixtures(dataset.cases)
    try:
        batch_ledger.reserve_batch(forecast_usd)
        with (
            (output_dir / "metadata.jsonl").open("a") as metadata,
            (output_dir / "raw-provider.jsonl").open("a") as raw,
        ):
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
                    save()
                    if "interrupted" in outcome.reasons:
                        return manifest
                    if "judge_usage_missing" in outcome.reasons or (
                        "judge_invalid" in outcome.reasons and outcome.judge_accounting is None
                    ):
                        manifest.reasons.append("budget_reforecast_required")
                        return manifest
        manifest.collection_complete = True
    except BaseException:
        manifest.reasons.append("collection_interrupted")
        raise
    finally:
        save()
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
    """Verify bytes, plan coverage, source identity and cross-file trace accounting."""
    raw_manifest = (directory / "manifest.json").read_bytes()
    if sha256(raw_manifest).hexdigest() != manifest_sha256:
        raise ValueError("Collection manifest digest mismatch")
    manifest = CollectionManifest.model_validate_json(raw_manifest)
    if set(path.name for path in directory.iterdir()) != set(ARTIFACT_FILES) | {"manifest.json"}:
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
        or set(manifest.files) != set(ARTIFACT_FILES)
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
            retrieved = [
                identifier
                for endpoint in endpoints
                if endpoint.phase == "analysis"
                for identifier in endpoint.retrieved_evidence_ids
            ]
            if row.retrieved_ids != retrieved:
                raise ValueError("Result retrieval observations mismatch")
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
            if set(item["value"]) != {"input_tokens"}:
                raise ValueError("Invalid raw token count response")
            tokens = item["value"]["input_tokens"]
            if call.response_code == "ok" and (type(tokens) is not int or tokens < 0):
                raise ValueError("Invalid successful token count")
        if item["event"] == "response" and item["operation"] == "generation":
            if (
                call.returned_model_id is not None
                and item["value"].get("model") != call.returned_model_id
            ):
                raise ValueError("Raw returned model does not match call metadata")
            if call.usage_complete:
                usage = item["value"].get("usage", {})
                if not isinstance(usage, dict):
                    raise ValueError("Invalid raw usage")
                if any(
                    type(usage.get(name)) is not int for name in ("input_tokens", "output_tokens")
                ):
                    raise ValueError("Raw token counts must be strict integers")
                if _usage(Message.model_validate(item["value"])) is None:
                    raise ValueError("Raw usage includes unpriced categories")
                if (usage.get("input_tokens"), usage.get("output_tokens")) != (
                    call.input_tokens,
                    call.output_tokens,
                ):
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
    expected_summary.update(execution_mode="synthetic", reportable=False, evaluation_complete=False)
    if json.loads((directory / "summary.json").read_text()) != json.loads(
        _encode(expected_summary)
    ):
        raise ValueError("Collection summary mismatch")
    return manifest


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
