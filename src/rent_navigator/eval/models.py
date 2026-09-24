"""Strict synthetic evaluation records; no provider calls or scoring."""

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self, get_args

from pydantic import BeforeValidator, ConfigDict, Field, field_serializer, model_validator

from rent_navigator.models import (
    AskRequest,
    AskResponse,
    CanonicalUUID,
    ErrorResponse,
    Extraction,
    NoticeFacts,
    NoticeRequest,
    RentFacts,
    RentRequest,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolName,
    ToolResult,
    ToolStatus,
)
from rent_navigator.security_cases import SecurityCaseId
from rent_navigator.trace import Milliseconds, NonNegativeInt, Usd

CaseId = Literal[
    "R01",
    "R02",
    "R03",
    "R04",
    "R05",
    "R06",
    "N01",
    "N02",
    "N03",
    "N04",
    "Q01",
    "Q02",
    "Q03",
    "Q04",
    "Q05",
    "Q06",
]
CASE_IDS: tuple[CaseId, ...] = get_args(CaseId)
Arm = Literal["production", "baseline"]
Classification = Literal["success", "incorrect", "refusal", "error"]
Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]
RelevanceGrade = Annotated[int, Field(strict=True, ge=1, le=2)]


class EvaluationModel(StrictModel):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")


class Claim(EvaluationModel):
    id: Nonempty
    text: Nonempty


class GoldCase(EvaluationModel):
    id: CaseId
    kind: Literal["rent", "notice", "qa"]
    request: AskRequest
    letter: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")] | None
    expected_extract: Extraction | None
    expected_status: ToolStatus | Literal["answered"]
    expected_tool: ToolName | None
    expected_tool_args: NoticeFacts | RentFacts | None
    expected_tool_result: ToolResult | None
    required_claims: Annotated[list[Claim], Field(min_length=1, max_length=4)]
    evidence_ids: Annotated[list[Sha256], Field(min_length=1)]
    relevance: dict[Sha256, RelevanceGrade]

    @model_validator(mode="after")
    def consistent_case(self) -> Self:
        mode = "question" if self.kind == "qa" else self.kind
        if (
            self.request.mode != mode
            or self.id[0] != {"rent": "R", "notice": "N", "qa": "Q"}[self.kind]
        ):
            raise ValueError("gold ID, kind and request mode must agree")
        if self.kind == "qa":
            if (
                self.expected_status != "answered"
                or self.expected_tool is not None
                or self.expected_tool_args is not None
                or self.expected_tool_result is not None
            ):
                raise ValueError("question gold requires answered status and null tool fields")
            if 2 not in self.relevance.values():
                raise ValueError("question gold requires at least one grade-2 chunk")
        else:
            if not isinstance(self.request, NoticeRequest | RentRequest):
                raise ValueError("fact gold requires a confirmed fact request")
            tool = "rent_increase_check" if self.kind == "rent" else "notice_deadline_check"
            if (
                self.expected_tool != tool
                or self.expected_tool_args != self.request.facts
                or self.expected_tool_result is None
                or self.expected_tool_result.tool != tool
                or self.expected_status != self.expected_tool_result.status
            ):
                raise ValueError("fact gold tool, arguments, result and status must agree")
            if self.relevance:
                raise ValueError("fact gold cannot contain retrieval relevance labels")
        if self.id in {"R02", "R04"}:
            if (
                self.letter is None
                or self.expected_extract is None
                or not isinstance(self.request, RentRequest)
            ):
                raise ValueError("R02 and R04 require a synthetic letter and expected extraction")
            facts = self.request.facts
            if (
                self.expected_extract.current_cents != facts.current_cents
                or self.expected_extract.proposed_cents != facts.proposed_cents
                or self.expected_extract.effective_on != facts.effective_on
            ):
                raise ValueError(
                    "expected extraction must equal the approved request's three fields"
                )
        elif self.letter is not None or self.expected_extract is not None:
            raise ValueError("only R02 and R04 may contain a letter or expected extraction")
        claim_ids = [claim.id for claim in self.required_claims]
        claim_texts = [claim.text for claim in self.required_claims]
        if len(set(claim_ids)) != len(claim_ids) or len(set(claim_texts)) != len(claim_texts):
            raise ValueError("required claim IDs and texts must be unique within a case")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("gold evidence IDs must be unique")
        return self


class JudgeClaimResult(EvaluationModel):
    id: Nonempty
    result: Literal["met", "missing", "contradicted"]


class JudgeStatementResult(EvaluationModel):
    id: Nonempty
    factual: Literal["supported", "unsupported", "contradicted"]
    citation_support: Literal["supported", "unsupported", "not_applicable"]


