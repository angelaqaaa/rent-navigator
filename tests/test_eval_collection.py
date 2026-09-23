"""Synthetic collections exercise the fixed schedule and persisted audit evidence."""

import asyncio
import json
import random
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from anthropic.types import Message, MessageTokensCount
from eval_fixtures import TOY_HASH, TOY_RUN_ID, TOY_SOURCE_SHA, write_toy_data
from test_eval_runner import JUDGE_CONFIG, Clock, SyntheticJudge, _harness

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.collection import (
    ARTIFACT_FILES,
    CollectionManifest,
    RunEnvironment,
    collect_synthetic,
    verify_collection,
)
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.models import CASE_IDS, GoldCase, ResultRow
from rent_navigator.eval.runner import CollectionPlan, PlanEntry, build_plan
from rent_navigator.index import SearchHit
from rent_navigator.provider import SpendLedger
from rent_navigator.trace import ACTOR_MODEL, TraceRecord, provider_cost_totals


def _message(content: list[dict[str, Any]], *, tool: bool = False) -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_collection",
            "type": "message",
            "role": "assistant",
            "model": ACTOR_MODEL,
            "content": content,
            "stop_reason": "tool_use" if tool else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )


def _responses(case: GoldCase) -> list[Message | BaseException]:
    responses: list[Message | BaseException] = []
    if case.letter is not None:
        assert case.expected_extract is not None
        responses.append(
            _message([{"type": "text", "text": case.expected_extract.model_dump_json()}])
        )
    if case.request.mode != "question":
        responses.append(
            _message(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_synthetic_collection",
                        "name": case.expected_tool,
                        "input": case.request.facts.model_dump(mode="json"),
                    }
                ],
                tool=True,
            )
        )
    responses.append(
        _message(
            [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "kind": "refusal",
                            "refusal_reason": "insufficient_evidence",
                            "statements": [],
                        }
                    ),
                }
            ]
        )
    )
    return responses


class ScriptedPort:
    def __init__(self, responses: list[Message | BaseException], events: list[str]) -> None:
        self.responses = iter(responses)
        self.events = events

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.events.append("count")
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        self.events.append("create")
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


class RecordingBatchLedger:
    def __init__(self, events: list[str], *, reject: bool = False) -> None:
        self.events = events
        self.forecasts: list[Decimal] = []
        self.reject = reject

    def reserve_batch(self, forecast_usd: Decimal) -> None:
        self.events.append("reserve_batch")
        self.forecasts.append(forecast_usd)
        if self.reject:
            raise RuntimeError("synthetic reservation rejected")


def _environment() -> RunEnvironment:
    return RunEnvironment(
        os="synthetic",
        cpu="synthetic",
        ram_bytes=None,
        docker_version=None,
        image_digest=None,
        source_clean=False,
        provider_access_date=None,
        pricing_url="https://example.invalid/synthetic-pricing",
    )


@dataclass(frozen=True)
class CollectionFixture:
    directory: Path
    dataset: GoldDataset
    corpus: Corpus
    manifest: CollectionManifest
    events: list[str]
    ledger: RecordingBatchLedger
    judge: SyntheticJudge | None = None


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory, corpus: Corpus) -> GoldDataset:
    directory = write_toy_data(tmp_path_factory.mktemp("collection-data"), corpus)
    return load_gold(directory, corpus)


