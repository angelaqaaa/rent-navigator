"""Native malformed token evidence through actual partial collection composition."""

import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message, MessageTokensCount
from anthropic.types import Usage as SDKUsage
from eval_fixtures import TOY_HASH, TOY_SOURCE_SHA, write_toy_data
from test_eval_real_collection import SyntheticRealCompositionPort, run

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.collection import RealCollectionManifest, verify_real_collection
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.evidence import verify_raw_links
from rent_navigator.eval.recording import RawProviderRecord
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, RequestedModel
from rent_navigator.provider import SpendLedger
from rent_navigator.trace import TraceRecord


class NativeUsagePort(SyntheticRealCompositionPort):
    def __init__(self, model: RequestedModel, token: str) -> None:
        super().__init__()
        self.model = model
        self.token = token
        self.count_calls = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.count_calls += 1
        return await super().count_tokens(**kwargs)

    async def create(self, **kwargs: Any) -> Message:
        response = await super().create(**kwargs)
        if kwargs["model"] == self.model:
            tokens: dict[str, Any] = {"input_tokens": 1000, "output_tokens": 100, self.token: True}
            response.usage = SDKUsage.model_construct(**tokens)
            assert getattr(response.usage, self.token) is True
        return response


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Corpus, GoldDataset]:
    corpus = load_corpus()
    dataset = load_gold(write_toy_data(tmp_path_factory.mktemp("usage-evidence"), corpus), corpus)
    return corpus, dataset


def verify_partial(
    directory: Path, corpus: Corpus, dataset: GoldDataset, *, complete: bool = False
) -> RealCollectionManifest:
    return verify_real_collection(
        directory,
        dataset=dataset,
        corpus=corpus,
        source_sha=TOY_SOURCE_SHA,
        lock_hash=TOY_HASH,
        manifest_sha256=sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        require_complete=complete,
    )


def rows(directory: Path, filename: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / filename).read_text().splitlines()]


def replace_rows(directory: Path, filename: str, values: list[dict[str, Any]]) -> None:
    path = directory / filename
    path.write_text("".join(json.dumps(value) + "\n" for value in values))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][filename] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
@pytest.mark.parametrize("token", ["input_tokens", "output_tokens"])
def test_native_boolean_usage_is_faithful_and_partial_real_collection_replays(
    tmp_path: Path, inputs: tuple[Corpus, GoldDataset], model: RequestedModel, token: str
) -> None:
    corpus, dataset = inputs
    port = NativeUsagePort(model, token)
    budget = SpendLedger(Decimal("20"))
    directory = tmp_path / "native"
    manifest = run(directory, dataset, corpus, port, budget=budget)
    assert budget.stopped and not manifest.reportable and not manifest.evaluation_complete
    assert len(manifest.started_attempts) == (1 if model == ACTOR_MODEL else 7)
    assert port.judge_calls == (0 if model == ACTOR_MODEL else 1)
    assert port.count_calls == port.actor_calls + port.judge_calls
    raw_name = "raw-provider.jsonl" if model == ACTOR_MODEL else "judge-raw-provider.jsonl"
    metadata_name = "metadata.jsonl" if model == ACTOR_MODEL else "judge-metadata.jsonl"
    raw = rows(directory, raw_name)
    assert raw[-1]["value"]["usage"][token] is True
    call = next(
        row for row in rows(directory, metadata_name) if row["provider_operation"] == "generation"
    )
    assert call["response_code"] == "provider_error"
    assert call[token] is None and call["actual_cost_usd"] is None
    hold = Decimal("0.027" if model == ACTOR_MODEL else "0.058")
    assert call["reserved_cost_usd"] == str(hold)
    assert budget.committed_usd == hold + (
        port.actor_calls * Decimal("0.0015") if model == JUDGE_MODEL else 0
    )
    assert verify_partial(directory, corpus, dataset) == manifest
    if model == ACTOR_MODEL:
        verify_raw_links(
            [RawProviderRecord.model_validate_json(json.dumps(item)) for item in raw],
            [
                TraceRecord.model_validate_json(json.dumps(item))
                for item in rows(directory, metadata_name)
            ],
            manifest.run_id,
        )
    with pytest.raises(ValueError):
        verify_partial(directory, corpus, dataset, complete=True)


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
@pytest.mark.parametrize("token", ["input_tokens", "output_tokens"])
def test_partial_real_collection_rejects_rehashed_failed_usage_normalization(
    tmp_path: Path, inputs: tuple[Corpus, GoldDataset], model: RequestedModel, token: str
) -> None:
    corpus, dataset = inputs
    directory = tmp_path / "tampered"
    manifest = run(directory, dataset, corpus, NativeUsagePort(model, token))
    raw_name = "raw-provider.jsonl" if model == ACTOR_MODEL else "judge-raw-provider.jsonl"
    raw = rows(directory, raw_name)
    raw[-1]["value"]["usage"][token] = 1
    replace_rows(directory, raw_name, raw)
    with pytest.raises(ValueError):
        verify_partial(directory, corpus, dataset)
    if model == ACTOR_MODEL:
        with pytest.raises(ValueError):
            verify_raw_links(
                [RawProviderRecord.model_validate_json(json.dumps(item)) for item in raw],
                [
                    TraceRecord.model_validate_json(line)
                    for line in (directory / "metadata.jsonl").read_text().splitlines()
                ],
                manifest.run_id,
            )


@pytest.mark.parametrize("missing", [False, True])
def test_native_invalid_count_is_preserved_without_generation_and_replays(
    tmp_path: Path, inputs: tuple[Corpus, GoldDataset], missing: bool
) -> None:
    class InvalidCountPort(SyntheticRealCompositionPort):
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            fields: dict[str, Any] = {} if missing else {"input_tokens": True}
            return MessageTokensCount.model_construct(**fields)

    corpus, dataset = inputs
    port = InvalidCountPort()
    directory = tmp_path / "count"
    manifest = run(directory, dataset, corpus, port)
    assert port.actor_calls == port.judge_calls == 0
    raw = rows(directory, "raw-provider.jsonl")
    assert raw[-1]["event"] == "response"
    assert raw[-1]["value"] == ({} if missing else {"input_tokens": True})
    assert verify_partial(directory, corpus, dataset) == manifest


def test_failed_billed_judge_packet_is_replayed_in_partial_real_collection(
    tmp_path: Path, inputs: tuple[Corpus, GoldDataset]
) -> None:
    corpus, dataset = inputs
    directory = tmp_path / "invalid-judgment"
    manifest = run(directory, dataset, corpus, SyntheticRealCompositionPort(fail_judge=True))
    assert not manifest.judge_evaluations and not manifest.reportable
    assert next(iter(manifest.judge_accounting.values())).cost.actual_cost_usd == Decimal("0.003")
    assert verify_partial(directory, corpus, dataset) == manifest
    raw = rows(directory, "judge-raw-provider.jsonl")
    for event in raw:
        if event["event"] == "request":
            event["value"]["system"] = "Replaced judge instructions."
    replace_rows(directory, "judge-raw-provider.jsonl", raw)
    with pytest.raises(ValueError):
        verify_partial(directory, corpus, dataset)
