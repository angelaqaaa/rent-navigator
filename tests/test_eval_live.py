"""Isolated scripted gate artifacts; none are real project measurements."""

import asyncio
import json
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from test_eval_security import synthetic_security_xml

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.live import (
    LiveGatePlan,
    LiveManifest,
    LiveSummary,
    build_live_plan,
    run_live_gate,
    verify_live_gate,
)
from rent_navigator.eval.live_budget import LiveBudget, receipt_from_events
from rent_navigator.eval.models import CASE_IDS
from rent_navigator.eval.offline import file_hash, run_offline
from rent_navigator.eval.permit import LivePermit
from rent_navigator.extract import EXTRACTION_SYSTEM
from rent_navigator.index import build_index, search
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import ACTOR_MODEL, JUDGE_MODEL, Usage, cost_for_usage

SOURCE = "3" * 40


def permit() -> LivePermit:
    return LivePermit(
        schema_version=1,
        repository="angelaqaaa/rent-navigator",
        workflow=".github/workflows/ci.yml",
        source_sha=SOURCE,
        run_id="1234",
        run_attempt=1,
        batch_uuid=uuid4(),
        purpose="bootstrap",
        baseline_sha256=None,
        future_live_batches_remaining=3,
        funded_slot="synthetic-fixture",
        max_actor_calls=77,
        max_judge_calls=39,
        reserved_usd=Decimal("2.399"),
        incurred_usd=Decimal("0.045685"),
        held_usd=Decimal(0),
        still_required_usd=Decimal("17.833"),
        development_cap_usd=Decimal(21),
        provider_funding_usd=Decimal(30),
        demo_reserved_usd=Decimal(9),
        development_slots_remaining=14,
        prior_ledger_sha256="4" * 64,
        issued_at_utc=datetime.now(UTC),
        expires_at_utc=datetime.now(UTC) + timedelta(hours=1),
    )


