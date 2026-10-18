"""Approved input authority through synthetic composition and independently rebuilt replay."""

import asyncio
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message
from test_eval_live import (
    GateFixture,
    activate_synthetic_baseline,
    permit,
    rehash,
    verify,
)
from test_eval_real_collection import SyntheticRealCompositionPort, run

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.collection import RealCollectionManifest, verify_real_collection
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.live import run_live_gate
from rent_navigator.eval.models import Arm, ResultRow
from rent_navigator.eval.offline import file_hash, run_offline
from rent_navigator.eval.runner import approved_facts_from_case
from rent_navigator.extract import EXTRACTION_SYSTEM
from rent_navigator.index import build_index, search
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.trace import provider_cost_totals

SOURCE = "3" * 40


class FactAuthorityPort(SyntheticRealCompositionPort):
    """Explicitly synthetic votes, with optional failure at an approved fact target."""

    def __init__(self, dataset: GoldDataset, target: str, outcome: str) -> None:
        super().__init__()
        self.dataset = dataset
        self.target = approved_facts_from_case(next(c for c in dataset.cases if c.id == target))
        self.outcome = outcome
        self.packets: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        if kwargs["system"] == EXTRACTION_SYSTEM:
            from test_eval_runner import _message

            self.actor_calls += 1
            case = next(
                c for c in self.dataset.cases if c.letter == kwargs["messages"][0]["content"]
            )
            assert case.expected_extract is not None
            return _message([{"type": "text", "text": case.expected_extract.model_dump_json()}])
        message = await super().create(**kwargs)
        if kwargs["model"] != JUDGE_MODEL:
            return message
        packet = json.loads(kwargs["messages"][0]["content"])
        self.packets.append(packet)
        assert self.target is not None
        if packet["approved_facts"] != self.target.model_dump(mode="json"):
            return message
        if self.outcome == "complete":
            return message
        payload = message.model_dump(mode="json")
        payload["content"] = [{"type": "text", "text": "synthetic invalid vote"}]
        if self.outcome == "failed_unknown":
            payload["usage"] = None
            return message.model_copy(update=payload)
        return Message.model_validate(payload)


def mutate_fact_requests(directory: Path, raw_name: str, target: dict[str, Any]) -> int:
    """Jointly rehash locally altered count/create packets, retaining billing and votes."""
    path = directory / raw_name
    records = [json.loads(line) for line in path.read_text().splitlines()]
    changes = 0
    for record in records:
        if record["event"] != "request":
            continue
        packet = json.loads(record["value"]["messages"][0]["content"])
        if packet["approved_facts"] != target:
            continue
        facts = packet["approved_facts"]["facts"]
        if target["kind"] == "rent":
            facts["proposed_cents"] = 103400
        else:
            assert facts["served_on"] is None
            facts["served_on"] = "2026-12-01"
        record["value"]["messages"][0]["content"] = json.dumps(
            packet, sort_keys=True, separators=(",", ":")
        )
        changes += 1
    assert changes >= 2
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    rehash(directory)
    return changes


@pytest.mark.parametrize("case_id", ["R04", "N04"])
@pytest.mark.parametrize("outcome", ["complete", "failed_known", "failed_unknown"])
def test_collection_rebuilds_approved_facts_on_full_and_failed_replay(
    tmp_path: Path, case_id: str, outcome: str
) -> None:
    corpus = load_corpus()
    dataset = load_gold(Path("eval"), corpus)
    case = next(c for c in dataset.cases if c.id == case_id)
    original = case.model_dump_json()
    target = approved_facts_from_case(case)
    assert target is not None
    port = FactAuthorityPort(dataset, case_id, outcome)
    directory = tmp_path / "collection"
    # This port never constructs an SDK client or uses an external transport.
    manifest = run(directory, dataset, corpus, port)
    assert case.model_dump_json() == original
    assert manifest.evaluation_complete == (outcome == "complete")
    if outcome != "complete":
        assert not manifest.collection_complete
        assert not any(
            target.model_dump(mode="json") == packet["approved_facts"]
            for packet in port.packets[
                port.packets.index(
                    next(
                        p
                        for p in port.packets
                        if p["approved_facts"] == target.model_dump(mode="json")
                    )
                )
                + 1 :
            ]
        )
    verify_real_collection(
        directory,
        dataset=dataset,
        corpus=corpus,
        source_sha="1" * 40,
        lock_hash="2" * 64,
        manifest_sha256=file_hash(directory / "manifest.json"),
        require_complete=outcome == "complete",
    )
    raw_before = (directory / "judge-metadata.jsonl").read_bytes()
    accounting_before = {
        key: value.model_dump_json() for key, value in manifest.judge_accounting.items()
    }
    target_accounting = next(
        manifest.judge_accounting[str(row.attempt_id)]
        for line in (directory / "results.jsonl").read_text().splitlines()
        if (row := ResultRow.model_validate_json(line)).case_id == case_id
    )
    if outcome == "failed_unknown":
        assert target_accounting.cost.actual_cost_usd is None
        assert target_accounting.cost.reserved_cost_usd == Decimal("0.21384")
        assert not target_accounting.cost.usage_complete
    else:
        assert target_accounting.cost.actual_cost_usd == Decimal("0.003")
    mutate_fact_requests(directory, "judge-raw-provider.jsonl", target.model_dump(mode="json"))
    with pytest.raises(ValueError, match="request|packet"):
        verify_real_collection(
            directory,
            dataset=dataset,
            corpus=corpus,
            source_sha="1" * 40,
            lock_hash="2" * 64,
            manifest_sha256=file_hash(directory / "manifest.json"),
            require_complete=outcome == "complete",
        )
    retained = RealCollectionManifest.model_validate_json(
        (directory / "manifest.json").read_bytes()
    )
    assert (directory / "judge-metadata.jsonl").read_bytes() == raw_before
    assert accounting_before == {
        key: value.model_dump_json() for key, value in retained.judge_accounting.items()
    }
    assert provider_cost_totals(target_accounting.records) == target_accounting.cost
    assert case.model_dump_json() == original


