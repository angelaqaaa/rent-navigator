"""Accepted attempt records distinguish prepared context from observed requests."""

import json
from typing import Any
from uuid import uuid4

import pytest
from test_eval_runner import _extraction, _harness

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.collection import _verify_observations
from rent_navigator.eval.recording import RawProviderRecord, verify_synthetic_records
from rent_navigator.index import SearchHit
from rent_navigator.models import ErrorResponse


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.mark.parametrize("stage", ["extraction_only", "retrieval_failure", "prepared_timeout"])
def test_failed_attempt_acceptance_preserves_unobserved_preparation(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    harness = _harness(corpus, "R02")
    retrieve = harness.retrieve

    def interrupted(query: str) -> tuple[SearchHit, ...]:
        if stage == "retrieval_failure":
            raise ValueError("Synthetic retrieval failure")
        hits = retrieve(query)
        harness.clock.now += 46
        return hits

    if stage == "extraction_only":
        harness.fake.responses[0] = _extraction(current_cents=1)
    else:
        monkeypatch.setattr(harness, "retrieve", interrupted)
    outcome = harness.run()
    raw: list[dict[str, Any]] = [json.loads(line) for line in harness.raw.getvalue().splitlines()]
    assert raw and {record["phase"] for record in raw} == {"extraction"}
    if stage == "prepared_timeout":
        assert isinstance(outcome.row.response, ErrorResponse)
        assert outcome.row.response.error.code == "deadline_exceeded"
        assert outcome.row.retrieved_ids
        assert outcome.row.initial_context_evidence_ids == outcome.row.retrieved_ids
    else:
        assert outcome.row.retrieved_ids == outcome.row.initial_context_evidence_ids == []
    assert outcome.row.foundation_evidence_ids == []
    assert outcome.row.classification != "success"
    verify_synthetic_records(
        [RawProviderRecord.model_validate_json(json.dumps(item)) for item in raw],
        case=harness.case,
        corpus=corpus,
        arm="production",
    )
    _verify_observations(outcome.row, harness.case, list(outcome.records), raw, corpus)


@pytest.mark.parametrize("change", ["trace", "phase", "attempt", "context"])
def test_observed_raw_context_must_link_to_the_matching_endpoint(
    corpus: Corpus, change: str
) -> None:
    harness = _harness(corpus)
    outcome = harness.run()
    raw: list[dict[str, Any]] = [json.loads(line) for line in harness.raw.getvalue().splitlines()]
    if change == "trace":
        raw[0]["trace_id"] = str(uuid4())
    elif change == "phase":
        raw[0]["phase"] = "extraction"
        raw[0]["context_provenance"] = {
            "retrieved_evidence_ids": [],
            "foundation_evidence_ids": [],
            "initial_context_evidence_ids": [],
        }
    elif change == "attempt":
        raw[0]["attempt_id"] = str(uuid4())
    else:
        raw[0]["context_provenance"]["foundation_evidence_ids"] = []
    from rent_navigator.eval.collection import verify_prepared_context

    with pytest.raises(ValueError):
        verify_prepared_context(
            outcome.records,
            [RawProviderRecord.model_validate_json(json.dumps(item)) for item in raw],
            mode=harness.case.request.mode,
            arm="production",
            corpus=corpus,
            retrieved_ids=outcome.row.retrieved_ids,
            foundation_evidence_ids=outcome.row.foundation_evidence_ids,
            initial_context_evidence_ids=outcome.row.initial_context_evidence_ids,
        )
