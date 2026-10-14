"""Native analysis observations remain distinct from prepared endpoint context."""

import json
from typing import Any

import pytest
from anthropic.types import Message
from test_eval_runner import _extraction, _harness

from rent_navigator.context import foundation_ids
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.critical import critical_gold, native_observation
from rent_navigator.eval.recording import RawProviderRecord
from rent_navigator.index import SearchHit
from rent_navigator.models import ErrorResponse


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def raw_records(value: str) -> list[RawProviderRecord]:
    return [RawProviderRecord.model_validate_json(line) for line in value.splitlines()]


def test_no_raw_analysis_has_explicit_unobserved_context(corpus: Corpus) -> None:
    harness = _harness(corpus)
    observation = native_observation(harness.case, [], corpus)
    assert not observation.complete
    assert not observation.analysis_context_observed
    assert observation.retrieved_ids == ()
    assert observation.foundation_evidence_ids == observation.initial_context_evidence_ids == ()
    assert observation.actor_responses == 0 and observation.generated is None


def test_extraction_only_response_does_not_observe_analysis(corpus: Corpus) -> None:
    harness = _harness(corpus, "R02")
    harness.fake.responses[0] = _extraction(current_cents=1)
    outcome = harness.run()
    raw = raw_records(harness.raw.getvalue())
    assert {record.phase for record in raw} == {"extraction"}
    observation = native_observation(harness.case, raw, corpus)
    assert observation.complete and not observation.analysis_context_observed
    assert observation.retrieved_ids == observation.foundation_evidence_ids == ()
    assert observation.initial_context_evidence_ids == ()
    assert observation.actor_responses == 0 and observation.generated is None
    assert outcome.row.classification != "success"


def test_prepared_analysis_after_extraction_is_not_fabricated_as_observed(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(corpus, "R02")
    retrieve = harness.retrieve

    def delayed(question: str) -> tuple[SearchHit, ...]:
        hits = retrieve(question)
        harness.clock.now += 46
        return hits

    monkeypatch.setattr(harness, "retrieve", delayed)
    outcome = harness.run()
    raw = raw_records(harness.raw.getvalue())
    assert isinstance(outcome.row.response, ErrorResponse)
    assert outcome.row.response.error.code == "deadline_exceeded"
    assert outcome.row.retrieved_ids
    assert outcome.row.initial_context_evidence_ids == outcome.row.retrieved_ids
    assert outcome.row.foundation_evidence_ids == []
    assert {record.phase for record in raw} == {"extraction"}
    observation = native_observation(harness.case, raw, corpus)
    assert not observation.analysis_context_observed
    assert observation.retrieved_ids == observation.initial_context_evidence_ids == ()
    assert observation.actor_responses == 0 and observation.generated is None
    # Complete retained extraction is auditable without comparing unobserved native seeds
    # to successfully prepared analysis context; the row remains a failed attempt.
    assert critical_gold(harness.case, outcome.row, raw, corpus).complete
    assert outcome.row.classification == "error"
    forged_success = outcome.row.model_copy(update={"classification": "success"})
    assert not critical_gold(harness.case, forged_success, raw, corpus).complete


def test_observed_zero_seed_question_reports_foundation_separately(corpus: Corpus) -> None:
    harness = _harness(corpus)
    outcome = harness.run()
    observation = native_observation(harness.case, raw_records(harness.raw.getvalue()), corpus)
    assert observation.analysis_context_observed and observation.complete
    assert observation.retrieved_ids == ()
    assert observation.foundation_evidence_ids == foundation_ids(corpus)
    assert observation.initial_context_evidence_ids == foundation_ids(corpus)
    assert observation.actual_tool_args is None
    assert observation.actual_tool_result is None
    assert outcome.row.retrieved_ids == []


@pytest.mark.parametrize("case_id", ["Q01", "R01"])
def test_baseline_analysis_can_be_observed_with_empty_context(corpus: Corpus, case_id: str) -> None:
    harness = _harness(corpus, case_id, arm="baseline")
    harness.run()
    observation = native_observation(
        harness.case, raw_records(harness.raw.getvalue()), corpus, arm="baseline"
    )
    assert observation.complete and observation.analysis_context_observed
    assert observation.retrieved_ids == observation.foundation_evidence_ids == ()
    assert observation.initial_context_evidence_ids == ()
    assert (observation.actual_tool_result is not None) == (case_id == "R01")


@pytest.mark.parametrize("outside", [False, True])
def test_critical_citations_use_actual_initial_context_not_seed_list(
    corpus: Corpus, outside: bool
) -> None:
    harness = _harness(corpus)
    allowed = set(foundation_ids(corpus))
    identifier = (
        next(chunk.id for chunk in corpus.chunks if chunk.id not in allowed)
        if outside
        else foundation_ids(corpus)[0]
    )
    response = harness.fake.responses[0]
    assert isinstance(response, Message)
    wire: dict[str, Any] = response.model_dump(mode="json")
    generated = json.loads(wire["content"][0]["text"])
    generated["statements"][0]["citation_ids"] = [identifier]
    wire["content"][0]["text"] = json.dumps(generated)
    harness.fake.responses[0] = Message.model_validate(wire)
    outcome = harness.run()
    assert outcome.row.retrieved_ids == []
    result = critical_gold(harness.case, outcome.row, raw_records(harness.raw.getvalue()), corpus)
    assert result.complete
    assert result.flags == (("critical_citation",) if outside else ())


@pytest.mark.parametrize(
    "field", ["retrieved_ids", "foundation_evidence_ids", "initial_context_evidence_ids"]
)
def test_observed_context_must_match_each_row_provenance_field(corpus: Corpus, field: str) -> None:
    harness = _harness(corpus)
    outcome = harness.run()
    row = outcome.row.model_copy(update={field: [corpus.chunks[-1].id]})
    result = critical_gold(harness.case, row, raw_records(harness.raw.getvalue()), corpus)
    assert not result.complete
