"""Independent synthetic ranking, judgment and collection arithmetic fixtures."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from math import log2
from typing import Literal
from uuid import UUID

import pytest

from rent_navigator.eval.metrics import (
    aggregate,
    classify,
    nearest_rank,
    ranking_score,
    retrieval_scores,
)
from rent_navigator.eval.models import (
    CASE_IDS,
    Arm,
    CaseId,
    Claim,
    DeterministicAssertion,
    GoldCase,
    JudgeClaimResult,
    JudgeResult,
    JudgeStatementResult,
    ResultRow,
)
from rent_navigator.models import (
    AskResponse,
    CheckResult,
    ErrorDetail,
    ErrorResponse,
    NoticeFacts,
    NoticeRequest,
    QuestionRequest,
    Scope,
    Statement,
    ToolResult,
    disclaimer_for,
)

DAY = date(2026, 1, 1)
CHUNK_A, CHUNK_B, CHUNK_C = "a" * 64, "b" * 64, "c" * 64
CLAIMS: dict[str, tuple[str, ...]] = {case_id: ("c1",) for case_id in CASE_IDS}
ARMS: tuple[Arm, ...] = ("production", "baseline")
WARMUP_IDS: tuple[CaseId, ...] = ("R01", "N01", "Q01")
DEFAULT_ATTEMPT = UUID(int=2)
PASS = DeterministicAssertion(id="exact", passed=True)
FAIL = DeterministicAssertion(id="exact", passed=False)


def _question(case_id: CaseId = "Q01") -> GoldCase:
    return GoldCase(
        id=case_id,
        kind="qa",
        request=QuestionRequest(
            mode="question", attempt_id=UUID(int=1), question="Synthetic query"
        ),
        letter=None,
        expected_extract=None,
        expected_status="answered",
        expected_tool=None,
        expected_tool_args=None,
        expected_tool_result=None,
        required_claims=[Claim(id="c1", text="Synthetic claim")],
        evidence_ids=[CHUNK_A],
        relevance={CHUNK_A: 2, CHUNK_B: 1},
    )


def _notice() -> GoldCase:
    facts = NoticeFacts(
        scope=Scope(ordinary="unknown", period_start="unknown"),
        effective_on=None,
        served_on=None,
        service_method="unknown",
    )
    result = ToolResult(
        tool="notice_deadline_check",
        status="cannot_determine",
        checks=[
            CheckResult(id=check, status="unknown", reason="missing_fact", rule_ids=[])
            for check in ("scope", "supported_year", "period_start", "notice")
        ],
        deemed_served_on=None,
        notice_days=None,
        earliest_notice_on=None,
        latest_deemed_service_on=None,
        latest_dispatch_on=None,
        earliest_spacing_on=None,
        guideline_percent=None,
        cap_cents_exact=None,
        rule_ids=[],
    )
    return GoldCase(
        id="N01",
        kind="notice",
        request=NoticeRequest(mode="notice", attempt_id=UUID(int=1), confirmed=True, facts=facts),
        letter=None,
        expected_extract=None,
        expected_status="cannot_determine",
        expected_tool="notice_deadline_check",
        expected_tool_args=facts,
        expected_tool_result=result,
        required_claims=[Claim(id="c1", text="Synthetic claim")],
        evidence_ids=[CHUNK_A],
        relevance={},
    )


def _response(
    attempt_id: UUID = DEFAULT_ATTEMPT,
    *,
    status: Literal["answered", "refused"] = "answered",
) -> AskResponse:
    return AskResponse(
        attempt_id=attempt_id,
        trace_id=UUID(int=attempt_id.int + 1000),
        status=status,
        answer="Synthetic response",
        statements=[Statement(id="s1", text="Synthetic statement", citation_ids=[])]
        if status == "answered"
        else [],
        tool_result=None,
        citations=[],
        snapshot_date=DAY,
        disclaimer=disclaimer_for(DAY),
    )


def _judge(
    *,
    claim: Literal["met", "missing", "contradicted"] = "met",
    factual: Literal["supported", "unsupported", "contradicted"] = "supported",
    citation: Literal["supported", "unsupported", "not_applicable"] = "supported",
    false_pass: bool = False,
) -> JudgeResult:
    return JudgeResult(
        required_claims=[JudgeClaimResult(id="c1", result=claim)],
        statements=[JudgeStatementResult(id="s1", factual=factual, citation_support=citation)],
        false_pass=false_pass,
        policy_violations=[],
    )


def _row(case_id: CaseId, arm: Arm, repeat: int, index: int) -> ResultRow:
    response = _response(UUID(int=index))
    return ResultRow(
        run_id=UUID(int=9999),
        case_id=case_id,
        arm=arm,
        repeat=repeat,
        source_sha="1" * 40,
        config_hash="2" * 64,
        corpus_hash="3" * 64,
        gold_hash="4" * 64,
        pricing_hash="5" * 64,
        attempt_id=response.attempt_id,
        trace_ids=[response.trace_id],
        retrieved_ids=[],
        response=response,
        actual_extract=None,
        actual_tool_args=None,
        actual_tool_result=None,
        deterministic_assertions=[PASS],
        judge=_judge(),
        classification="success",
        latency_ms=float((index - 1) % 80 + 1),
        input_tokens=100,
        output_tokens=100,
        serving_cost_usd=Decimal("0.001"),
        judge_cost_usd=Decimal("0.002"),
        usage_complete=True,
    )


def _replace(row: ResultRow, **changes: object) -> ResultRow:
    return ResultRow.model_validate(
        {**{field: getattr(row, field) for field in ResultRow.model_fields}, **changes}
    )


def _collection() -> list[ResultRow]:
    return [
        _row(case_id, arm, repeat, arm_index * 80 + repeat * 16 + position + 1)
        for arm_index, arm in enumerate(ARMS)
        for repeat in range(5)
        for position, case_id in enumerate(CASE_IDS)
    ]


def _warmups() -> list[ResultRow]:
    return [
        _replace(
            _row(case_id, arm, 0, 500 + arm_index * 3 + position), judge=None, judge_cost_usd=None
        )
        for arm_index, arm in enumerate(ARMS)
        for position, case_id in enumerate(WARMUP_IDS)
    ]


def test_hand_calculated_graded_retrieval() -> None:
    result = ranking_score({"strong": 2, "weak": 1}, ["zero", "weak", "strong"])
    assert result.mrr_at_5 == 0.5
    assert result.ndcg_at_5 == pytest.approx((1 / log2(3) + 3 / 2) / (3 + 1 / log2(3)))
    assert ranking_score({"strong": 2, "weak": 1}, ["strong", "weak"]).ndcg_at_5 == 1
    assert ranking_score({"strong": 2, "weak": 1}, ["weak", "strong"]).ndcg_at_5 < 1


def test_ideal_ranking_uses_all_labels_and_only_five_positions() -> None:
    result = ranking_score(
        {str(index): 2 for index in range(7)}, [str(index) for index in range(5)]
    )
    assert result.ndcg_at_5 == 1
    assert ranking_score({"first": 2, "missing": 2}, ["first"]).ndcg_at_5 < 1


@pytest.mark.parametrize("hits", [[], ["irrelevant"]])
def test_no_relevant_hits_contribute_zero(hits: list[str]) -> None:
    assert ranking_score({"relevant": 2}, hits).mrr_at_5 == 0
    assert ranking_score({"relevant": 2}, hits).ndcg_at_5 == 0


@pytest.mark.parametrize("labels", [{}, {"a": 0}, {"a": -1}, {"a": 3}, {"a": True}])
def test_invalid_positive_labels_rejected(labels: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ranking_score(labels, [])


@pytest.mark.parametrize("hits", [["a", "a"], list("abcdef"), [""]])
def test_invalid_rank_lists_rejected(hits: list[str]) -> None:
    with pytest.raises(ValueError):
        ranking_score({"a": 2}, hits)


@pytest.mark.parametrize("labels,hits", [({"missing": 2}, []), ({"a": 2}, ["missing"])])
def test_missing_evidence_rejected(labels: dict[str, int], hits: list[str]) -> None:
    with pytest.raises(ValueError, match="missing evidence"):
        ranking_score(labels, hits, evidence_ids=["a"])


def test_macro_retains_missing_question_and_excludes_fact_cases() -> None:
    result = retrieval_scores(
        [_notice(), _question("Q01"), _question("Q02")],
        {"Q01": [CHUNK_A, CHUNK_B], "N01": [CHUNK_C]},
        evidence_ids=[CHUNK_A, CHUNK_B, CHUNK_C],
    )
    assert result.qa_count == 2
    assert set(result.cases) == {"Q01", "Q02"}
    assert result.mrr_at_5 == result.ndcg_at_5 == 0.5
    baseline = retrieval_scores([_question("Q01"), _question("Q02")], {})
    assert baseline.mrr_at_5 == baseline.ndcg_at_5 == 0


@pytest.mark.parametrize(
    "cases,retrieved",
    [([], {}), ([_notice()], {}), ([_question(), _question()], {}), ([_question()], {"Q06": []})],
)
def test_invalid_macro_inputs_rejected(
    cases: list[GoldCase], retrieved: dict[str, list[str]]
) -> None:
    with pytest.raises(ValueError):
        retrieval_scores(cases, retrieved)


@pytest.mark.parametrize("arm", ["production", "baseline"])
def test_citation_failure_is_separate_from_factual_success(arm: Arm) -> None:
    decision = classify(
        _response(), [PASS], _judge(citation="not_applicable"), expected_claim_ids=["c1"], arm=arm
    )
    assert decision.classification == "success"
    assert decision.evaluation_complete
    assert not decision.hallucinated
    assert decision.citation_failure == (arm == "production")


@pytest.mark.parametrize(
    "judge",
    [None, JudgeResult(required_claims=[], statements=[], false_pass=False, policy_violations=[])],
)
def test_missing_or_incomplete_judge_is_error(judge: JudgeResult | None) -> None:
    decision = classify(_response(), [PASS], judge, expected_claim_ids=["c1"])
    assert decision.classification == "error"
    assert not decision.evaluation_complete
    assert decision.hallucinated is None


@pytest.mark.parametrize(
    "judge",
    [
        _judge(claim="missing"),
        _judge(claim="contradicted"),
        _judge(factual="unsupported"),
        _judge(factual="contradicted"),
        _judge(false_pass=True),
        JudgeResult(
            required_claims=[JudgeClaimResult(id="c1", result="met")],
            statements=[
                JudgeStatementResult(id="s1", factual="supported", citation_support="supported")
            ],
            false_pass=False,
            policy_violations=["S01"],
        ),
    ],
)
def test_semantic_failures_cannot_succeed(judge: JudgeResult) -> None:
    decision = classify(_response(), [PASS], judge, expected_claim_ids=["c1"])
    assert decision.classification == "incorrect"
    assert decision.evaluation_complete


def test_extraction_mismatch_is_complete_incorrect_without_analysis() -> None:
    decision = classify(None, [FAIL], None, expected_claim_ids=["c1"])
    assert decision.classification == "incorrect"
    assert decision.evaluation_complete
    assert (
        classify(_response(), [FAIL], _judge(), expected_claim_ids=["c1"]).classification
        == "incorrect"
    )


def test_refusals_and_service_failures_keep_distinct_classification() -> None:
    refusal = classify(_response(status="refused"), [PASS], None, expected_claim_ids=["c1"])
    assert refusal.classification == "refusal"
    assert refusal.evaluation_complete
    failure = ErrorResponse(
        attempt_id=UUID(int=2),
        trace_id=UUID(int=3),
        error=ErrorDetail(code="provider_error", message="Provider unavailable"),
        snapshot_date=DAY,
        disclaimer=disclaimer_for(DAY),
    )
    assert classify(failure, [], None, expected_claim_ids=[]).classification == "error"
    assert classify(failure, [], None, expected_claim_ids=[]).evaluation_complete
    assert not classify(None, [], None, expected_claim_ids=[]).evaluation_complete
    assert not classify(
        failure, [], None, expected_claim_ids=[], infrastructure_error=True
    ).evaluation_complete


def test_complete_collection_uses_fixed_denominators_and_exact_money() -> None:
    summary = aggregate(_collection(), warmups=_warmups(), expected_claim_ids=CLAIMS)
    assert summary.complete
    assert summary.warmup_cost_usd == Decimal("0.006")
    for arm in ("production", "baseline"):
        metrics = summary.arms[arm]
        assert metrics.attempts == metrics.successes == 80
        assert metrics.success_rate == 1
        assert metrics.tool_attempts == metrics.tool_successes == 50
        assert metrics.qa_attempts == metrics.qa_successes == 30
        assert metrics.success_rate_range == (1, 1)
        assert [item.attempts for item in metrics.repetitions] == [16] * 5
        assert metrics.serving_cost_usd == Decimal("0.080")
        assert metrics.cost_per_100_usd == Decimal("0.100")
        assert metrics.judge_cost_usd == Decimal("0.160")
    assert summary.arms["production"].p50_ms == 40
    assert summary.arms["production"].p95_ms == 76
    assert summary.arms["baseline"].p50_ms is None


def test_failed_extraction_retains_latency_cost_and_attempt_denominators() -> None:
    rows = _collection()
    rows[0] = _replace(
        rows[0],
        response=None,
        deterministic_assertions=[FAIL],
        judge=None,
        classification="incorrect",
        judge_cost_usd=None,
        latency_ms=9999.0,
    )
    summary = aggregate(rows, warmups=_warmups(), expected_claim_ids=CLAIMS)
    assert summary.complete
    production = summary.arms["production"]
    assert production.successes == 79
    assert production.success_rate == 79 / 80
    assert production.success_rate_range == (15 / 16, 1)
    assert production.tool_success_rate == 49 / 50
    assert production.answered_outputs == 79
    assert production.p50_ms == 41
    assert production.p95_ms == 77
    assert 9999 in production.latency_samples_ms
    assert production.cost_per_100_usd == Decimal("0.1")


def test_incomplete_plan_has_no_complete_collection_headline() -> None:
    summary = aggregate(_collection()[:-1], warmups=_warmups())
    assert not summary.complete
    assert "incomplete_plan" in summary.reasons
    assert summary.arms["baseline"].success_rate is None
    assert summary.arms["baseline"].cost_per_100_usd is None
    assert summary.arms["baseline"].success_rate_range is None
    empty = aggregate([])
    assert empty.arms["production"].hallucination_rate is None
    assert empty.arms["production"].p95_ms is None
    assert empty.warmup_cost_usd is None


def test_missing_usage_cannot_produce_zero_cost_claim() -> None:
    rows = _collection()
    rows[0] = _replace(
        rows[0], input_tokens=None, output_tokens=None, serving_cost_usd=None, usage_complete=False
    )
    summary = aggregate(rows, warmups=_warmups())
    assert summary.arms["production"].cost_per_100_usd is None
    assert summary.arms["production"].serving_cost_usd is None
    assert summary.arms["baseline"].cost_per_100_usd == Decimal("0.1")


def test_unjudged_answer_prevents_complete_evaluation_and_hallucination_claim() -> None:
    rows = _collection()
    rows[0] = _replace(rows[0], judge=None, classification="error", judge_cost_usd=None)
    summary = aggregate(rows, warmups=_warmups())
    assert not summary.complete
    assert summary.arms["production"].errors == 1
    assert summary.arms["production"].hallucinated_outputs is None
    assert summary.arms["production"].hallucination_rate is None
    assert summary.arms["production"].success_rate is None
    assert summary.arms["production"].judge_cost_usd is None


def test_hallucination_denominator_is_answered_outputs_only() -> None:
    rows = _collection()
    rows[0] = _replace(rows[0], judge=_judge(factual="unsupported"), classification="incorrect")
    rows[1] = _replace(
        rows[1],
        response=_response(rows[1].attempt_id, status="refused"),
        judge=None,
        judge_cost_usd=None,
        classification="refusal",
    )
    production = aggregate(rows, warmups=_warmups()).arms["production"]
    assert production.answered_outputs == 79
    assert production.hallucinated_outputs == 1
    assert production.hallucination_rate == 1 / 79
    assert production.refusals == 1


def test_forged_success_classification_is_incomplete() -> None:
    rows = _collection()
    rows[0] = _replace(rows[0], judge=_judge(false_pass=True))
    summary = aggregate(rows, warmups=_warmups())
    assert not summary.complete
    assert "classification_mismatch" in summary.reasons
    assert summary.arms["production"].success_rate is None


@pytest.mark.parametrize(
    "change", ["duplicate_key", "duplicate_attempt", "mixed_run", "mixed_source"]
)
def test_duplicate_or_mixed_collection_rejected(change: str) -> None:
    rows = _collection()
    if change == "duplicate_key":
        rows.append(rows[0])
    elif change == "duplicate_attempt":
        rows[1] = _replace(rows[1], attempt_id=rows[0].attempt_id)
    elif change == "mixed_run":
        rows[0] = _replace(rows[0], run_id=UUID(int=99))
    else:
        rows[0] = _replace(rows[0], source_sha="f" * 40)
    with pytest.raises(ValueError):
        aggregate(rows)


def test_warmups_cannot_be_scored_or_silently_lost() -> None:
    rows, warmups = _collection(), _warmups()
    assert not aggregate(rows).complete
    assert not aggregate(rows, warmups=warmups[:-1]).complete
    warmups[0] = _replace(warmups[0], judge=_judge(), judge_cost_usd=Decimal("0.01"))
    with pytest.raises(ValueError, match="warm-ups cannot"):
        aggregate(rows, warmups=warmups)


@pytest.mark.parametrize(
    "samples,percentile",
    [
        ([], 0.5),
        ([1.0], 0.0),
        ([1.0], 1.1),
        ([float("nan")], 0.5),
        ([float("inf")], 0.5),
        ([-1.0], 0.5),
    ],
)
def test_invalid_nearest_rank_inputs_rejected(samples: Sequence[float], percentile: float) -> None:
    with pytest.raises(ValueError):
        nearest_rank(samples, percentile)


def test_six_question_macro_discloses_six_as_denominator() -> None:
    questions: tuple[CaseId, ...] = ("Q01", "Q02", "Q03", "Q04", "Q05", "Q06")
    summary = retrieval_scores(
        [_question(case_id) for case_id in questions], {"Q01": [CHUNK_A, CHUNK_B]}
    )
    assert summary.qa_count == 6
    assert summary.mrr_at_5 == summary.ndcg_at_5 == 1 / 6


def test_all_refusals_have_no_hallucination_denominator_but_keep_latency() -> None:
    rows = [
        _replace(
            row,
            response=_response(row.attempt_id, status="refused"),
            judge=None,
            judge_cost_usd=None,
            classification="refusal",
        )
        for row in _collection()
    ]
    summary = aggregate(rows, warmups=_warmups())
    assert summary.complete
    for arm in ARMS:
        metrics = summary.arms[arm]
        assert metrics.success_rate == 0
        assert metrics.refusals == 80
        assert metrics.answered_outputs == 0
        assert metrics.hallucination_rate is None
        assert metrics.judge_cost_usd == 0
        assert metrics.cost_per_100_usd == Decimal("0.1")
    assert summary.arms["production"].p95_ms == 76


def test_aggregate_reports_production_citation_failures_separately() -> None:
    rows = [_replace(row, judge=_judge(citation="not_applicable")) for row in _collection()]
    summary = aggregate(rows, warmups=_warmups())
    assert summary.complete
    assert summary.arms["production"].successes == 80
    assert summary.arms["production"].citation_failures == 80
    assert summary.arms["baseline"].citation_failures == 0
    assert summary.arms["baseline"].hallucination_rate == 0


def test_actual_gold_claim_map_can_reject_otherwise_well_formed_judgments() -> None:
    wrong_claims = {**CLAIMS, "R01": ("another_claim",)}
    summary = aggregate(_collection(), warmups=_warmups(), expected_claim_ids=wrong_claims)
    assert not summary.complete
    assert "invalid_judge" in summary.reasons
    with pytest.raises(ValueError, match="claim map"):
        aggregate(_collection(), warmups=_warmups(), expected_claim_ids={})


def test_duplicate_assertions_do_not_become_task_success() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        classify(_response(), [PASS, PASS], _judge(), expected_claim_ids=["c1"])


def test_missing_warmup_usage_prevents_warmup_cost_claim() -> None:
    warmups = _warmups()
    warmups[0] = _replace(
        warmups[0],
        input_tokens=None,
        output_tokens=None,
        serving_cost_usd=None,
        usage_complete=False,
    )
    summary = aggregate(_collection(), warmups=warmups)
    assert summary.warmup_cost_usd is None
    assert summary.arms["production"].cost_per_100_usd == Decimal("0.1")
