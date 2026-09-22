"""Strict, explicitly loaded synthetic security fixtures; no execution or scoring."""

from hashlib import sha256
from importlib.resources import files
from typing import Annotated, Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from rent_navigator.corpus import Corpus
from rent_navigator.models import AskRequest, ExtractRequest, Sha256, StrictModel

SecurityCaseId = Literal["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"]
SecurityAssertion = Literal[
    "question_tool_execution_forbidden",
    "citation_provenance_enforced",
    "generated_schema_strict",
    "server_disclaimer_preserved",
    "confirmed_arguments_immutable",
    "retrieved_attack_isolated",
    "pii_redacted_before_provider",
    "metadata_content_free",
    "non_pii_values_preserved",
    "excluded_scope_preserved",
    "refusal_text_server_owned",
]
_Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]


class PolicyClaim(StrictModel):
    id: _Nonempty
    text: _Nonempty


class SecurityCase(StrictModel):
    id: SecurityCaseId
    mode: Literal["extract", "question", "notice", "rent"]
    request: ExtractRequest | AskRequest
    injected_retrieved_text: Annotated[str, Field(min_length=1, max_length=4000)] | None
    expected_deterministic_assertions: Annotated[list[SecurityAssertion], Field(min_length=1)]
    required_policy_claims: list[PolicyClaim]
    canonical_evidence_ids: list[Sha256]

    @field_validator("expected_deterministic_assertions", "canonical_evidence_ids")
    @classmethod
    def sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("assertion tags and evidence IDs must be sorted and unique")
        return value

    @model_validator(mode="after")
    def consistent_case(self) -> Self:
        request_mode = "extract" if isinstance(self.request, ExtractRequest) else self.request.mode
        if self.mode != request_mode:
            raise ValueError("security case mode must match its request")
        if self.mode == "extract" and self.injected_retrieved_text is not None:
            raise ValueError("extraction cannot receive retrieved text")
        claim_ids = [claim.id for claim in self.required_policy_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("policy claim IDs must be unique within a case")
        if self.id == "S06":
            if self.mode != "extract" or self.required_policy_claims or self.canonical_evidence_ids:
                raise ValueError("S06 requires extraction with empty claims and evidence")
        elif not self.required_policy_claims or not self.canonical_evidence_ids:
            raise ValueError("policy cases require claims and canonical evidence")
        return self


def _resource_bytes() -> bytes:
    return files("rent_navigator").joinpath("security_cases.jsonl").read_bytes()


def security_cases_hash() -> str:
    """Hash the exact packaged fixture bytes, independently of runtime configuration."""
    return sha256(_resource_bytes()).hexdigest()


def load_security_cases(*, corpus: Corpus) -> tuple[SecurityCase, ...]:
    """Load the eight ordered fixtures and resolve their canonical evidence offline."""
    cases = tuple(SecurityCase.model_validate_json(line) for line in _resource_bytes().splitlines())
    if tuple(case.id for case in cases) != get_args(SecurityCaseId):
        raise ValueError("security cases must contain exactly S01 through S08 in order")
    canonical_ids = {chunk.id for chunk in corpus.chunks}
    if any(not set(case.canonical_evidence_ids) <= canonical_ids for case in cases):
        raise ValueError("security case evidence does not resolve in the supplied corpus")
    return cases
