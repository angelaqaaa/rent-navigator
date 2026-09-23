"""Real composition exercised exclusively with isolated synthetic recording ports."""

import asyncio
import json
import shutil
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message, MessageTokensCount
from eval_fixtures import TOY_HASH, TOY_SOURCE_SHA, write_toy_data
from test_eval_collection import RecordingBatchLedger, _environment

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.collection import (
    REAL_ARTIFACT_FILES,
    RealCollectionManifest,
    collect_real,
    verify_collection,
    verify_real_collection,
)
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.models import ResultRow
from rent_navigator.eval.runner import build_plan
from rent_navigator.index import SearchHit
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL
from rent_navigator.provider import SpendLedger


class SyntheticRealCompositionPort:
    """No network/client/key: reply to the real prepared actor and judge packets."""

    def __init__(self, *, fail_judge: bool = False, fail_actor: bool = False) -> None:
        self.actor_calls = 0
        self.judge_calls = 0
        self.fail_judge = fail_judge
        self.fail_actor = fail_actor

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        model = kwargs["model"]
        stop = "end_turn"
        if model == JUDGE_MODEL:
            self.judge_calls += 1
            packet = json.loads(kwargs["messages"][0]["content"])
            value = {
                "required_claims": [
                    {"id": claim["id"], "result": "met"} for claim in packet["required_claims"]
                ],
                "statements": [
                    {
                        "id": statement["id"],
                        "factual": "supported",
                        "citation_support": "supported"
                        if statement["citation_ids"]
                        else "not_applicable",
                    }
                    for statement in packet["statements"]
                ],
                "false_pass": False,
                "policy_violations": [],
            }
            content = [
                {
                    "type": "text",
                    "text": "invalid synthetic judgment" if self.fail_judge else json.dumps(value),
                }
            ]
        else:
            self.actor_calls += 1
            if self.fail_actor:
                raise RuntimeError("synthetic transport failure")
            schema = kwargs.get("output_config", {}).get("format", {}).get("schema", {})
            if "current_cents" in schema.get("properties", {}):
                content = [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"current_cents": None, "proposed_cents": None, "effective_on": None}
                        ),
                    }
                ]
            else:
                packet = json.loads(kwargs["messages"][0]["content"])
                if packet["mode"] != "question" and len(kwargs["messages"]) == 1:
                    stop = "tool_use"
                    content = [
                        {
                            "type": "tool_use",
                            "id": "toolu_synthetic_real_collection",
                            "name": "rent_increase_check"
                            if packet["mode"] == "rent"
                            else "notice_deadline_check",
                            "input": packet["facts"],
                        }
                    ]
                else:
                    evidence = packet.get("evidence", [])
                    citations = [evidence[0]["id"]] if evidence else []
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
                                            "text": "Synthetic output proposition.",
                                            "citation_ids": citations,
                                        }
                                    ],
                                }
                            ),
                        }
                    ]
        return Message.model_validate(
            {
                "id": "msg_synthetic_real_composition",
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
class Fixture:
    directory: Path
    dataset: GoldDataset
    corpus: Corpus
    port: SyntheticRealCompositionPort
    manifest: RealCollectionManifest


def run(
    directory: Path,
    dataset: GoldDataset,
    corpus: Corpus,
    port: SyntheticRealCompositionPort,
    *,
    budget: SpendLedger | None = None,
) -> RealCollectionManifest:
    return asyncio.run(
        collect_real(
            dataset,
            directory,
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            environment=_environment(),
            messages=port,
            budget=budget if budget is not None else SpendLedger(Decimal("20")),
            batch_ledger=RecordingBatchLedger([]),
            forecast_usd=Decimal("9.502"),
            retrieve=lambda _: (SearchHit(corpus.chunks[0], -1.0),),
            plan=build_plan(),
        )
    )


@pytest.fixture(scope="module")
def complete(tmp_path_factory: pytest.TempPathFactory) -> Fixture:
    corpus = load_corpus()
    base = tmp_path_factory.mktemp("synthetic-real-composition")
    dataset = load_gold(write_toy_data(base / "data", corpus), corpus)
    port = SyntheticRealCompositionPort()
    manifest = run(base / "artifacts", dataset, corpus, port)
    return Fixture(base / "artifacts", dataset, corpus, port, manifest)


def verify(fixture: Fixture, directory: Path | None = None) -> RealCollectionManifest:
    path = directory or fixture.directory
    return verify_real_collection(
        path,
        dataset=fixture.dataset,
        corpus=fixture.corpus,
        source_sha=TOY_SOURCE_SHA,
        lock_hash=TOY_HASH,
        manifest_sha256=sha256((path / "manifest.json").read_bytes()).hexdigest(),
    )


def test_real_callable_reuses_166_plan_and_separate_real_judge_composition(
    complete: Fixture,
) -> None:
    manifest = verify(complete)
    assert manifest.collection_complete and manifest.evaluation_complete
    assert manifest.execution_mode == "live"
    assert (
        not manifest.reportable
    )  # Isolated synthetic environment cannot substantiate a measurement.
    assert len(manifest.started_attempts) == 166
    assert len(manifest.judge_evaluations) == complete.port.judge_calls == 160
    assert set(manifest.files) == set(REAL_ARTIFACT_FILES)
    assert len((complete.directory / "warmups.jsonl").read_text().splitlines()) == 6
    assert len((complete.directory / "results.jsonl").read_text().splitlines()) == 160
    serving = [
        json.loads(line)
        for line in (complete.directory / "raw-provider.jsonl").read_text().splitlines()
    ]
    judges = [
        json.loads(line)
        for line in (complete.directory / "judge-raw-provider.jsonl").read_text().splitlines()
    ]
    assert all(item["phase"] != "judge" for item in serving)
    assert all(item["phase"] == "judge" for item in judges)
    assert all(
        item["value"]["model"] == ACTOR_MODEL for item in serving if item["event"] == "request"
    )
    assert all(
        item["value"]["model"] == JUDGE_MODEL for item in judges if item["event"] == "request"
    )
    rows = [
        ResultRow.model_validate_json(line)
        for line in (complete.directory / "results.jsonl").read_text().splitlines()
    ]
    assert all(row.judge_cost_usd == Decimal("0.003") for row in rows)
    assert all(
        row.serving_cost_usd is not None and row.serving_cost_usd < Decimal("0.005") for row in rows
    )
    with pytest.raises(ValueError):
        verify_collection(
            complete.directory,
            dataset=complete.dataset,
            corpus=complete.corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            manifest_sha256=sha256((complete.directory / "manifest.json").read_bytes()).hexdigest(),
        )


@pytest.mark.parametrize("filename", ["judge-raw-provider.jsonl", "judge-metadata.jsonl"])
def test_real_completeness_rejects_missing_judge_evidence_even_with_refreshed_hashes(
    complete: Fixture, tmp_path: Path, filename: str
) -> None:
    target = tmp_path / "tampered"
    shutil.copytree(complete.directory, target)
    path = target / filename
    path.write_text("\n".join(path.read_text().splitlines()[1:]) + "\n")
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][filename] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify(complete, target)


@pytest.mark.parametrize("failure", ["judge", "actor"])
def test_invalid_billed_judge_or_infrastructure_failure_stops_before_next_paid_attempt(
    complete: Fixture, tmp_path: Path, failure: str
) -> None:
    port = SyntheticRealCompositionPort(
        fail_judge=failure == "judge", fail_actor=failure == "actor"
    )
    directory = tmp_path / "partial"
    manifest = run(directory, complete.dataset, complete.corpus, port)
    assert (
        not manifest.evaluation_complete
        and not manifest.collection_complete
        and not manifest.reportable
    )
    assert "collection_untrustworthy" in manifest.reasons
    assert len(manifest.started_attempts) == (7 if failure == "judge" else 1)
    assert port.judge_calls == (1 if failure == "judge" else 0)
    if failure == "judge":
        assert next(iter(manifest.judge_accounting.values())).cost.actual_cost_usd == Decimal(
            "0.003"
        )
        assert not manifest.judge_evaluations
    with pytest.raises(ValueError):
        verify(complete, directory)


class ModelDriftPort(SyntheticRealCompositionPort):
    """Stop the old implementation after its first forbidden extra generation."""

    def __init__(self, *, drift_model: str = ACTOR_MODEL) -> None:
        super().__init__()
        self.drift_model = drift_model
        self.drift_seen = False
        self.count_calls = 0
        self.extra_operations = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.count_calls += 1
        if self.drift_seen:
            self.extra_operations += 1
        return await super().count_tokens(**kwargs)

    async def create(self, **kwargs: Any) -> Message:
        if self.drift_seen:
            self.extra_operations += 1
            self.actor_calls += kwargs["model"] == ACTOR_MODEL
            self.judge_calls += kwargs["model"] == JUDGE_MODEL
            raise RuntimeError("Synthetic probe stopped an unauthorized later generation")
        response = await super().create(**kwargs)
        if kwargs["model"] == self.drift_model:
            self.drift_seen = True
            return response.model_copy(
                update={"model": JUDGE_MODEL if self.drift_model == ACTOR_MODEL else ACTOR_MODEL}
            )
        return response


def test_first_actor_model_drift_stops_before_any_later_provider_operation(tmp_path: Path) -> None:
    corpus = load_corpus()
    dataset = load_gold(write_toy_data(tmp_path / "data", corpus), corpus)
    port = ModelDriftPort()
    directory = tmp_path / "drift"
    budget = SpendLedger(Decimal("20"))
    manifest = run(directory, dataset, corpus, port, budget=budget)
    assert port.actor_calls == 1
    assert port.judge_calls == 0
    assert port.count_calls == 1 and port.extra_operations == 0
    assert not manifest.collection_complete and not manifest.evaluation_complete
    assert "provider_model_mismatch" in manifest.reasons
    assert "budget_reforecast_required" in manifest.reasons
    assert budget.stopped and budget.reforecast_required
    metadata = [
        json.loads(line) for line in (directory / "metadata.jsonl").read_text().splitlines()
    ]
    generation = next(item for item in metadata if item["provider_operation"] == "generation")
    assert generation["requested_model_id"] == ACTOR_MODEL
    assert generation["returned_model_id"] == JUDGE_MODEL
    assert generation["input_tokens"] == 1000 and generation["output_tokens"] == 100
    raw = [json.loads(line) for line in (directory / "raw-provider.jsonl").read_text().splitlines()]
    assert raw[-1]["value"]["model"] == JUDGE_MODEL
    assert raw[-1]["value"]["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    with pytest.raises(ValueError):
        verify_real_collection(
            directory,
            dataset=dataset,
            corpus=corpus,
            source_sha=TOY_SOURCE_SHA,
            lock_hash=TOY_HASH,
            manifest_sha256=sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        )


def test_first_judge_model_drift_stops_shared_collection_and_preserves_billed_response(
    tmp_path: Path,
) -> None:
    corpus = load_corpus()
    dataset = load_gold(write_toy_data(tmp_path / "data", corpus), corpus)
    port = ModelDriftPort(drift_model=JUDGE_MODEL)
    directory = tmp_path / "judge-drift"
    budget = SpendLedger(Decimal("20"))
    manifest = run(directory, dataset, corpus, port, budget=budget)
    assert port.judge_calls == 1 and port.extra_operations == 0
    assert len(manifest.started_attempts) == 7
    assert not manifest.collection_complete and not manifest.evaluation_complete
    assert {"provider_model_mismatch", "budget_reforecast_required"} <= set(manifest.reasons)
    assert budget.stopped and budget.reforecast_required
    accounting = next(iter(manifest.judge_accounting.values()))
    assert accounting.returned_model_id == ACTOR_MODEL
    assert accounting.cost.input_tokens == 1000 and accounting.cost.output_tokens == 100
    assert not manifest.judge_evaluations
    raw = [
        json.loads(line)
        for line in (directory / "judge-raw-provider.jsonl").read_text().splitlines()
    ]
    assert raw[-1]["value"]["model"] == ACTOR_MODEL
    assert raw[-1]["value"]["usage"] == {"input_tokens": 1000, "output_tokens": 100}


@pytest.mark.parametrize("stream", ["actor", "judge"])
def test_complete_legacy_artifact_with_self_consistent_model_drift_is_rejected(
    complete: Fixture,
    tmp_path: Path,
    stream: str,
) -> None:
    directory = tmp_path / "legacy-model-drift"
    shutil.copytree(complete.directory, directory)
    raw_name = "raw-provider.jsonl" if stream == "actor" else "judge-raw-provider.jsonl"
    metadata_name = "metadata.jsonl" if stream == "actor" else "judge-metadata.jsonl"
    raw = [json.loads(line) for line in (directory / raw_name).read_text().splitlines()]
    metadata = [json.loads(line) for line in (directory / metadata_name).read_text().splitlines()]
    response = next(
        item for item in raw if item["operation"] == "generation" and item["event"] == "response"
    )
    drift = JUDGE_MODEL if stream == "actor" else ACTOR_MODEL
    response["value"]["model"] = drift
    matching = next(
        item
        for item in metadata
        if item["trace_id"] == response["trace_id"]
        and item["provider_call_index"] == response["operation_index"]
    )
    matching["returned_model_id"] = drift
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if stream == "judge":
        attempt = response["attempt_id"]
        for field in ("judge_accounting", "judge_evaluations"):
            manifest[field][attempt]["returned_model_id"] = drift
            entry = next(
                item
                for item in manifest[field][attempt]["records"]
                if item["provider_call_index"] == response["operation_index"]
            )
            entry["returned_model_id"] = drift
    for name, records in ((raw_name, raw), (metadata_name, metadata)):
        path = directory / name
        path.write_text("".join(json.dumps(item) + "\n" for item in records))
        manifest["files"][name] = sha256(path.read_bytes()).hexdigest()
    assert manifest["evaluation_complete"] and manifest["collection_complete"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="model mismatch requires reforecast"):
        verify(complete, directory)
