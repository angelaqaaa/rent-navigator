"""Pure evaluation arithmetic over explicit observations and judgments."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, isfinite, log2

from rent_navigator.eval.models import (
    CASE_IDS,
    Arm,
    Classification,
    DeterministicAssertion,
    GoldCase,
    JudgeResult,
    ResultRow,
)
from rent_navigator.models import AskResponse, ErrorResponse


@dataclass(frozen=True, slots=True)
class RankingScore:
    mrr_at_5: float
    ndcg_at_5: float


@dataclass(frozen=True, slots=True)
class RetrievalScores:
    qa_count: int
    cases: dict[str, RankingScore]
    mrr_at_5: float
    ndcg_at_5: float


def ranking_score(
    relevance: Mapping[str, int],
    retrieved_ids: Sequence[str],
    *,
    evidence_ids: Collection[str] | None = None,
) -> RankingScore:
    """Score a top-five ranked list; unlisted evidence has relevance zero."""
    if not relevance or any(
        type(grade) is not int or grade not in (1, 2) for grade in relevance.values()
    ):
        raise ValueError("relevance requires positive strict integer grades 1 or 2")
    if any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in relevance):
        raise ValueError("relevance IDs must be nonempty strings")
    if any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in retrieved_ids):
        raise ValueError("retrieved IDs must be nonempty strings")
    if len(retrieved_ids) > 5 or len(set(retrieved_ids)) != len(retrieved_ids):
        raise ValueError("retrieval requires at most five unique IDs")
    if evidence_ids is not None:
        known = set(evidence_ids)
        if not set(relevance).issubset(known) or not set(retrieved_ids).issubset(known):
            raise ValueError("ranking references missing evidence")
    reciprocal = next(
        (1.0 / rank for rank, chunk_id in enumerate(retrieved_ids, 1) if chunk_id in relevance),
        0.0,
    )
    dcg = sum(
        (2 ** relevance.get(chunk_id, 0) - 1) / log2(rank + 1)
        for rank, chunk_id in enumerate(retrieved_ids, 1)
    )
    ideal = sum(
        (2**grade - 1) / log2(rank + 1)
        for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:5], 1)
    )
    return RankingScore(reciprocal, dcg / ideal)


def retrieval_scores(
    cases: Sequence[GoldCase],
    retrieved_by_case: Mapping[str, Sequence[str]],
    *,
    evidence_ids: Collection[str] | None = None,
) -> RetrievalScores:
    """Macro-average only Q&A cases, retaining zero for every missing retrieval."""
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case IDs")
    if not set(retrieved_by_case).issubset(ids):
        raise ValueError("retrieval contains unexpected case IDs")
    scores: dict[str, RankingScore] = {
        case.id: ranking_score(
            case.relevance, retrieved_by_case.get(case.id, ()), evidence_ids=evidence_ids
        )
        for case in cases
        if case.kind == "qa"
    }
    if not scores:
        raise ValueError("retrieval metrics require Q&A cases")
    return RetrievalScores(
        qa_count=len(scores),
        cases=scores,
        mrr_at_5=sum(score.mrr_at_5 for score in scores.values()) / len(scores),
        ndcg_at_5=sum(score.ndcg_at_5 for score in scores.values()) / len(scores),
    )


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    classification: Classification
    evaluation_complete: bool
    citation_failure: bool
    hallucinated: bool | None
    reasons: tuple[str, ...]


def classify(
    response: AskResponse | ErrorResponse | None,
    assertions: Sequence[DeterministicAssertion],
    judge: JudgeResult | None,
    *,
    expected_claim_ids: Collection[str],
    arm: Arm = "production",
    infrastructure_error: bool = False,
) -> ClassificationDecision:
    """Separate factual task success from production citation compliance."""
    if arm not in ("production", "baseline"):
        raise ValueError("unknown evaluation arm")
    assertion_ids = [assertion.id for assertion in assertions]
    if len(assertion_ids) != len(set(assertion_ids)):
        raise ValueError("duplicate deterministic assertion IDs")
    if infrastructure_error:
        return ClassificationDecision("error", False, False, None, ("infrastructure_error",))
    if isinstance(response, ErrorResponse):
        return ClassificationDecision("error", True, False, None, ("service_error",))
    mismatch = any(not assertion.passed for assertion in assertions)
    if response is None:
        if mismatch:
            return ClassificationDecision("incorrect", True, False, None, ("exact_mismatch",))
        return ClassificationDecision("error", False, False, None, ("missing_response",))
    if response.status == "refused":
        return ClassificationDecision("refusal", True, False, None, ("refusal",))
    if judge is None:
        return ClassificationDecision("error", False, False, None, ("missing_judge",))
    try:
        judge.validate_coverage(
            expected_claim_ids, (statement.id for statement in response.statements)
        )
    except ValueError:
        return ClassificationDecision("error", False, False, None, ("invalid_judge",))
    hallucinated = any(statement.factual != "supported" for statement in judge.statements)
    citation_failure = arm == "production" and any(
        statement.citation_support != "supported" for statement in judge.statements
    )
    reasons = []
    if mismatch:
        reasons.append("exact_mismatch")
    if any(claim.result != "met" for claim in judge.required_claims):
        reasons.append("required_claim_failure")
    if hallucinated:
        reasons.append("factual_failure")
    if judge.false_pass:
        reasons.append("false_pass")
    if judge.policy_violations:
        reasons.append("policy_violation")
    return ClassificationDecision(
        "incorrect" if reasons else "success", True, citation_failure, hallucinated, tuple(reasons)
    )


@dataclass(frozen=True, slots=True)
class RepetitionMetrics:
    repeat: int
    attempts: int
    successes: int
    success_rate: float | None


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    complete: bool
    attempts: int
    expected_attempts: int
    successes: int
    success_rate: float | None
    repetitions: tuple[RepetitionMetrics, ...]
    success_rate_range: tuple[float, float] | None
    tool_attempts: int
    tool_successes: int
    qa_attempts: int
    qa_successes: int
    tool_success_rate: float | None
    qa_success_rate: float | None
    answered_outputs: int
    hallucinated_outputs: int | None
    hallucination_rate: float | None
    citation_failures: int
    refusals: int
    errors: int
    incorrect: int
    latency_samples_ms: tuple[float, ...]
    p50_ms: float | None
    p95_ms: float | None
    serving_cost_usd: Decimal | None
    cost_per_100_usd: Decimal | None
    judge_cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class CollectionMetrics:
    complete: bool
    arms: dict[Arm, ArmMetrics]
    warmup_cost_usd: Decimal | None
    reasons: tuple[str, ...]


def nearest_rank(samples: Sequence[float], percentile: float) -> float:
    """Return the one-based ceil(p*n) order statistic without interpolation."""
    if (
        not samples
        or not 0 < percentile <= 1
        or any(not isfinite(sample) or sample < 0 for sample in samples)
    ):
        raise ValueError("nearest rank requires samples and percentile in (0, 1]")
    return sorted(samples)[ceil(percentile * len(samples)) - 1]


def _serving_total(rows: Sequence[ResultRow]) -> Decimal | None:
    if any(not row.usage_complete or row.serving_cost_usd is None for row in rows):
        return None
    return sum(
        (row.serving_cost_usd for row in rows if row.serving_cost_usd is not None), Decimal(0)
    )


def _judge_total(rows: Sequence[ResultRow]) -> Decimal | None:
    if any(
        isinstance(row.response, AskResponse)
        and row.response.status == "answered"
        and row.judge_cost_usd is None
        for row in rows
    ):
        return None
    return sum((row.judge_cost_usd for row in rows if row.judge_cost_usd is not None), Decimal(0))


def aggregate(
    rows: Sequence[ResultRow],
    *,
    warmups: Sequence[ResultRow] = (),
    expected_claim_ids: Mapping[str, Collection[str]] | None = None,
) -> CollectionMetrics:
    """Summarize measured observations; partial runs have no headline rates.

    Artifact, source and execution-mode acceptance remains a manifest concern.
    Supplying the gold claim map additionally checks the contextual judge coverage.
    """
    keys = [(row.case_id, row.arm, row.repeat) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate measured plan keys")
    if any(row.case_id not in CASE_IDS for row in rows):
        raise ValueError("unexpected measured case")
    all_rows = [*rows, *warmups]
    if len({row.attempt_id for row in all_rows}) != len(all_rows):
        raise ValueError("duplicate attempt IDs")
    identities = {
        (row.run_id, row.source_sha, row.corpus_hash, row.gold_hash, row.pricing_hash)
        for row in all_rows
    }
    if len(identities) > 1:
        raise ValueError("mixed collection identities")
    if expected_claim_ids is not None and not set(CASE_IDS).issubset(expected_claim_ids):
        raise ValueError("expected claim map does not cover the collection")
    if warmups:
        warmup_keys = [(row.case_id, row.arm, row.repeat) for row in warmups]
        expected_warmups = {
            (case_id, arm, 0)
            for case_id in ("R01", "N01", "Q01")
            for arm in ("production", "baseline")
        }
        if len(set(warmup_keys)) != len(warmup_keys) or not set(warmup_keys).issubset(
            expected_warmups
        ):
            raise ValueError("unexpected or duplicate warm-up plan keys")
        if any(row.judge is not None or row.judge_cost_usd is not None for row in warmups):
            raise ValueError("warm-ups cannot have judge results or costs")
    decisions: dict[str, ClassificationDecision] = {}
    reasons: set[str] = set()
    for row in rows:
        claims = (
            expected_claim_ids[row.case_id]
            if expected_claim_ids is not None
            else tuple(claim.id for claim in row.judge.required_claims)
            if row.judge
            else ()
        )
        decision = classify(
            row.response,
            row.deterministic_assertions,
            row.judge,
            expected_claim_ids=claims,
            arm=row.arm,
        )
        decisions[str(row.attempt_id)] = decision
        if decision.classification != row.classification:
            reasons.add("classification_mismatch")
        if not decision.evaluation_complete:
            reasons.update(decision.reasons)
    warmups_complete = len(warmups) == 6
    if not warmups_complete:
        reasons.add("incomplete_warmups")
    arms: dict[Arm, ArmMetrics] = {}
    for arm in ("production", "baseline"):
        arm_rows = [row for row in rows if row.arm == arm]
        planned = {(case_id, repeat) for case_id in CASE_IDS for repeat in range(5)}
        present = {(row.case_id, row.repeat) for row in arm_rows}
        complete = present == planned and all(
            decisions[str(row.attempt_id)].evaluation_complete
            and decisions[str(row.attempt_id)].classification == row.classification
            for row in arm_rows
        )
        if present != planned:
            reasons.add("incomplete_plan")
        successes = sum(row.classification == "success" for row in arm_rows)
        repetitions = tuple(
            RepetitionMetrics(
                repeat=repeat,
                attempts=sum(row.repeat == repeat for row in arm_rows),
                successes=sum(
                    row.repeat == repeat and row.classification == "success" for row in arm_rows
                ),
                success_rate=(
                    sum(
                        row.repeat == repeat and row.classification == "success" for row in arm_rows
                    )
                    / 16
                    if complete
                    else None
                ),
            )
            for repeat in range(5)
        )
        rates = [item.success_rate for item in repetitions if item.success_rate is not None]
        tools = [row for row in arm_rows if not row.case_id.startswith("Q")]
        questions = [row for row in arm_rows if row.case_id.startswith("Q")]
        tool_successes = sum(row.classification == "success" for row in tools)
        qa_successes = sum(row.classification == "success" for row in questions)
        answered = [
            row
            for row in arm_rows
            if isinstance(row.response, AskResponse) and row.response.status == "answered"
        ]
        judgments_known = all(
            decisions[str(row.attempt_id)].hallucinated is not None for row in answered
        )
        hallucinated = (
            sum(decisions[str(row.attempt_id)].hallucinated is True for row in answered)
            if judgments_known
            else None
        )
        latencies = tuple(row.latency_ms for row in arm_rows)
        serving = _serving_total(arm_rows) if complete else None
        arms[arm] = ArmMetrics(
            complete=complete,
            attempts=len(arm_rows),
            expected_attempts=80,
            successes=successes,
            success_rate=successes / 80 if complete else None,
            repetitions=repetitions,
            success_rate_range=(min(rates), max(rates)) if complete else None,
            tool_attempts=len(tools),
            tool_successes=tool_successes,
            qa_attempts=len(questions),
            qa_successes=qa_successes,
            tool_success_rate=tool_successes / 50 if complete else None,
            qa_success_rate=qa_successes / 30 if complete else None,
            answered_outputs=len(answered),
            hallucinated_outputs=hallucinated,
            hallucination_rate=(
                hallucinated / len(answered)
                if complete and answered and hallucinated is not None
                else None
            ),
            citation_failures=sum(
                decisions[str(row.attempt_id)].citation_failure for row in arm_rows
            ),
            refusals=sum(row.classification == "refusal" for row in arm_rows),
            errors=sum(row.classification == "error" for row in arm_rows),
            incorrect=sum(row.classification == "incorrect" for row in arm_rows),
            latency_samples_ms=latencies,
            p50_ms=nearest_rank(latencies, 0.5) if complete and arm == "production" else None,
            p95_ms=nearest_rank(latencies, 0.95) if complete and arm == "production" else None,
            serving_cost_usd=serving,
            cost_per_100_usd=100 * serving / 80 if serving is not None else None,
            judge_cost_usd=_judge_total(arm_rows),
        )
    return CollectionMetrics(
        complete=warmups_complete and all(summary.complete for summary in arms.values()),
        arms=arms,
        warmup_cost_usd=_serving_total(warmups) if warmups_complete else None,
        reasons=tuple(sorted(reasons)),
    )
