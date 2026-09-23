"""Synthetic recorder boundaries rebuild schemas from actual prepared observations."""

import asyncio
import json
from copy import deepcopy
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from test_eval_runner import Harness, _final, _harness

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.recording import (
    RawProviderRecord,
    SyntheticAllowlist,
    SyntheticRecorder,
    verify_synthetic_records,
)
from rent_navigator.models import ToolResult
from rent_navigator.provider import ProviderFailure


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def records(harness: Harness) -> list[RawProviderRecord]:
    return [
        RawProviderRecord.model_validate_json(line) for line in harness.raw.getvalue().splitlines()
    ]


def citations(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = value["output_config"]["format"]["schema"]["$defs"]["Statement"][
        "properties"
    ]["citation_ids"]
    return result


def verify(harness: Harness, raw: list[RawProviderRecord]) -> None:
    verify_synthetic_records(
        raw,
        case=harness.case,
        corpus=harness.corpus,
        arm=harness.arm,
        retrieved_ids=(harness.corpus.chunks[0].id,) if harness.arm == "production" else (),
    )


@pytest.mark.parametrize(
    "mutation", ["extra", "missing", "missing_enum", "zero_min", "missing_min"]
)
def test_historical_raw_rejects_forged_dynamic_schema(corpus: Corpus, mutation: str) -> None:
    harness = _harness(corpus, "R01")
    harness.run()
    raw = records(harness)
    verify(harness, raw)
    for record in raw:
        if record.event != "request" or "output_config" not in record.value:
            continue
        field = citations(record.value)
        if mutation == "extra":
            field["items"]["enum"].append(
                next(chunk.id for chunk in corpus.chunks if chunk.id not in field["items"]["enum"])
            )
        elif mutation == "missing":
            field["items"]["enum"].pop()
        elif mutation == "missing_enum":
            del field["items"]["enum"]
        elif mutation == "zero_min":
            field["minItems"] = 0
        else:
            del field["minItems"]
    # Both count/create requests were modified alike; content validation still rejects them.
    with pytest.raises(ValueError):
        verify(harness, raw)


@pytest.mark.parametrize("mutation", ["extra_passage", "missing_passage", "wrong_retrieval"])
def test_canonical_passages_must_equal_actual_retrieval_and_executed_rules(
    corpus: Corpus,
    mutation: str,
) -> None:
    harness = _harness(corpus, "R01")
    harness.run()
    raw = records(harness)
    for record in raw:
        if record.event != "request" or "output_config" not in record.value:
            continue
        if mutation == "wrong_retrieval":
            initial = json.loads(record.value["messages"][0]["content"])
            chunk = corpus.chunks[1]
            initial["evidence"] = [{"id": chunk.id, "heading": chunk.heading, "text": chunk.text}]
            record.value["messages"][0]["content"] = json.dumps(initial)
            continue
        for block in record.value["messages"][-1]["content"]:
            if block["type"] != "text":
                continue
            packet = json.loads(block["text"])
            if "evidence" not in packet:
                continue
            if mutation == "missing_passage":
                packet["evidence"].pop()
            else:
                allowed = citations(record.value)["items"]["enum"]
                chunk = next(chunk for chunk in corpus.chunks if chunk.id not in allowed)
                packet["evidence"].append(
                    {"id": chunk.id, "heading": chunk.heading, "text": chunk.text}
                )
                allowed.append(chunk.id)
                allowed.sort()
            block["text"] = json.dumps(packet)
    with pytest.raises(ValueError):
        verify(harness, raw)


class Port:
    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.generations = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        self.generations += 1
        return _final(self.corpus, "production")


def bound_recorder(harness: Harness) -> tuple[SyntheticRecorder, Port, StringIO]:
    stream, port = StringIO(), Port(harness.corpus)
    recorder = SyntheticRecorder(
        port,
        case=harness.case,
        allowlist=SyntheticAllowlist.from_fixtures([harness.case]),
        corpus=harness.corpus,
        stream=stream,
        run_id=uuid4(),
        attempt_id=harness.case.request.attempt_id,
    )
    recorder.bind(uuid4(), "analysis", harness.case.request, arm=harness.arm)
    recorder.expected_retrieved_ids = (harness.corpus.chunks[0].id,)
    return recorder, port, stream


def test_online_count_create_mismatch_is_rejected_before_dispatch(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.run()
    recorder, port, stream = bound_recorder(harness)
    asyncio.run(recorder.count_tokens(**deepcopy(harness.fake.counts[0])))
    payload = deepcopy(harness.fake.creates[0])
    citations(payload)["minItems"] = 0
    before = stream.getvalue()
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.create(**payload))
    assert port.generations == 0 and stream.getvalue() == before


def test_missing_observation_differs_from_observed_empty_retrieval(corpus: Corpus) -> None:
    harness = _harness(corpus)
    harness.run()
    recorder, _, _ = bound_recorder(harness)
    payload = deepcopy(harness.fake.counts[0])
    recorder.expected_retrieved_ids = None
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.count_tokens(**payload))
    initial = json.loads(payload["messages"][0]["content"])
    initial["evidence"] = []
    payload["messages"][0]["content"] = json.dumps(initial)
    field = citations(payload)
    del field["items"]["enum"]
    del field["minItems"]
    recorder.expected_retrieved_ids = ()
    assert asyncio.run(recorder.count_tokens(**payload)).input_tokens == 1000