class ScriptedGatePort:
    def __init__(
        self, dataset: GoldDataset, *, malformed_judge: bool = False, fail_first: bool = False
    ) -> None:
        self.dataset = dataset
        self.creates = 0
        self.judges = 0
        self.malformed_judge = malformed_judge
        self.fail_first = fail_first

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        self.creates += 1
        if self.fail_first:
            raise RuntimeError("private-synthetic-exception")
        model = kwargs["model"]
        stop = "end_turn"
        if model == JUDGE_MODEL:
            self.judges += 1
            packet = json.loads(kwargs["messages"][0]["content"])
            result = {
                "required_claims": [
                    {"id": c["id"], "result": "met"} for c in packet["required_claims"]
                ],
                "statements": [
                    {"id": s["id"], "factual": "supported", "citation_support": "supported"}
                    for s in packet["statements"]
                ],
                "false_pass": False,
                "policy_violations": [],
            }
            content = [
                {"type": "text", "text": "{" if self.malformed_judge else json.dumps(result)}
            ]
        elif kwargs["system"] == EXTRACTION_SYSTEM:
            case = next(
                c for c in self.dataset.cases if c.letter == kwargs["messages"][0]["content"]
            )
            assert case.expected_extract is not None
            content = [{"type": "text", "text": case.expected_extract.model_dump_json()}]
        else:
            packet = json.loads(kwargs["messages"][0]["content"])
            if kwargs.get("tool_choice", {}).get("type") == "any":
                content = [
                    {
                        "type": "tool_use",
                        "id": "toolu_scripted_gate",
                        "name": packet["mode"] + "_increase_check"
                        if packet["mode"] == "rent"
                        else "notice_deadline_check",
                        "input": packet["facts"],
                    }
                ]
                stop = "tool_use"
            else:
                content = [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "kind": "answer",
                                "refusal_reason": None,
                                "statements": [
                                    {
                                        "id": "s1",
                                        "text": "Synthetic isolated proposition.",
                                        "citation_ids": [packet["evidence"][0]["id"]],
                                    }
                                ],
                            }
                        ),
                    }
                ]
        return Message.model_validate(
            {
                "id": "msg_scripted_gate",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": stop,
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )


@dataclass(frozen=True)
class GateFixture:
    data_dir: Path
    directory: Path
    corpus: Corpus
    dataset: GoldDataset
    lock: Path
    offline: Path
    index: Path
    manifest: LiveManifest


def make_fixture(
    root: Path, *, malformed_judge: bool = False, fail_first: bool = False
) -> GateFixture:
    corpus = load_corpus()
    data_dir = root / "data"
    shutil.copytree(Path("eval"), data_dir)
    dataset = load_gold(data_dir, corpus)
    lock = root / "lock"
    lock.write_text("isolated synthetic lock fixture\n")
    offline = root / "offline"
    run_offline(
        data_dir,
        offline,
        corpus=corpus,
        source_sha=SOURCE,
        security_report=synthetic_security_xml(root / "security.xml"),
        lock_path=lock,
    )
    index = root / "index.sqlite"
    build_index(index, corpus=corpus)
    directory = root / "gate"
    manifest = asyncio.run(
        run_live_gate(
            data_dir,
            directory,
            dataset=dataset,
            corpus=corpus,
            source_sha=SOURCE,
            lock_path=lock,
            offline_dir=offline,
            permit=permit(),
            messages=ScriptedGatePort(
                dataset, malformed_judge=malformed_judge, fail_first=fail_first
            ),
            retrieve=lambda q: search(index, q),
        )
    )
    return GateFixture(data_dir, directory, corpus, dataset, lock, offline, index, manifest)


@pytest.fixture(scope="module")
def complete(tmp_path_factory: pytest.TempPathFactory) -> GateFixture:
    return make_fixture(tmp_path_factory.mktemp("synthetic-live-gate"))


def verify(
    fixture: GateFixture, directory: Path | None = None, *, require_pass: bool = True
) -> LiveManifest:
    directory = directory or fixture.directory
    return verify_live_gate(
        directory,
        data_dir=fixture.data_dir,
        dataset=fixture.dataset,
        corpus=fixture.corpus,
        source_sha=SOURCE,
        lock_path=fixture.lock,
        manifest_sha256=file_hash(directory / "manifest.json"),
        require_pass=require_pass,
    )


def rehash(directory: Path) -> None:
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["files"] = {name: file_hash(directory / name) for name in manifest["files"]}
    (directory / "manifest.json").write_text(json.dumps(manifest))


def test_complete_scripted_plan_raw_judge_receipt_verified(complete: GateFixture) -> None:
    manifest = verify(complete)
    assert manifest.passed and manifest.evaluation_complete and not manifest.reportable
    summary = LiveSummary.model_validate_json((complete.directory / "summary.json").read_bytes())
    assert (summary.gold_count, summary.security_count, summary.successes) == (32, 7, 32)
    assert len(manifest.judge_evaluations) == 39
    assert len(manifest.started_attempts) == 39
    assert len({item.attempt_id for item in manifest.started_attempts}) == 39
    receipt = json.loads((complete.directory / "receipt.json").read_text())
    assert Decimal(receipt["actual_usd"]) == Decimal("0.2145")
    assert receipt["actor_calls"] == 65 and receipt["judge_calls"] == 39
    assert receipt["unresolved_hold_usd"] == "0"


@pytest.mark.parametrize("change", ["duplicate", "missing", "order", "warmup", "arm"])
def test_plan_is_exact32_plus7(change: str) -> None:
    payload = build_live_plan().model_dump(mode="json")
    assert [e["case_id"] for e in payload["gold"][:16]] == list(CASE_IDS)
    if change == "duplicate":
        payload["gold"][1] = payload["gold"][0]
    if change == "missing":
        payload["security"].pop()
    if change == "order":
        payload["gold"].reverse()
    if change == "warmup":
        payload["gold"].append(payload["gold"][0])
    if change == "arm":
        payload["gold"][0]["arm"] = "baseline"
    with pytest.raises(ValueError):
        LiveGatePlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "filename",
    [
        "raw-provider.jsonl",
        "judge-raw.jsonl",
        "judge-metadata.jsonl",
        "budget.jsonl",
        "offline/security.xml",
    ],
)
def test_missing_required_evidence_cannot_pass(
    complete: GateFixture, tmp_path: Path, filename: str
) -> None:
    directory = tmp_path / "copy"
    shutil.copytree(complete.directory, directory)
    (directory / filename).unlink()
    with pytest.raises((ValueError, OSError)):
        verify(complete, directory)