def _collect(
    output_dir: Path,
    dataset: GoldDataset,
    corpus: Corpus,
    *,
    factory: Callable[[GoldCase, PlanEntry, bool], ScriptedPort] | None = None,
    events: list[str] | None = None,
    budget: SpendLedger | None = None,
    ledger: RecordingBatchLedger | None = None,
) -> CollectionFixture:
    events = [] if events is None else events
    budget = SpendLedger(Decimal("10")) if budget is None else budget
    ledger = RecordingBatchLedger(events) if ledger is None else ledger
    started = 0

    def default_factory(case: GoldCase, entry: PlanEntry, warmup: bool) -> ScriptedPort:
        nonlocal started
        assert events[0] == "reserve_batch"
        assert (output_dir / "plan.json").is_file()
        manifest = CollectionManifest.model_validate_json(
            (output_dir / "manifest.json").read_bytes()
        )
        assert len(manifest.started_attempts) == started + 1
        # Every previous result must be on disk before another attempt starts.
        persisted = sum(
            len((output_dir / name).read_text().splitlines())
            for name in ("results.jsonl", "warmups.jsonl")
        )
        assert persisted == started
        started += 1
        return ScriptedPort(_responses(case), events)

    manifest = asyncio.run(
        collect_synthetic(
            dataset,
            output_dir,
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            environment=_environment(),
            messages_factory=factory or default_factory,
            budget=budget,
            batch_ledger=ledger,
            forecast_usd=Decimal("6.308"),
            retrieve=lambda query: (),
            plan=build_plan(UUID(TOY_RUN_ID)),
        )
    )
    return CollectionFixture(output_dir, dataset, corpus, manifest, events, ledger)


@pytest.fixture(scope="module")
def completed(
    tmp_path_factory: pytest.TempPathFactory, dataset: GoldDataset, corpus: Corpus
) -> CollectionFixture:
    return _collect(tmp_path_factory.mktemp("collection-parent") / "run", dataset, corpus)


@pytest.fixture
def copied(tmp_path: Path, completed: CollectionFixture) -> Path:
    destination = tmp_path / "copy"
    shutil.copytree(completed.directory, destination)
    return destination


def _manifest_digest(directory: Path) -> str:
    return sha256((directory / "manifest.json").read_bytes()).hexdigest()


def _verify(
    directory: Path, completed: CollectionFixture, *, require_complete: bool = True
) -> CollectionManifest:
    return verify_collection(
        directory,
        dataset=completed.dataset,
        corpus=completed.corpus,
        source_sha=TOY_SOURCE_SHA,
        lock_hash=TOY_HASH,
        manifest_sha256=_manifest_digest(directory),
        require_complete=require_complete,
    )


def _read_rows(directory: Path, filename: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / filename).read_text().splitlines()]


def _write_rows(directory: Path, filename: str, values: list[dict[str, Any]]) -> None:
    (directory / filename).write_text("".join(json.dumps(value) + "\n" for value in values))


def _rehash(directory: Path) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"] = {
        filename: sha256((directory / filename).read_bytes()).hexdigest()
        for filename in ARTIFACT_FILES
    }
    path.write_text(json.dumps(manifest))


def test_plan_has_exact_warmups_fresh_shuffle_and_alternating_paired_arms() -> None:
    plan = build_plan(UUID(TOY_RUN_ID))
    assert [(entry.case_id, entry.arm, entry.repeat) for entry in plan.warmups] == [
        ("R01", "production", 0),
        ("N01", "production", 0),
        ("Q01", "production", 0),
        ("R01", "baseline", 0),
        ("N01", "baseline", 0),
        ("Q01", "baseline", 0),
    ]
    assert len(plan.measured) == 160
    randomizer = random.Random(42)
    for repetition in range(5):
        case_ids = sorted(CASE_IDS)
        randomizer.shuffle(case_ids)
        portion = plan.measured[repetition * 32 : (repetition + 1) * 32]
        assert [entry.case_id for entry in portion[::2]] == case_ids
        assert [entry.case_id for entry in portion[1::2]] == case_ids
        assert all(entry.repeat == repetition for entry in portion)
        for position in range(16):
            pair = portion[position * 2 : position * 2 + 2]
            assert [entry.arm for entry in pair] == (
                ["production", "baseline"]
                if (repetition + position) % 2 == 0
                else ["baseline", "production"]
            )
    # Explicit first shuffle is independent of the production helper.
    assert [entry.case_id for entry in plan.measured[:8:2]] == ["Q04", "Q06", "Q02", "Q03"]
    assert CollectionPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("field", ["warmups", "measured"])