def test_actual_result_not_gold_defines_rule_union(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rent_navigator.agent as agent

    harness = _harness(corpus, "R01")
    assert harness.case.expected_tool_result is not None
    data = harness.case.expected_tool_result.model_dump(mode="json")
    for check in data["checks"]:
        check["rule_ids"] = ["scope.ordinary"]
    data["rule_ids"] = ["scope.ordinary"]
    observed = ToolResult.model_validate_json(json.dumps(data))
    monkeypatch.setattr(agent, "rent_increase_check", lambda *_args, **_kwargs: observed)
    outcome = harness.run()
    assert outcome.row.actual_tool_result == observed != harness.case.expected_tool_result
    expected = {corpus.chunks[0].id, *corpus.rule("scope.ordinary").evidence_ids}
    assert citations(harness.fake.counts[-1])["items"]["enum"] == sorted(expected)
    verify(harness, records(harness))
    assert len(harness.queries) == 1
    gold_allowed = {corpus.chunks[0].id}
    for rule_id in harness.case.expected_tool_result.rule_ids:
        gold_allowed.update(corpus.rule(rule_id).evidence_ids)
    assert gold_allowed != expected
    forged = records(harness)
    for record in forged:
        if record.event == "request" and "output_config" in record.value:
            citations(record.value)["items"]["enum"] = sorted(gold_allowed)
    with pytest.raises(ValueError):
        verify(harness, forged)


def test_baseline_has_no_retrieval_or_dynamic_constraint(corpus: Corpus) -> None:
    harness = _harness(corpus, "R01", arm="baseline")
    harness.run()
    assert not harness.queries
    assert "enum" not in citations(harness.fake.counts[-1])["items"]
    assert "minItems" not in citations(harness.fake.counts[-1])
    verify(harness, records(harness))


def test_historical_default_binds_first_canonical_context_and_requires_consistency(
    corpus: Corpus,
) -> None:
    harness = _harness(corpus)
    harness.run()
    raw = records(harness)
    verify_synthetic_records(raw, case=harness.case, corpus=corpus, arm="production")
    generation = next(
        record for record in raw if record.event == "request" and record.operation == "generation"
    )
    initial = json.loads(generation.value["messages"][0]["content"])
    replacement = corpus.chunks[1]
    initial["evidence"] = [
        {"id": replacement.id, "heading": replacement.heading, "text": replacement.text}
    ]
    generation.value["messages"][0]["content"] = json.dumps(initial)
    citations(generation.value)["items"]["enum"] = [replacement.id]
    with pytest.raises(ValueError):
        verify_synthetic_records(raw, case=harness.case, corpus=corpus, arm="production")
