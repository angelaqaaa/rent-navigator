"""Pure fixed-reference assembly; no provider or scoring observations."""

import json
from dataclasses import replace
from typing import Literal

import pytest
from pydantic import ValidationError
from test_agent import request_for

from rent_navigator.context import (
    ContextProvenance,
    context_provenance,
    foundation_ids,
    query_for_request,
)
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.index import NOTICE_QUERY, RENT_QUERY


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def test_foundation_is_all_existing_rule_evidence_unique_in_corpus_order(corpus: Corpus) -> None:
    expected = {identifier for rule in corpus.rules for identifier in rule.evidence_ids}
    actual = foundation_ids(corpus)
    assert actual == tuple(chunk.id for chunk in corpus.chunks if chunk.id in expected)
    assert len(actual) == len(set(actual)) == 21
    assert sum(corpus.chunk(identifier).word_count for identifier in actual) == 5092


def test_question_unions_foundation_and_seeds_without_losing_rank_provenance(
    corpus: Corpus,
) -> None:
    foundation = foundation_ids(corpus)
    outside = next(chunk.id for chunk in corpus.chunks if chunk.id not in foundation)
    seeds = (outside, foundation[-1], foundation[0])
    result = context_provenance("question", "production", seeds, corpus)
    assert result.retrieved_evidence_ids == list(seeds)
    assert result.foundation_evidence_ids == list(foundation)
    assert result.initial_context_evidence_ids == [
        chunk.id for chunk in corpus.chunks if chunk.id in set(foundation) | set(seeds)
    ]
    reversed_seeds = context_provenance("question", "production", tuple(reversed(seeds)), corpus)
    assert reversed_seeds.initial_context_evidence_ids == result.initial_context_evidence_ids
    assert reversed_seeds.retrieved_evidence_ids != result.retrieved_evidence_ids


@pytest.mark.parametrize("mode", ["notice", "rent"])
def test_facts_preserve_rank_order_without_foundation(
    corpus: Corpus, mode: Literal["notice", "rent"]
) -> None:
    seeds = tuple(chunk.id for chunk in reversed(corpus.chunks[:5]))
    result = context_provenance(mode, "production", seeds, corpus)
    assert result.retrieved_evidence_ids == result.initial_context_evidence_ids == list(seeds)
    assert result.foundation_evidence_ids == []


@pytest.mark.parametrize("mode", ["question", "notice", "rent"])
def test_baseline_has_no_hidden_context(
    corpus: Corpus, mode: Literal["question", "notice", "rent"]
) -> None:
    assert context_provenance(mode, "baseline", (), corpus).model_dump() == {
        "retrieved_evidence_ids": [],
        "foundation_evidence_ids": [],
        "initial_context_evidence_ids": [],
    }
    with pytest.raises(ValueError):
        context_provenance(mode, "baseline", (corpus.chunks[0].id,), corpus)


def test_normal_zero_seed_question_still_has_fixed_foundation(corpus: Corpus) -> None:
    result = context_provenance("question", "production", (), corpus)
    assert result.retrieved_evidence_ids == []
    assert (
        result.foundation_evidence_ids
        == result.initial_context_evidence_ids
        == list(foundation_ids(corpus))
    )


@pytest.mark.parametrize(
    "invalid", ["duplicates", "over_five", "unknown", "bad_rule", "missing_rule"]
)
def test_bad_seed_or_foundation_provenance_is_not_zero_hits(corpus: Corpus, invalid: str) -> None:
    seeds: tuple[str, ...] = (corpus.chunks[0].id,)
    if invalid == "duplicates":
        seeds *= 2
    elif invalid == "over_five":
        seeds = tuple(chunk.id for chunk in corpus.chunks[:6])
    elif invalid == "unknown":
        seeds = ("f" * 64,)
    elif invalid == "bad_rule":
        corpus = replace(
            corpus,
            rules=(
                corpus.rules[0].model_copy(update={"evidence_ids": ("f" * 64,)}),
                *corpus.rules[1:],
            ),
        )
    else:
        corpus = replace(corpus, rules=corpus.rules[:-1])
    with pytest.raises((ValueError, KeyError)):
        context_provenance("question", "production", seeds, corpus)


@pytest.mark.parametrize(
    "field", ["retrieved_evidence_ids", "foundation_evidence_ids", "initial_context_evidence_ids"]
)
@pytest.mark.parametrize("mutation", ["missing", "null", "duplicate", "not_sha256"])
def test_public_context_provenance_wire_is_required_closed_and_unique(
    field: str, mutation: str
) -> None:
    payload: dict[str, list[str]] = {
        "retrieved_evidence_ids": [],
        "foundation_evidence_ids": [],
        "initial_context_evidence_ids": [],
    }
    value: dict[str, object] = dict(payload)
    if mutation == "missing":
        del value[field]
    else:
        value[field] = {
            "null": None,
            "duplicate": ["a" * 64, "a" * 64],
            "not_sha256": ["private text"],
        }[mutation]
    with pytest.raises(ValidationError):
        ContextProvenance.model_validate_json(json.dumps(value))
    with pytest.raises(ValidationError):
        ContextProvenance.model_validate_json(json.dumps({**payload, "origin": "invented"}))


def test_shared_query_policy_preserves_original_queries() -> None:
    request = request_for("question")
    assert request.mode == "question"
    assert query_for_request(request) == request.question
    assert query_for_request(request_for("notice")) == NOTICE_QUERY
    assert query_for_request(request_for("rent")) == RENT_QUERY
