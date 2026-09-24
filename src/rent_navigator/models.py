"""Public wire contracts for confirmed facts and structured responses."""

import re
from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from anthropic.types import ToolParam
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    WithJsonSchema,
    field_validator,
    model_validator,
)

_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _iso_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or re.fullmatch(_DATE_PATTERN, value) is None:
        raise ValueError("date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def _canonical_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or re.fullmatch(_UUID_PATTERN, value) is None:
        raise ValueError("UUID must use canonical lowercase hyphenated form")
    return UUID(value)


def _confirmed(value: object) -> Literal[True]:
    if value is not True:
        raise ValueError("facts require explicit boolean confirmation")
    return True


ISODate = Annotated[
    date,
    BeforeValidator(_iso_date),
    WithJsonSchema({"type": "string", "format": "date", "pattern": _DATE_PATTERN}),
]
CanonicalUUID = Annotated[
    UUID,
    BeforeValidator(_canonical_uuid),
    WithJsonSchema({"type": "string", "format": "uuid", "pattern": _UUID_PATTERN}),
]
SourceCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveCents = Annotated[int, Field(strict=True, gt=0)]
Confirmed = Annotated[Literal[True], BeforeValidator(_confirmed)]
ScopeState = Literal["confirmed", "excluded", "unknown"]
ToolName = Literal["notice_deadline_check", "rent_increase_check"]
CheckId = Literal[
    "scope", "supported_year", "period_start", "notice", "spacing", "guideline", "form"
]
CheckStatus = Literal["pass", "fail", "unknown", "not_applicable"]
CheckReason = Literal[
    "satisfied",
    "violated",
    "missing_fact",
    "excluded_scope",
    "out_of_year_range",
    "exempt",
    "rounding_uncertain",
]
RuleId = Literal[
    "notice.90_days",
    "notice.mail_5_days",
    "spacing.12_months",
    "guideline.2026",
    "guideline.2027",
    "exemption.s6_1",
    "calendar.s89",
    "form.N1",
    "form.N2",
    "scope.ordinary",
]
ToolStatus = Literal[
    "unsupported", "cannot_determine", "fails_checked_rules", "passes_checked_rules"
]
RefusalReason = Literal["needs_confirmation", "out_of_scope", "insufficient_evidence"]
ErrorCode = Literal[
    "invalid_request",
    "body_too_large",
    "rate_limited",
    "busy",
    "budget_exhausted",
    "stale_corpus",
    "provider_error",
    "invalid_generated_output",
    "tool_protocol_error",
    "deadline_exceeded",
]
DecimalString = Annotated[
    str, Field(pattern=r"^(?:0|-?[1-9][0-9]*)(?:\.[0-9]*[1-9])?$|^-0\.[0-9]*[1-9]$")
]

ERROR_HTTP_STATUSES: dict[ErrorCode, int] = {
    "invalid_request": 422,
    "body_too_large": 413,
    "rate_limited": 429,
    "busy": 429,
    "budget_exhausted": 429,
    "stale_corpus": 503,
    "provider_error": 502,
    "invalid_generated_output": 502,
    "tool_protocol_error": 502,
    "deadline_exceeded": 504,
}

DISCLAIMER_TEMPLATE = (
    "Independent project; not affiliated with the Government of Ontario or the "
    "Landlord and Tenant Board. General legal information, not legal advice. "
    "Rules as of {snapshot_date}; results depend on confirmed facts. For advice, "
    "consult a licensed Ontario lawyer or paralegal."
)


def disclaimer_for(snapshot_date: date) -> str:
    return DISCLAIMER_TEMPLATE.format(snapshot_date=snapshot_date.isoformat())


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExtractRequest(StrictModel):
    attempt_id: CanonicalUUID
    letter: Annotated[str, Field(min_length=1, max_length=4000)]


class Extraction(StrictModel):
    current_cents: PositiveCents | None
    proposed_cents: PositiveCents | None
    effective_on: ISODate | None


class Scope(StrictModel):
    ordinary: ScopeState
    period_start: ScopeState


class NoticeFacts(StrictModel):
    scope: Scope
    effective_on: ISODate | None
    served_on: ISODate | None
    service_method: Literal["hand", "mail", "unknown"]


class LastIncrease(StrictModel):
    state: Literal["known", "none", "unknown"]
    date: ISODate | None

    @model_validator(mode="after")
    def date_matches_state(self) -> Self:
        if (self.state == "known") != (self.date is not None):
            raise ValueError("date is required exactly when last increase is known")
        return self


class RentFacts(NoticeFacts):
    current_cents: PositiveCents | None
    proposed_cents: PositiveCents | None
    tenancy_start: ISODate | None
    last_increase: LastIncrease
    guideline_status: Literal["controlled", "exempt_s6_1", "unknown"]
    form: Literal["N1", "N2", "other", "unknown"]

    @model_validator(mode="after")
    def last_increase_not_before_tenancy(self) -> Self:
        if (
            self.tenancy_start is not None
            and self.last_increase.date is not None
            and self.last_increase.date < self.tenancy_start
        ):
            raise ValueError("known last increase precedes known tenancy start")
        return self


class QuestionRequest(StrictModel):
    mode: Literal["question"]
    attempt_id: CanonicalUUID
    question: Annotated[str, Field(min_length=1, max_length=1500)]


class NoticeRequest(StrictModel):
    mode: Literal["notice"]
    attempt_id: CanonicalUUID
    confirmed: Confirmed
    facts: NoticeFacts


class RentRequest(StrictModel):
    mode: Literal["rent"]
    attempt_id: CanonicalUUID
    confirmed: Confirmed
    facts: RentFacts


AskRequest = Annotated[QuestionRequest | NoticeRequest | RentRequest, Field(discriminator="mode")]
ASK_REQUEST_ADAPTER: TypeAdapter[AskRequest] = TypeAdapter(AskRequest)


class CheckResult(StrictModel):
    id: CheckId
    status: CheckStatus
    reason: CheckReason
    rule_ids: list[RuleId]

    @field_validator("rule_ids")
    @classmethod
    def sorted_unique_rules(cls, value: list[RuleId]) -> list[RuleId]:
        if value != sorted(set(value)):
            raise ValueError("rule IDs must be lexically sorted and unique")
        return value


class ToolResult(StrictModel):
    tool: ToolName
    status: ToolStatus
    checks: list[CheckResult]
    deemed_served_on: ISODate | None
    notice_days: int | None
    earliest_notice_on: ISODate | None
    latest_deemed_service_on: ISODate | None
    latest_dispatch_on: ISODate | None
    earliest_spacing_on: ISODate | None
    guideline_percent: Literal["2.1", "1.9"] | None
    cap_cents_exact: DecimalString | None
    rule_ids: list[RuleId]

    @model_validator(mode="after")
    def ordered_checks_and_rule_union(self) -> Self:
        expected = ["scope", "supported_year", "period_start", "notice"]
        if self.tool == "rent_increase_check":
            expected.extend(["spacing", "guideline", "form"])
        elif any(
            value is not None
            for value in (
                self.earliest_spacing_on,
                self.guideline_percent,
                self.cap_cents_exact,
            )
        ):
            raise ValueError("notice results must have null rent-only derived fields")
        if [check.id for check in self.checks] != expected:
            raise ValueError("checks must contain each required check in fixed order")
        if self.rule_ids != sorted(
            {rule_id for check in self.checks for rule_id in check.rule_ids}
        ):
            raise ValueError("rule IDs must be the sorted unique union of check rules")
        return self


class Statement(StrictModel):
    id: Literal["s1", "s2", "s3", "s4", "s5", "s6"]
    text: Annotated[str, Field(min_length=1, max_length=600)]
    citation_ids: list[str]


def _validate_statement_sequence(statements: list[Statement]) -> None:
    if [statement.id for statement in statements] != [
        f"s{index}" for index in range(1, len(statements) + 1)
    ]:
        raise ValueError("statement IDs must be sequential starting with s1")


class GeneratedResult(StrictModel):
    kind: Literal["answer", "refusal"]
    refusal_reason: RefusalReason | None
    statements: Annotated[list[Statement], Field(max_length=6)]

    @model_validator(mode="after")
    def answer_or_refusal(self) -> Self:
        _validate_statement_sequence(self.statements)
        if self.kind == "answer":
            if not self.statements or self.refusal_reason is not None:
                raise ValueError("answer requires statements and null refusal reason")
        elif self.statements or self.refusal_reason is None:
            raise ValueError("refusal requires no statements and a refusal reason")
        return self


class Citation(StrictModel):
    id: Sha256
    url: str
    heading: str
    snapshot_date: ISODate


class AskResponse(StrictModel):
    attempt_id: CanonicalUUID
    trace_id: CanonicalUUID
    status: Literal["answered", "refused"]
    answer: str
    statements: Annotated[list[Statement], Field(max_length=6)]
    tool_result: ToolResult | None
    citations: list[Citation]
    snapshot_date: ISODate
    disclaimer: str

    @model_validator(mode="after")
    def response_structure(self) -> Self:
        _validate_statement_sequence(self.statements)
        if (self.status == "answered") != bool(self.statements):
            raise ValueError("only answered responses contain statements")
        citation_ids = [citation.id for citation in self.citations]
        if citation_ids != sorted(set(citation_ids)):
            raise ValueError("citations must be sorted by unique chunk ID")
        if self.disclaimer != disclaimer_for(self.snapshot_date):
            raise ValueError("disclaimer must match the fixed snapshot disclaimer")
        return self


class ExtractResponse(StrictModel):
    attempt_id: CanonicalUUID
    trace_id: CanonicalUUID
    extraction: Extraction
    snapshot_date: ISODate
    disclaimer: str

    @model_validator(mode="after")
    def fixed_disclaimer(self) -> Self:
        if self.disclaimer != disclaimer_for(self.snapshot_date):
            raise ValueError("disclaimer must match the fixed snapshot disclaimer")
        return self


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str


class ErrorResponse(StrictModel):
    attempt_id: CanonicalUUID | None
    trace_id: CanonicalUUID
    error: ErrorDetail
    snapshot_date: ISODate
    disclaimer: str

    @model_validator(mode="after")
    def fixed_disclaimer(self) -> Self:
        if self.disclaimer != disclaimer_for(self.snapshot_date):
            raise ValueError("disclaimer must match the fixed snapshot disclaimer")
        return self


def provider_tool_definitions() -> list[ToolParam]:
    """Publish argument schemas directly from the confirmed-fact models."""
    return [
        {
            "name": "notice_deadline_check",
            "input_schema": NoticeFacts.model_json_schema(),
        },
        {
            "name": "rent_increase_check",
            "input_schema": RentFacts.model_json_schema(),
        },
    ]
