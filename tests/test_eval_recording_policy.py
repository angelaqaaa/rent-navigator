"""Synthetic recorder boundaries rebuild schemas from actual prepared observations."""

import asyncio
import json
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from test_eval_runner import Harness, _final, _harness

from rent_navigator.agent import _final_schema
from rent_navigator.context import context_provenance, query_for_request
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.recording import (
    RawProviderRecord,
    SyntheticAllowlist,
    SyntheticRecorder,
    verify_synthetic_records,
)
from rent_navigator.index import SearchHit, build_index, search
from rent_navigator.models import ToolResult
from rent_navigator.provider import ProviderFailure


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture(scope="module")
def index(corpus: Corpus, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("recording-index") / "corpus.sqlite"
    build_index(path, corpus)
    return path


@pytest.fixture(autouse=True)
def actual_retrieval(index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def retrieve(harness: Harness, question: str) -> tuple[SearchHit, ...]:
        harness.queries.append(question)
        return search(index, question, expected_corpus_hash=harness.corpus.corpus_hash)

    monkeypatch.setattr(Harness, "retrieve", retrieve)


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
    if harness.arm == "production":
        recorder.observe_retrieval(harness.retrieve(query_for_request(harness.case.request)))
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
    provenance = context_provenance("question", "production", (), corpus)
    initial["evidence"] = [
        {"id": chunk.id, "heading": chunk.heading, "text": chunk.text}
        for chunk in (corpus.chunk(i) for i in provenance.initial_context_evidence_ids)
    ]
    payload["messages"][0]["content"] = json.dumps(initial)
    payload["output_config"]["format"]["schema"] = _final_schema(
        "production", set(provenance.initial_context_evidence_ids)
    )
    recorder.observe_retrieval(())
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
    expected = {*outcome.row.retrieved_ids, *corpus.rule("scope.ordinary").evidence_ids}
    assert citations(harness.fake.counts[-1])["items"]["enum"] == sorted(expected)
    verify(harness, records(harness))
    assert len(harness.queries) == 1
    gold_allowed = set(outcome.row.retrieved_ids)
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


def test_accepted_replay_independently_recomputes_context_and_requires_consistency(
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


def ranked_question(corpus: Corpus) -> Harness:
    harness = _harness(corpus)
    request = harness.case.request.model_copy(update={"question": "Ontario rent guideline"})
    harness.case = harness.case.model_copy(update={"request": request})
    return harness


@pytest.mark.parametrize("mutation", ["swap", "substitute"])
def test_independent_rank_rejects_changed_seeds_inside_unchanged_foundation(
    corpus: Corpus, mutation: str
) -> None:
    harness = ranked_question(corpus)
    harness.run()
    raw = records(harness)
    verify(harness, raw)
    provenance = raw[0].context_provenance
    seed_ids = list(provenance.retrieved_evidence_ids)
    foundation = set(provenance.foundation_evidence_ids)
    positions = [i for i, identifier in enumerate(seed_ids) if identifier in foundation]
    assert len(positions) >= 2
    if mutation == "swap":
        first, second = positions[:2]
        seed_ids[first], seed_ids[second] = seed_ids[second], seed_ids[first]
    else:
        seed_ids[positions[0]] = next(iter(sorted(foundation - set(seed_ids))))
    original_payloads = [deepcopy(record.value) for record in raw]
    original_context = list(provenance.initial_context_evidence_ids)
    forged_context = context_provenance("question", "production", tuple(seed_ids), corpus)
    assert forged_context.initial_context_evidence_ids == original_context
    for record in raw:
        record.context_provenance = forged_context.model_copy(deep=True)
    assert [record.value for record in raw] == original_payloads
    # Even a candidate row's matching list cannot become the ranking authority.
    for assertion in (None, tuple(seed_ids)):
        with pytest.raises(ValueError, match="allowed scenario"):
            verify_synthetic_records(
                raw, case=harness.case, corpus=corpus, arm="production", retrieved_ids=assertion
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "object_missing",
        "object_null",
        "extra",
        "R_missing",
        "F_missing",
        "C_missing",
        "R_null",
        "F_null",
        "C_null",
        "R_duplicate",
        "F_duplicate",
        "C_duplicate",
    ],
)
def test_raw_wire_requires_closed_nonnull_unique_provenance(corpus: Corpus, mutation: str) -> None:
    harness = ranked_question(corpus)
    harness.run()
    wire = records(harness)[0].model_dump(mode="json")
    if mutation == "object_missing":
        del wire["context_provenance"]
    elif mutation == "object_null":
        wire["context_provenance"] = None
    elif mutation == "extra":
        wire["context_provenance"]["extra"] = []
    else:
        short, action = mutation.split("_")
        name = {
            "R": "retrieved_evidence_ids",
            "F": "foundation_evidence_ids",
            "C": "initial_context_evidence_ids",
        }[short]
        if action == "missing":
            del wire["context_provenance"][name]
        elif action == "null":
            wire["context_provenance"][name] = None
        else:
            wire["context_provenance"][name] = [corpus.chunks[0].id] * 2
    with pytest.raises(ValueError):
        RawProviderRecord.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize("terminal_failure", [False, True])
@pytest.mark.parametrize("event_index", range(4))
def test_every_raw_event_must_keep_identical_derived_context(
    corpus: Corpus, terminal_failure: bool, event_index: int
) -> None:
    harness = _harness(corpus)
    if terminal_failure:
        harness.fake.responses[0] = RuntimeError("synthetic provider failure")
    harness.run()
    raw = records(harness)
    assert len(raw) == 4
    assert raw[-1].event == ("failure" if terminal_failure else "response")
    assert all(record.context_provenance == raw[0].context_provenance for record in raw)
    verify(harness, raw)
    raw[event_index].context_provenance.foundation_evidence_ids.pop()
    with pytest.raises(ValueError):
        verify(harness, raw)


@pytest.mark.parametrize("mutation", ["phase", "trace", "index", "attempt", "run"])
def test_raw_event_association_cannot_change(corpus: Corpus, mutation: str) -> None:
    harness = _harness(corpus)
    harness.run()
    raw = records(harness)
    updates: dict[str, dict[str, Any]] = {
        "phase": {"phase": "extraction"},
        "trace": {"trace_id": uuid4()},
        "index": {"operation_index": 9},
        "attempt": {"attempt_id": uuid4()},
        "run": {"run_id": uuid4()},
    }
    raw[1] = raw[1].model_copy(update=updates[mutation])
    with pytest.raises(ValueError):
        verify(harness, raw)


def test_bind_clears_retrieval_provenance_and_actual_tool_state(corpus: Corpus) -> None:
    harness = _harness(corpus, "R02")
    harness.run()
    recorder, _, stream = bound_recorder(harness)
    recorder.actual_tool_args = harness.case.expected_tool_args
    recorder.actual_tool_result = harness.case.expected_tool_result
    recorder.bind(uuid4(), "extraction", harness.case.request)
    assert recorder.expected_retrieved_ids is None
    assert recorder.actual_tool_args is None
    assert recorder.actual_tool_result is None
    asyncio.run(recorder.count_tokens(**deepcopy(harness.fake.counts[0])))
    extraction = [
        RawProviderRecord.model_validate_json(line) for line in stream.getvalue().splitlines()
    ]
    assert all(
        record.context_provenance.model_dump()
        == {
            "retrieved_evidence_ids": [],
            "foundation_evidence_ids": [],
            "initial_context_evidence_ids": [],
        }
        for record in extraction
    )
    recorder.bind(uuid4(), "analysis", harness.case.request)
    assert recorder.expected_retrieved_ids is None
    before = stream.getvalue()
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.count_tokens(**deepcopy(harness.fake.counts[1])))
    assert stream.getvalue() == before
    recorder.observe_retrieval(harness.retrieve(query_for_request(harness.case.request)))
    asyncio.run(recorder.count_tokens(**deepcopy(harness.fake.counts[1])))
    recorder.bind(uuid4(), "analysis", harness.case.request)
    assert recorder.expected_retrieved_ids is None
    with pytest.raises(ProviderFailure):
        asyncio.run(recorder.count_tokens(**deepcopy(harness.fake.counts[1])))


@pytest.mark.parametrize("problem", ["duplicate", "too_many", "noncanonical"])
def test_invalid_observed_seeds_do_not_leave_stale_retrieval(corpus: Corpus, problem: str) -> None:
    harness = _harness(corpus)
    recorder, _, stream = bound_recorder(harness)
    valid = SearchHit(corpus.chunks[0], -1)
    bad = (
        (valid, valid)
        if problem == "duplicate"
        else tuple(SearchHit(c, -1) for c in corpus.chunks[:6])
    )
    if problem == "noncanonical":
        bad = (SearchHit(corpus.chunks[0].model_copy(update={"text": "changed"}), -1),)
    with pytest.raises((ProviderFailure, ValueError)):
        recorder.observe_retrieval(bad)
    assert recorder.expected_retrieved_ids is None
    assert stream.getvalue() == ""


@pytest.mark.parametrize("stage", ["count", "generation"])
def test_failed_recording_port_preserves_observation_without_claiming_a_response(
    corpus: Corpus, stage: str
) -> None:
    from rent_navigator.eval.critical import native_observation

    harness = _harness(corpus)
    if stage == "count":
        harness.fake.estimates[0] = RuntimeError("synthetic count transport failure")
    else:
        harness.fake.responses[0] = ProviderFailure("budget_exhausted")
    harness.run()
    raw = records(harness)
    observed = native_observation(harness.case, raw, corpus)
    assert observed.analysis_context_observed
    assert observed.complete and observed.actor_responses == 0
    assert observed.generated is None
    assert observed.actual_tool_args is None
    assert observed.actual_tool_result is None
    assert raw[-1].event == "failure"
    assert len(raw) == (2 if stage == "count" else 4)
    assert len(harness.fake.creates) == (0 if stage == "count" else 1)
    # A captured generation request can fail at downstream admission without a response.
    assert all(record.context_provenance == raw[0].context_provenance for record in raw)