@pytest.mark.parametrize(
    "change",
    [
        "judge_text",
        "judge_usage",
        "actual_tool",
        "classification",
        "summary",
        "reservation",
        "source",
        "offline_result",
    ],
)
def test_rehashed_tampering_still_fails(complete: GateFixture, tmp_path: Path, change: str) -> None:
    directory = tmp_path / "copy"
    shutil.copytree(complete.directory, directory)
    filename = "results.jsonl"
    if change.startswith("judge_"):
        filename = "judge-raw.jsonl"
    if change == "reservation":
        filename = "budget.jsonl"
    if change == "offline_result":
        path = directory / "offline/report.json"
        data = json.loads(path.read_text())
        data["case_results"][0]["actual_tool_result"]["notice_days"] = 89
        path.write_text(json.dumps(data))
    elif change == "summary":
        path = directory / "summary.json"
        data = json.loads(path.read_text())
        data["successes"] = 31
        path.write_text(json.dumps(data))
    else:
        path = directory / filename
        data = [json.loads(line) for line in path.read_text().splitlines()]
        if change.startswith("judge_"):
            entry = next(
                d for d in data if d["event"] == "response" and d["operation"] == "generation"
            )
            if change == "judge_text":
                entry["value"]["content"][0]["text"] = "{}"
            else:
                entry["value"]["usage"]["input_tokens"] = 999
        elif change == "reservation":
            data[1]["reserved_usd"] = "0"
        elif change == "actual_tool":
            data[0]["actual_tool_result"]["notice_days"] = 89
        elif change == "classification":
            data[0]["classification"] = "incorrect"
        elif change == "source":
            data[0]["source_sha"] = "5" * 40
        path.write_text("".join(json.dumps(d) + "\n" for d in data))
    rehash(directory)
    with pytest.raises(ValueError):
        verify(complete, directory)


def test_billed_invalid_judge_retains_first_attempt_stops(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, malformed_judge=True)
    assert not fixture.manifest.passed and not fixture.manifest.evaluation_complete
    assert len(fixture.manifest.started_attempts) == 1
    assert len(fixture.manifest.judge_accounting) == 1
    assert not fixture.manifest.judge_evaluations
    receipt = json.loads((fixture.directory / "receipt.json").read_text())
    assert Decimal(receipt["actual_usd"]) == Decimal("0.006")
    with pytest.raises(ValueError):
        verify(fixture)


def test_unknown_actor_usage_is_held_and_stops(tmp_path: Path) -> None:
    fixture = make_fixture(tmp_path, fail_first=True)
    assert len(fixture.manifest.started_attempts) == 1 and not fixture.manifest.passed
    receipt = json.loads((fixture.directory / "receipt.json").read_text())
    assert Decimal(receipt["unresolved_hold_usd"]) == Decimal("0.019")
    assert not receipt["complete"] and receipt["actor_calls"] == 1
    assert "private-synthetic-exception" not in "".join(
        p.read_text() for p in fixture.directory.rglob("*") if p.is_file()
    )


def test_budget_persists_before_generation_unknown_and_exact_once() -> None:
    stream = StringIO()
    grant = permit()
    budget = LiveBudget(grant, stream, config_hash="5" * 64)
    budget.bind(uuid4())
    ticket = budget.reserve(ACTOR_MODEL)
    assert json.loads(stream.getvalue().splitlines()[-1])["event"] == "generation_start"
    assert budget.receipt().unresolved_hold_usd == Decimal("0.019")
    budget.reconcile(ticket, cost_for_usage(ACTOR_MODEL, None))
    assert budget.stopped
    with pytest.raises(ProviderFailure):
        budget.reserve(JUDGE_MODEL)
    with pytest.raises(ProviderFailure):
        budget.reconcile(ticket, cost_for_usage(ACTOR_MODEL, None))
    assert receipt_from_events(grant, "5" * 64, budget.events) == budget.receipt()


def test_known_usage_refunds_reservation_and_preserves_future_reserves() -> None:
    grant = permit()
    budget = LiveBudget(grant, StringIO(), config_hash="5" * 64)
    budget.bind(uuid4())
    ticket = budget.reserve(JUDGE_MODEL)
    budget.reconcile(
        ticket, cost_for_usage(JUDGE_MODEL, Usage(input_tokens=1000, output_tokens=100))
    )
    receipt = budget.receipt()
    assert receipt.actual_usd == Decimal("0.003") and receipt.unresolved_hold_usd == 0
    assert receipt.permit.still_required_usd == Decimal("17.833")