@pytest.mark.parametrize("case_id", ["R04", "N04"])
@pytest.mark.parametrize("outcome", ["complete", "failed_known", "failed_unknown"])
def test_live_rebuilds_approved_facts_on_full_and_failed_replay(
    tmp_path: Path, case_id: str, outcome: str
) -> None:
    from test_eval_security import synthetic_security_xml

    corpus = load_corpus()
    data_dir = tmp_path / "data"
    shutil.copytree(Path("eval"), data_dir)
    dataset = activate_synthetic_baseline(data_dir, corpus)
    case = next(c for c in dataset.cases if c.id == case_id)
    target = approved_facts_from_case(case)
    assert target is not None
    lock = tmp_path / "lock"
    lock.write_text("synthetic fact authority lock\n")
    offline = tmp_path / "offline"
    run_offline(
        data_dir,
        offline,
        corpus=corpus,
        source_sha=SOURCE,
        security_report=synthetic_security_xml(tmp_path / "security.xml"),
        lock_path=lock,
    )
    index = tmp_path / "index.sqlite"
    build_index(index, corpus=corpus)
    directory = tmp_path / "gate"
    manifest = asyncio.run(
        run_live_gate(
            data_dir,
            directory,
            dataset=dataset,
            corpus=corpus,
            source_sha=SOURCE,
            lock_path=lock,
            offline_dir=offline,
            permit=permit(file_hash(data_dir / "baseline.json")),
            messages=FactAuthorityPort(dataset, case_id, outcome),
            retrieve=lambda q: search(index, q),
        )
    )
    fixture = GateFixture(data_dir, directory, corpus, dataset, lock, offline, index, manifest)
    verify(fixture, require_pass=outcome == "complete")
    receipt = (directory / "receipt.json").read_bytes()
    cost_before = {
        key: value.cost.model_dump_json() for key, value in manifest.judge_accounting.items()
    }
    mutate_fact_requests(directory, "judge-raw.jsonl", target.model_dump(mode="json"))
    with pytest.raises(ValueError, match="request|packet"):
        verify(fixture, require_pass=outcome == "complete")
    assert (directory / "receipt.json").read_bytes() == receipt
    assert cost_before == {
        key: value.cost.model_dump_json() for key, value in manifest.judge_accounting.items()
    }


def test_actual_both_arm_judge_inputs_use_identical_original_letter_facts() -> None:
    from test_eval_runner import _final, _harness, _message, _selection

    corpus: Corpus = load_corpus()
    arms: tuple[Arm, ...] = ("production", "baseline")
    for case in load_gold(Path("eval"), corpus).cases:
        original = case.model_dump_json()
        envelopes = []
        for arm in arms:
            harness = _harness(corpus, case.id, arm=arm)
            harness.case = case
            responses: list[Message | BaseException] = []
            if case.letter is not None:
                assert case.expected_extract is not None
                responses.append(
                    _message([{"type": "text", "text": case.expected_extract.model_dump_json()}])
                )
            if case.kind != "qa":
                responses.append(_selection(case))
            responses.append(_final(corpus, arm))
            harness.fake.responses = responses
            outcome = harness.run()
            assert harness.judge is not None and harness.judge.inputs, outcome.reasons
            envelope = harness.judge.inputs[0].approved_facts
            assert envelope == approved_facts_from_case(case)
            envelopes.append(None if envelope is None else envelope.model_dump_json())
            assert case.model_dump_json() == original
        assert envelopes[0] == envelopes[1]