def test_modified_schedule_is_rejected(field: str) -> None:
    payload = build_plan(UUID(TOY_RUN_ID)).model_dump(mode="json")
    payload[field][0], payload[field][1] = payload[field][1], payload[field][0]
    with pytest.raises(ValueError, match="fixed protocol"):
        CollectionPlan.model_validate_json(json.dumps(payload))


def test_complete_collection_is_serial_persisted_synthetic_and_nonreportable(
    completed: CollectionFixture,
) -> None:
    manifest = _verify(completed.directory, completed)
    assert manifest.collection_complete is True
    assert manifest.execution_mode == "synthetic"
    assert manifest.reportable is manifest.evaluation_complete is False
    assert completed.events[0] == "reserve_batch"
    assert completed.ledger.forecasts == [Decimal("6.308")]
    assert completed.events[1:] == ["count", "create"] * 290
    rows = [
        ResultRow.model_validate_json(json.dumps(row))
        for name in ("warmups.jsonl", "results.jsonl")
        for row in _read_rows(completed.directory, name)
    ]
    assert len(rows) == len(manifest.started_attempts) == 166
    assert len({row.attempt_id for row in rows}) == 166
    assert len({trace for row in rows for trace in row.trace_ids}) == 186
    assert all(row.classification == "refusal" for row in rows)
    assert all(row.usage_complete for row in rows)
    assert sum((row.serving_cost_usd or Decimal(0)) for row in rows) == Decimal("0.435")
    assert sum(row.case_id in {"R02", "R04"} for row in rows) == 20
    assert set(manifest.files) == set(ARTIFACT_FILES)
    summary = json.loads((completed.directory / "summary.json").read_text())
    assert summary["execution_mode"] == "synthetic"
    assert summary["reportable"] is summary["evaluation_complete"] is False
    assert completed.manifest == manifest


def test_output_directory_is_never_overwritten(completed: CollectionFixture) -> None:
    before = _manifest_digest(completed.directory)
    with pytest.raises(FileExistsError):
        _collect(completed.directory, completed.dataset, completed.corpus)
    assert _manifest_digest(completed.directory) == before


def test_batch_reservation_rejection_happens_before_any_provider_call(
    tmp_path: Path, dataset: GoldDataset, corpus: Corpus
) -> None:
    events: list[str] = []
    ledger = RecordingBatchLedger(events, reject=True)
    directory = tmp_path / "rejected"
    with pytest.raises(RuntimeError, match="synthetic reservation rejected"):
        _collect(directory, dataset, corpus, events=events, ledger=ledger)
    assert events == ["reserve_batch"]
    assert (directory / "plan.json").is_file()
    assert (directory / "results.jsonl").read_bytes() == b""
    assert (directory / "warmups.jsonl").read_bytes() == b""
    manifest = CollectionManifest.model_validate_json((directory / "manifest.json").read_bytes())
    assert manifest.started_attempts == []
    assert not manifest.collection_complete
    assert "collection_interrupted" in manifest.reasons


def test_interruption_preserves_started_attempts_without_inventing_remaining_rows(
    tmp_path: Path, dataset: GoldDataset, corpus: Corpus
) -> None:
    events: list[str] = []
    started: list[tuple[str, str, bool]] = []

    def factory(case: GoldCase, entry: PlanEntry, warmup: bool) -> ScriptedPort:
        started.append((case.id, entry.arm, warmup))
        responses: list[Message | BaseException] = (
            _responses(case) if len(started) == 1 else [asyncio.CancelledError()]
        )
        return ScriptedPort(responses, events)

    fixture = _collect(tmp_path / "interrupted", dataset, corpus, factory=factory, events=events)
    assert len(started) == 2
    assert len(fixture.manifest.started_attempts) == 2
    assert not fixture.manifest.collection_complete
    assert _read_rows(fixture.directory, "results.jsonl") == []
    rows = _read_rows(fixture.directory, "warmups.jsonl")
    assert len(rows) == 2
    assert [row["classification"] for row in rows] == ["refusal", "error"]
    assert any(reason.endswith(":interrupted") for reason in fixture.manifest.reasons)
    assert "CancelledError" not in (fixture.directory / "raw-provider.jsonl").read_text()
    with pytest.raises(ValueError, match="incomplete"):
        _verify(fixture.directory, fixture)
    assert not _verify(fixture.directory, fixture, require_complete=False).collection_complete