@pytest.mark.parametrize(
    "baseline_B,incorrect_count,expected",
    [(28, 4, True), (28, 5, False), (32, 2, True), (32, 3, False)],
)
def test_fixed32_denominator_and_original_baseline(
    complete: GateFixture, baseline_B: int, incorrect_count: int, expected: bool
) -> None:
    from rent_navigator.eval.live import summarize_gate
    from rent_navigator.eval.models import ResultRow
    from rent_navigator.eval.offline import Baseline
    from rent_navigator.eval.recording import RawProviderRecord
    from rent_navigator.eval.safety_execution import SecurityResultRow

    rows = [
        ResultRow.model_validate_json(line)
        for line in (complete.directory / "results.jsonl").read_text().splitlines()
    ]
    safety = [
        SecurityResultRow.model_validate_json(line)
        for line in (complete.directory / "security-results.jsonl").read_text().splitlines()
    ]
    raw = [
        RawProviderRecord.model_validate_json(line)
        for line in (complete.directory / "raw-provider.jsonl").read_text().splitlines()
    ]
    for index in range(incorrect_count):
        payload = rows[index].model_dump(mode="json")
        payload["judge"]["required_claims"][0]["result"] = "missing"
        payload["classification"] = "incorrect"
        rows[index] = ResultRow.model_validate_json(json.dumps(payload))
    frozen = Baseline(
        schema_version=1,
        source_sha=SOURCE,
        corpus_hash=complete.corpus.corpus_hash,
        gold_hash=complete.dataset.gold_hash,
        config_hash="6" * 64,
        mrr_at_5=0.0,
        ndcg_at_5=0.0,
        B=baseline_B,
        bootstrap_run_id=uuid4(),
        bootstrap_manifest_sha256="7" * 64,
    )
    result = summarize_gate(
        rows,
        safety,
        dataset=complete.dataset,
        corpus=complete.corpus,
        raw=raw,
        baseline=frozen,
        accounting_complete=True,
    )
    assert result.gold_count == 32 and result.successes == 32 - incorrect_count
    assert result.required_successes == max(28, baseline_B - 2) and result.passed == expected


@pytest.mark.parametrize("critical", ["citation", "false_pass", "policy"])
def test_critical_judgment_blocks_even_with_at_least31_successes(
    complete: GateFixture, critical: str
) -> None:
    from rent_navigator.eval.live import summarize_gate
    from rent_navigator.eval.models import ResultRow
    from rent_navigator.eval.recording import RawProviderRecord
    from rent_navigator.eval.safety_execution import SecurityResultRow

    rows = [
        ResultRow.model_validate_json(line)
        for line in (complete.directory / "results.jsonl").read_text().splitlines()
    ]
    safety = [
        SecurityResultRow.model_validate_json(line)
        for line in (complete.directory / "security-results.jsonl").read_text().splitlines()
    ]
    raw = [
        RawProviderRecord.model_validate_json(line)
        for line in (complete.directory / "raw-provider.jsonl").read_text().splitlines()
    ]
    payload = rows[0].model_dump(mode="json")
    if critical == "citation":
        payload["judge"]["statements"][0]["citation_support"] = "unsupported"
    elif critical == "false_pass":
        payload["judge"]["false_pass"] = True
        payload["classification"] = "incorrect"
    else:
        payload["judge"]["policy_violations"] = ["S08"]
        payload["classification"] = "incorrect"
    rows[0] = ResultRow.model_validate_json(json.dumps(payload))
    result = summarize_gate(
        rows,
        safety,
        dataset=complete.dataset,
        corpus=complete.corpus,
        raw=raw,
        baseline=None,
        accounting_complete=True,
    )
    assert (
        result.successes >= 31 and result.complete and not result.passed and result.critical_flags
    )


@pytest.mark.parametrize("token_value", ["1000", 1000.0, True])
def test_raw_usage_does_not_coerce_json_types(
    complete: GateFixture, tmp_path: Path, token_value: object
) -> None:
    directory = tmp_path / "copy"
    shutil.copytree(complete.directory, directory)
    path = directory / "raw-provider.jsonl"
    data = [json.loads(line) for line in path.read_text().splitlines()]
    response = next(d for d in data if d["event"] == "response" and d["operation"] == "generation")
    response["value"]["usage"]["input_tokens"] = token_value
    path.write_text("".join(json.dumps(d) + "\n" for d in data))
    rehash(directory)
    with pytest.raises(ValueError, match="strict integers"):
        verify(complete, directory)