class JudgeResult(EvaluationModel):
    required_claims: list[JudgeClaimResult]
    statements: list[JudgeStatementResult]
    false_pass: bool
    policy_violations: list[SecurityCaseId]

    @model_validator(mode="after")
    def unique_entries(self) -> Self:
        for entries in (
            [claim.id for claim in self.required_claims],
            [statement.id for statement in self.statements],
            self.policy_violations,
        ):
            if len(set(entries)) != len(entries):
                raise ValueError("judge entries must have unique identifiers")
        return self

    def validate_coverage(self, claim_ids: Iterable[str], statement_ids: Iterable[str]) -> Self:
        """Require exact once-only coverage of this attempt's claims and statements."""
        expected_claims, expected_statements = tuple(claim_ids), tuple(statement_ids)
        if (
            len(set(expected_claims)) != len(expected_claims)
            or len(set(expected_statements)) != len(expected_statements)
            or {claim.id for claim in self.required_claims} != set(expected_claims)
            or {statement.id for statement in self.statements} != set(expected_statements)
        ):
            raise ValueError("judge coverage must match expected claim and actual statement IDs")
        return self


class DeterministicAssertion(EvaluationModel):
    id: Nonempty
    passed: bool


class ResultRow(EvaluationModel):
    run_id: CanonicalUUID
    case_id: CaseId
    arm: Arm
    repeat: Annotated[int, Field(ge=0, le=4)]
    source_sha: SourceCommit
    config_hash: Sha256
    corpus_hash: Sha256
    gold_hash: Sha256
    pricing_hash: Sha256
    attempt_id: CanonicalUUID
    trace_ids: list[CanonicalUUID]
    retrieved_ids: Annotated[list[Sha256], Field(max_length=5)]
    foundation_evidence_ids: list[Sha256]
    initial_context_evidence_ids: list[Sha256]
    response: AskResponse | ErrorResponse | None
    actual_extract: Extraction | None
    actual_tool_args: NoticeFacts | RentFacts | None
    actual_tool_result: ToolResult | None
    deterministic_assertions: list[DeterministicAssertion]
    judge: JudgeResult | None
    classification: Classification
    latency_ms: Milliseconds
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None
    serving_cost_usd: Usd | None
    judge_cost_usd: Usd | None
    usage_complete: bool

    @model_validator(mode="after")
    def unique_identifiers(self) -> Self:
        assertion_ids = [assertion.id for assertion in self.deterministic_assertions]
        if (
            len(set(assertion_ids)) != len(assertion_ids)
            or len(set(self.trace_ids)) != len(self.trace_ids)
            or len(set(self.retrieved_ids)) != len(self.retrieved_ids)
            or len(set(self.foundation_evidence_ids)) != len(self.foundation_evidence_ids)
            or len(set(self.initial_context_evidence_ids)) != len(self.initial_context_evidence_ids)
        ):
            raise ValueError("result assertion, trace and retrieval identifiers must be unique")
        complete = self.input_tokens is not None and self.output_tokens is not None
        if self.usage_complete != complete or complete != (self.serving_cost_usd is not None):
            raise ValueError("serving usage completeness and actual serving cost must agree")
        return self

    @field_serializer("serving_cost_usd", "judge_cost_usd")
    def serialize_cost(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


def _schema_one(value: object) -> Literal[1]:
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be integer 1")
    return 1


def _utc_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        if (
            re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value)
            is None
        ):
            raise ValueError("approval timestamp must be a UTC ISO timestamp")
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("approval timestamp must be a UTC ISO timestamp")
    return value


class GoldApproval(EvaluationModel):
    schema_version: Annotated[Literal[1], BeforeValidator(_schema_one)]
    status: Literal["draft", "approved"]
    gold_sha256: Sha256
    corpus_hash: Sha256
    approved_by: Literal["Angela"] | None
    approved_at_utc: Annotated[datetime, BeforeValidator(_utc_timestamp)] | None

    @model_validator(mode="after")
    def approval_state(self) -> Self:
        if self.status == "draft":
            if self.approved_by is not None or self.approved_at_utc is not None:
                raise ValueError("draft gold requires null approval fields")
        elif self.approved_by is None or self.approved_at_utc is None:
            raise ValueError("approved gold requires owner and UTC approval timestamp")
        return self


class Activation(EvaluationModel):
    schema_version: Annotated[Literal[1], BeforeValidator(_schema_one)]
    baseline_phase: Literal["pending", "active"]