def test_missing_usage_stops_new_attempts_and_retains_reservation(
    tmp_path: Path, dataset: GoldDataset, corpus: Corpus
) -> None:
    events: list[str] = []
    budget = SpendLedger(Decimal("10"))

    def factory(case: GoldCase, entry: PlanEntry, warmup: bool) -> ScriptedPort:
        response = _responses(case)[0]
        assert isinstance(response, Message)
        return ScriptedPort([response.model_copy(update={"usage": None})], events)

    fixture = _collect(
        tmp_path / "missing-usage", dataset, corpus, factory=factory, events=events, budget=budget
    )
    assert budget.stopped
    assert budget.committed_usd == Decimal("0.019")
    assert events == ["reserve_batch", "count", "create"]
    rows = _read_rows(fixture.directory, "warmups.jsonl")
    assert len(rows) == 1
    assert rows[0]["serving_cost_usd"] is None
    assert rows[0]["usage_complete"] is False
    assert "budget_reforecast_required" in fixture.manifest.reasons
    assert not fixture.manifest.collection_complete
    records = [
        TraceRecord.model_validate_json(line)
        for line in (fixture.directory / "metadata.jsonl").read_text().splitlines()
    ]
    total = provider_cost_totals(records)
    assert total.actual_cost_usd is None
    assert total.reserved_cost_usd == Decimal("0.019")
    _verify(fixture.directory, fixture, require_complete=False)


@pytest.mark.parametrize("failure", ["missing_usage", "unknown_accounting"])
def test_judge_unknown_usage_stops_after_first_measured_attempt(
    tmp_path: Path, dataset: GoldDataset, corpus: Corpus, failure: str
) -> None:
    events: list[str] = []
    ledger = RecordingBatchLedger(events)
    budget = SpendLedger(Decimal("10"))
    judge = SyntheticJudge(Clock())
    judge.missing_usage = failure == "missing_usage"
    if failure == "unknown_accounting":
        judge.error = RuntimeError("synthetic judge transport failure")
    output_dir = tmp_path / failure
    manifest = asyncio.run(
        collect_synthetic(
            dataset,
            output_dir,
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            environment=_environment(),
            messages_factory=lambda case, entry, warmup: (
                _harness(corpus, case.id, arm=entry.arm).fake
            ),
            budget=budget,
            batch_ledger=ledger,
            forecast_usd=Decimal("6.308"),
            retrieve=lambda query: (SearchHit(corpus.chunks[0], -1.0),),
            judge=judge,
            judge_config_hash=JUDGE_CONFIG,
            plan=build_plan(UUID(TOY_RUN_ID)),
        )
    )
    fixture = CollectionFixture(output_dir, dataset, corpus, manifest, events, ledger)
    assert len(manifest.started_attempts) == 7
    assert len(_read_rows(output_dir, "warmups.jsonl")) == 6
    measured = _read_rows(output_dir, "results.jsonl")
    assert len(measured) == 1
    assert len(judge.inputs) == 1
    assert measured[0]["judge_cost_usd"] is None
    assert measured[0]["usage_complete"] is True
    assert budget.stopped is False
    assert "budget_reforecast_required" in manifest.reasons
    assert manifest.collection_complete is False
    if failure == "missing_usage":
        accounting = manifest.judge_accounting[measured[0]["attempt_id"]]
        assert accounting.cost.actual_cost_usd is None
        assert accounting.cost.reserved_cost_usd == Decimal("0.034")
        assert accounting.cost.usage_complete is False
    else:
        assert measured[0]["classification"] == "error"
        assert any(reason.endswith(":judge_invalid") for reason in manifest.reasons)
    _verify(output_dir, fixture, require_complete=False)


