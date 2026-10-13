"""Failed native counts cross the real deadline before token-type validation."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from anthropic.types import MessageTokensCount
from eval_fixtures import write_toy_data
from test_eval_real_collection import SyntheticRealCompositionPort, run
from test_eval_runner import _harness
from test_eval_usage_evidence import replace_rows, rows, verify_partial

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval import collection
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.evidence import verify_raw_links
from rent_navigator.eval.recording import RawProviderRecord
from rent_navigator.eval.runner import AttemptOutcome, run_attempt
from rent_navigator.models import ErrorResponse
from rent_navigator.provider import SpendLedger
from rent_navigator.trace import ACTOR_MODEL, TraceRecord, provider_cost_totals


@dataclass
class Clock:
    now: float = 10.0

    def __call__(self) -> float:
        return self.now


def count_response(shape: str) -> MessageTokensCount:
    shapes: dict[str, dict[str, Any]] = {
        "boolean": {"input_tokens": True},
        "integer": {"input_tokens": 1000},
        "string": {"input_tokens": "1000"},
        "float": {"input_tokens": 1000.0},
        "null": {"input_tokens": None},
        "missing": {},
        "negative": {"input_tokens": -1},
    }
    return MessageTokensCount.model_construct(**shapes[shape])


CASES = [
    (shape, True)
    for shape in ("boolean", "integer", "string", "float", "null", "missing", "negative")
] + [("boolean", False)]


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Corpus, GoldDataset]:
    corpus = load_corpus()
    dataset = load_gold(write_toy_data(tmp_path_factory.mktemp("count-timeout"), corpus), corpus)
    return corpus, dataset


def assert_zero_count_accounting(records: list[TraceRecord], code: str) -> None:
    details = [record for record in records if record.record_kind == "provider_call"]
    assert len(details) == 1 and details[0].provider_operation == "count_tokens"
    assert details[0].response_code == code
    assert all(record.response_code == code for record in records)
    total = provider_cost_totals(records)
    assert total.usage_complete
    assert total.actual_cost_usd == total.reserved_cost_usd == Decimal(0)
    assert total.input_tokens == total.output_tokens == 0


def assert_raw_count(raw: list[RawProviderRecord], shape: str) -> None:
    assert len(raw) == 2 and [record.event for record in raw] == ["request", "response"]
    expected = count_response(shape).model_dump(mode="python", exclude_unset=True, warnings=False)
    assert raw[-1].value == expected
    if shape != "missing":
        assert type(raw[-1].value["input_tokens"]) is type(expected["input_tokens"])


@pytest.mark.parametrize("shape,expired", CASES)
def test_serving_count_timeout_preserves_native_failure_and_free_accounting(
    inputs: tuple[Corpus, GoldDataset], monkeypatch: pytest.MonkeyPatch, shape: str, expired: bool
) -> None:
    corpus, _ = inputs
    test = _harness(corpus)
    test.clock.now = 10.0

    async def count(**kwargs: Any) -> MessageTokensCount:
        test.fake.counts.append(kwargs)
        test.clock.now = 56.0 if expired else 10.0
        return count_response(shape)

    monkeypatch.setattr(test.fake, "count_tokens", count)
    outcome = test.run()
    code = "deadline_exceeded" if expired else "provider_error"
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == code
    assert outcome.row.classification == "error" and outcome.row.judge is None
    assert len(test.fake.counts) == 1 and test.fake.creates == []
    assert test.budget.committed_usd == Decimal(0)
    raw = [RawProviderRecord.model_validate_json(line) for line in test.raw.getvalue().splitlines()]
    assert_raw_count(raw, shape)
    assert_zero_count_accounting(list(outcome.records), code)
    verify_raw_links(raw, list(outcome.records), outcome.row.run_id)


def test_valid_unexpired_native_count_enters_generation(
    inputs: tuple[Corpus, GoldDataset], monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus, _ = inputs
    test = _harness(corpus)

    async def count(**kwargs: Any) -> MessageTokensCount:
        test.fake.counts.append(kwargs)
        return count_response("integer")

    monkeypatch.setattr(test.fake, "count_tokens", count)
    outcome = test.run()
    assert outcome.row.classification == "success"
    assert len(test.fake.counts) == len(test.fake.creates) == 1
    assert test.budget.committed_usd == Decimal("0.0015")
    details = [record for record in outcome.records if record.record_kind == "provider_call"]
    assert [record.provider_operation for record in details] == ["count_tokens", "generation"]
    assert details[0].actual_cost_usd == details[0].reserved_cost_usd == Decimal(0)
    total = provider_cost_totals(outcome.records)
    assert total.usage_complete and total.actual_cost_usd == Decimal("0.0015")
    raw = [RawProviderRecord.model_validate_json(line) for line in test.raw.getvalue().splitlines()]
    assert_raw_count([record for record in raw if record.operation == "count_tokens"], "integer")
    verify_raw_links(raw, list(outcome.records), outcome.row.run_id)


class CollectionCountPort(SyntheticRealCompositionPort):
    def __init__(self, clock: Clock, shape: str, expired: bool) -> None:
        super().__init__()
        self.clock, self.shape, self.expired = clock, shape, expired
        self.count_calls = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.count_calls += 1
        self.clock.now = 56.0 if self.expired else 10.0
        return count_response(self.shape)


def timed_collection(
    directory: Path,
    inputs: tuple[Corpus, GoldDataset],
    monkeypatch: pytest.MonkeyPatch,
    shape: str = "boolean",
    expired: bool = True,
) -> tuple[CollectionCountPort, UUID, list[TraceRecord]]:
    corpus, dataset = inputs
    clock = Clock()

    # Each synthetic attempt starts before its own simulated native-count timeout.
    async def attempt(*args: Any, **kwargs: Any) -> AttemptOutcome:
        clock.now = 10.0
        return await run_attempt(*args, **kwargs, clock=clock)

    monkeypatch.setattr(collection, "run_attempt", attempt)
    port = CollectionCountPort(clock, shape, expired)
    budget = SpendLedger(Decimal("20"))
    manifest = run(directory, dataset, corpus, port, budget=budget)
    assert port.count_calls == 5 and port.actor_calls == port.judge_calls == 0
    assert budget.committed_usd == Decimal(0)
    assert (
        not manifest.collection_complete
        and not manifest.evaluation_complete
        and not manifest.reportable
    )
    assert len(manifest.started_attempts) == 5
    assert not manifest.judge_accounting and not manifest.judge_evaluations
    assert rows(directory, "results.jsonl") == []
    warmups = rows(directory, "warmups.jsonl")
    assert len(warmups) == 5
    code = "deadline_exceeded" if expired else "provider_error"
    assert all(
        result["response"]["error"]["code"] == code and result["classification"] == "error"
        for result in warmups
    )
    records = [
        TraceRecord.model_validate_json(line)
        for line in (directory / "metadata.jsonl").read_text().splitlines()
    ]
    attempt_ids = {record.attempt_id for record in records}
    assert len(attempt_ids) == 5
    for attempt_id in attempt_ids:
        assert_zero_count_accounting(
            [record for record in records if record.attempt_id == attempt_id], code
        )
    total = provider_cost_totals(records)
    assert total.usage_complete and total.actual_cost_usd == total.reserved_cost_usd == Decimal(0)
    return port, manifest.run_id, records


@pytest.mark.parametrize("shape,expired", CASES)
def test_real_partial_collection_count_timeout_replays_without_generation(
    tmp_path: Path,
    inputs: tuple[Corpus, GoldDataset],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expired: bool,
) -> None:
    directory = tmp_path / "partial"
    _, run_id, records = timed_collection(directory, inputs, monkeypatch, shape, expired)
    raw = [
        RawProviderRecord.model_validate_json(line)
        for line in (directory / "raw-provider.jsonl").read_text().splitlines()
    ]
    assert len(raw) == 10
    for attempt_id in {record.attempt_id for record in records}:
        assert_raw_count([record for record in raw if record.attempt_id == attempt_id], shape)
    corpus, dataset = inputs
    assert not verify_partial(directory, corpus, dataset).collection_complete
    verify_raw_links(raw, records, run_id)
    with pytest.raises(ValueError):
        verify_partial(directory, corpus, dataset, complete=True)


@pytest.mark.parametrize(
    "mutation", ["ok", "budget_exhausted", "invalid_generated_output", "extra_field", "generation"]
)
def test_invalid_count_replay_rejects_success_unrelated_errors_fields_and_later_generation(
    tmp_path: Path,
    inputs: tuple[Corpus, GoldDataset],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    directory = tmp_path / "tampered"
    _, run_id, _ = timed_collection(directory, inputs, monkeypatch)
    metadata = rows(directory, "metadata.jsonl")
    raw = rows(directory, "raw-provider.jsonl")
    call = next(record for record in reversed(metadata) if record["record_kind"] == "provider_call")
    if mutation == "extra_field":
        raw[-1]["value"]["extra"] = 1
    elif mutation == "generation":
        generation = {
            **call,
            "provider_call_index": 2,
            "provider_operation": "generation",
            "returned_model_id": ACTOR_MODEL,
            "reserved_cost_usd": "0.027",
        }
        metadata.insert(-1, generation)
        metadata[-1]["reserved_cost_usd"] = "0.027"
    else:
        call["response_code"] = mutation
    replace_rows(directory, "metadata.jsonl", metadata)
    replace_rows(directory, "raw-provider.jsonl", raw)
    corpus, dataset = inputs
    with pytest.raises(ValueError, match="Invalid raw token count response"):
        verify_partial(directory, corpus, dataset)
    with pytest.raises(ValueError, match="Invalid token count evidence"):
        verify_raw_links(
            [RawProviderRecord.model_validate_json(json.dumps(item)) for item in raw],
            [TraceRecord.model_validate_json(json.dumps(item)) for item in metadata],
            run_id,
        )