def test_gate_cannot_overwrite_original_evidence(complete: GateFixture) -> None:
    with pytest.raises(FileExistsError):
        asyncio.run(
            run_live_gate(
                complete.data_dir,
                complete.directory,
                dataset=complete.dataset,
                corpus=complete.corpus,
                source_sha=SOURCE,
                lock_path=complete.lock,
                offline_dir=complete.offline,
                permit=permit(),
                messages=ScriptedGatePort(complete.dataset),
                retrieve=lambda q: search(complete.index, q),
            )
        )


@pytest.mark.parametrize("change", [None, "B", "config_hash", "mrr_at_5", "bootstrap_run_id"])
def test_attachment_must_preserve_actual_bootstrap_baseline(
    complete: GateFixture, tmp_path: Path, change: str | None
) -> None:
    from rent_navigator.eval.offline import Baseline, OfflineReport

    data_dir = tmp_path / "active-data"
    shutil.copytree(complete.data_dir, data_dir)
    report = OfflineReport.model_validate_json(
        (complete.directory / "offline/report.json").read_bytes()
    )
    frozen = Baseline(
        schema_version=1,
        source_sha=SOURCE,
        corpus_hash=complete.corpus.corpus_hash,
        gold_hash=complete.dataset.gold_hash,
        config_hash=complete.manifest.config_hash,
        mrr_at_5=report.production_retrieval.mrr_at_5,
        ndcg_at_5=report.production_retrieval.ndcg_at_5,
        B=32,
        bootstrap_run_id=complete.manifest.batch_uuid,
        bootstrap_manifest_sha256=file_hash(complete.directory / "manifest.json"),
    ).model_dump(mode="json")
    if change is not None:
        frozen[change] = {
            "B": 28,
            "config_hash": "9" * 64,
            "mrr_at_5": 0.0,
            "bootstrap_run_id": str(uuid4()),
        }[change]
    (data_dir / "baseline.json").write_text(json.dumps(frozen))
    (data_dir / "activation.json").write_text(
        json.dumps({"schema_version": 1, "baseline_phase": "active"})
    )
    active = replace(complete, data_dir=data_dir, dataset=load_gold(data_dir, complete.corpus))
    if change is None:
        assert verify(active).passed
    else:
        with pytest.raises(ValueError, match="actual passing bootstrap"):
            verify(active)


@pytest.mark.parametrize(
    "field", ["expected_run_id", "expected_run_attempt", "expected_batch_uuid"]
)
def test_artifact_must_match_external_producer(complete: GateFixture, field: str) -> None:
    options: dict[str, Any] = {
        "expected_run_id": complete.manifest.workflow_run_id,
        "expected_run_attempt": 1,
        "expected_batch_uuid": complete.manifest.batch_uuid,
    }
    options[field] = {
        "expected_run_id": "99999",
        "expected_run_attempt": 2,
        "expected_batch_uuid": uuid4(),
    }[field]
    with pytest.raises(ValueError, match="artifact producer"):
        verify_live_gate(
            complete.directory,
            data_dir=complete.data_dir,
            dataset=complete.dataset,
            corpus=complete.corpus,
            source_sha=SOURCE,
            lock_path=complete.lock,
            manifest_sha256=file_hash(complete.directory / "manifest.json"),
            **options,
        )


def test_refused_attempt_cannot_hide_unaccounted_judge_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ScriptedGatePort.create

    async def refuse_first_final(self: ScriptedGatePort, **kwargs: Any) -> Message:
        response = await original(self, **kwargs)
        if self.creates == 2:
            payload = response.model_dump(mode="json")
            payload["content"] = [
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
            return Message.model_validate(payload)
        return response

    monkeypatch.setattr(ScriptedGatePort, "create", refuse_first_final)
    fixture = make_fixture(tmp_path)
    assert verify(fixture).passed
    rows = (fixture.directory / "results.jsonl").read_text().splitlines()
    refused = json.loads(rows[0])
    assert refused["classification"] == "refusal" and refused["judge"] is None
    path = fixture.directory / "judge-raw.jsonl"
    rogue = json.loads(path.read_text().splitlines()[0])
    rogue["attempt_id"] = refused["attempt_id"]
    with path.open("a") as stream:
        stream.write(json.dumps(rogue) + "\n")
    rehash(fixture.directory)
    with pytest.raises(ValueError, match="Unlinked provider or judge evidence"):
        verify(fixture)