@pytest.fixture(scope="module")
def answered_completed(
    tmp_path_factory: pytest.TempPathFactory, dataset: GoldDataset, corpus: Corpus
) -> CollectionFixture:
    events: list[str] = []
    ledger = RecordingBatchLedger(events)
    judge = SyntheticJudge(Clock())
    directory = tmp_path_factory.mktemp("answered-collection-parent") / "run"
    manifest = asyncio.run(
        collect_synthetic(
            dataset,
            directory,
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            environment=_environment(),
            messages_factory=lambda case, entry, warmup: (
                _harness(corpus, case.id, arm=entry.arm).fake
            ),
            budget=SpendLedger(Decimal("10")),
            batch_ledger=ledger,
            forecast_usd=Decimal("6.308"),
            retrieve=lambda query: (SearchHit(corpus.chunks[0], -1.0),),
            judge=judge,
            judge_config_hash=JUDGE_CONFIG,
            plan=build_plan(UUID(TOY_RUN_ID)),
        )
    )
    return CollectionFixture(directory, dataset, corpus, manifest, events, ledger, judge)


def test_full_answered_collection_keeps_judge_evidence_and_spending_separate(
    answered_completed: CollectionFixture,
) -> None:
    fixture = answered_completed
    manifest = _verify(fixture.directory, fixture)
    measured = [
        ResultRow.model_validate_json(json.dumps(row))
        for row in _read_rows(fixture.directory, "results.jsonl")
    ]
    warmups = [
        ResultRow.model_validate_json(json.dumps(row))
        for row in _read_rows(fixture.directory, "warmups.jsonl")
    ]
    assert len(measured) == 160
    assert len(warmups) == 6
    assert len(manifest.started_attempts) == 166
    assert fixture.judge is not None
    assert len(fixture.judge.inputs) == len(fixture.judge.contexts) == 160
    assert (
        set(manifest.judge_accounting)
        == set(manifest.judge_evaluations)
        == {str(row.attempt_id) for row in measured}
    )
    assert all(row.classification == "success" and row.judge is not None for row in measured)
    assert all(row.judge is None and row.judge_cost_usd is None for row in warmups)
    assert sum((row.serving_cost_usd or Decimal(0)) for row in measured) == Decimal("0.420")
    assert sum((row.serving_cost_usd or Decimal(0)) for row in warmups) == Decimal("0.015")
    assert sum((row.judge_cost_usd or Decimal(0)) for row in measured) == Decimal("0.112")
    assert all(
        accounting.cost.actual_cost_usd == Decimal("0.0007")
        and accounting.cost.usage_complete
        and len(accounting.records) == 2
        and all(record.phase == "judge" for record in accounting.records)
        for accounting in manifest.judge_accounting.values()
    )
    serving_records = [
        TraceRecord.model_validate_json(line)
        for line in (fixture.directory / "metadata.jsonl").read_text().splitlines()
    ]
    assert all(record.phase != "judge" for record in serving_records)
    assert provider_cost_totals(serving_records).actual_cost_usd == Decimal("0.435")
    assert manifest.collection_complete is True
    assert manifest.execution_mode == "synthetic"
    assert manifest.reportable is manifest.evaluation_complete is False
    summary = json.loads((fixture.directory / "summary.json").read_text())
    assert summary["reportable"] is summary["evaluation_complete"] is False


def test_rehashed_judge_accounting_cost_tampering_is_rejected(
    tmp_path: Path, answered_completed: CollectionFixture
) -> None:
    directory = tmp_path / "tampered-judge"
    shutil.copytree(answered_completed.directory, directory)
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    accounting = next(iter(manifest["judge_accounting"].values()))
    accounting["cost"]["actual_cost_usd"] = "0.009"
    path.write_text(json.dumps(manifest))
    _rehash(directory)
    with pytest.raises(ValueError):
        _verify(directory, answered_completed)


@pytest.mark.parametrize("filename", list(ARTIFACT_FILES) + ["manifest.json"])
def test_missing_artifacts_fail(copied: Path, completed: CollectionFixture, filename: str) -> None:
    (copied / filename).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        _verify(copied, completed)


def test_payload_and_manifest_digests_are_checked(
    copied: Path, completed: CollectionFixture
) -> None:
    original_digest = _manifest_digest(copied)
    (copied / "results.jsonl").write_text((copied / "results.jsonl").read_text() + "\n")
    with pytest.raises(ValueError, match="digest"):
        _verify(copied, completed)
    _rehash(copied)
    with pytest.raises(ValueError, match="manifest digest"):
        verify_collection(
            copied,
            dataset=completed.dataset,
            corpus=completed.corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            manifest_sha256=original_digest,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "drop_result",
        "duplicate_result",
        "wrong_case",
        "stale_source",
        "wrong_trace",
        "drop_metadata",
        "duplicate_metadata",
        "orphan_metadata_attempt",
        "drop_raw",
        "duplicate_raw",
        "wrong_raw_trace",
        "wrong_raw_phase",
        "wrong_raw_run_id",
        "wrong_raw_request_model",
        "wrong_raw_returned_model",
        "wrong_raw_billed_usage",
        "unaccounted_billed_usage",
        "raw_success_replaced_by_failure",
        "malformed_final_raw_text",
        "invalid_raw_count_usage",
        "boolean_raw_index",
        "empty_raw_value",
        "wrong_summary",
        "wrong_retrieval",
        "wrong_cost",
        "wrong_tool_arguments",
        "wrong_tool_result",
        "wrong_extraction",
        "letter_response_uses_extraction_trace",
        "wrong_assertion",
        "missing_assertion",
        "renamed_assertion",
    ],
)
def test_rehashed_semantic_tampering_still_fails(
    copied: Path, completed: CollectionFixture, mutation: str
) -> None:
    filename = "results.jsonl"
    rows = _read_rows(copied, filename)
    if mutation == "drop_result":
        rows.pop()
    elif mutation == "duplicate_result":
        rows[1] = rows[0]
    elif mutation == "wrong_case":
        rows[0]["case_id"] = "Q01" if rows[0]["case_id"] != "Q01" else "Q02"
    elif mutation == "stale_source":
        rows[0]["source_sha"] = "9" * 40
    elif mutation == "wrong_trace":
        rows[0]["trace_ids"] = ["00000000-0000-4000-8000-000000000099"]
    elif mutation in {"drop_metadata", "duplicate_metadata", "orphan_metadata_attempt"}:
        filename = "metadata.jsonl"
        rows = _read_rows(copied, filename)
        if mutation == "drop_metadata":
            rows.pop()
        elif mutation == "duplicate_metadata":
            rows.append(rows[0])
        else:
            orphan = next(row for row in rows if row["record_kind"] == "endpoint").copy()
            orphan["attempt_id"] = "00000000-0000-4000-8000-000000000099"
            rows.append(orphan)
    elif mutation in {
        "drop_raw",
        "duplicate_raw",
        "wrong_raw_trace",
        "wrong_raw_phase",
        "wrong_raw_run_id",
        "wrong_raw_request_model",
        "wrong_raw_returned_model",
        "wrong_raw_billed_usage",
        "unaccounted_billed_usage",
        "raw_success_replaced_by_failure",
        "malformed_final_raw_text",
        "invalid_raw_count_usage",
        "boolean_raw_index",
        "empty_raw_value",
    }:
        filename = "raw-provider.jsonl"
        rows = _read_rows(copied, filename)
        if mutation == "drop_raw":
            rows.pop()
        elif mutation == "duplicate_raw":
            rows.append(rows[0])
        elif mutation == "wrong_raw_trace":
            rows[0]["trace_id"] = "00000000-0000-4000-8000-000000000099"
        elif mutation == "wrong_raw_phase":
            rows[0]["phase"] = "judge"
        elif mutation == "wrong_raw_run_id":
            rows[0]["run_id"] = "00000000-0000-4000-8000-000000000099"
        elif mutation == "wrong_raw_request_model":
            rows[0]["value"]["model"] = "claude-synthetic-unapproved"
        elif mutation == "wrong_raw_returned_model":
            reply = next(
                row
                for row in rows
                if row["event"] == "response" and row["operation"] == "generation"
            )
            reply["value"]["model"] = "claude-synthetic-unapproved"
        elif mutation == "wrong_raw_billed_usage":
            reply = next(
                row
                for row in rows
                if row["event"] == "response" and row["operation"] == "generation"
            )
            reply["value"]["usage"]["input_tokens"] += 1
        elif mutation == "unaccounted_billed_usage":
            reply = next(
                row
                for row in rows
                if row["event"] == "response" and row["operation"] == "generation"
            )
            reply["value"]["usage"]["cache_read_input_tokens"] = 1
        elif mutation in {"raw_success_replaced_by_failure", "malformed_final_raw_text"}:
            reply = next(
                row
                for row in reversed(rows)
                if row["event"] == "response"
                and row["operation"] == "generation"
                and row["phase"] == "analysis"
            )
            if mutation == "raw_success_replaced_by_failure":
                reply["event"] = "failure"
                reply["value"] = {"code": "provider_error"}
            else:
                reply["value"]["content"][0]["text"] = "synthetic malformed final JSON"
        elif mutation == "invalid_raw_count_usage":
            reply = next(
                row
                for row in rows
                if row["event"] == "response" and row["operation"] == "count_tokens"
            )
            reply["value"]["input_tokens"] = -1
        elif mutation == "boolean_raw_index":
            rows[0]["operation_index"] = True
        else:
            rows[0]["value"] = {}
    elif mutation == "wrong_summary":
        summary = json.loads((copied / "summary.json").read_text())
        summary["evaluation_complete"] = True
        (copied / "summary.json").write_text(json.dumps(summary))
        _rehash(copied)
        with pytest.raises(ValueError):
            _verify(copied, completed)
        return
    elif mutation == "wrong_retrieval":
        rows[0]["retrieved_ids"] = [completed.corpus.chunks[0].id]
    elif mutation == "wrong_tool_arguments":
        row = next(row for row in rows if row["case_id"].startswith("R"))
        row["actual_tool_args"]["current_cents"] = 123
    elif mutation == "wrong_tool_result":
        row = next(row for row in rows if row["case_id"].startswith("R"))
        row["actual_tool_result"]["notice_days"] = 37
    elif mutation == "wrong_extraction":
        row = next(row for row in rows if row["case_id"] == "R02")
        row["actual_extract"]["current_cents"] = 123
    elif mutation == "letter_response_uses_extraction_trace":
        row = next(row for row in rows if row["case_id"] == "R02")
        assert len(row["trace_ids"]) == 2
        assert row["response"]["trace_id"] == row["trace_ids"][1]
        row["response"]["trace_id"] = row["trace_ids"][0]
    elif mutation in {"wrong_assertion", "missing_assertion", "renamed_assertion"}:
        row = next(row for row in rows if row["case_id"].startswith("R"))
        if mutation == "wrong_assertion":
            row["deterministic_assertions"][0]["passed"] = False
        elif mutation == "missing_assertion":
            row["deterministic_assertions"].pop()
        else:
            row["deterministic_assertions"][0]["id"] = "invented_assertion"
    else:
        rows[0]["serving_cost_usd"] = "0.123"
    _write_rows(copied, filename, rows)
    _rehash(copied)
    with pytest.raises(ValueError):
        _verify(copied, completed)
