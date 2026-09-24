"""Fixed rule references and ranked seeds, with separate context provenance."""

from typing import Annotated, Final, Literal, get_args

from pydantic import Field, field_validator

from rent_navigator.corpus import Chunk, Corpus, Rule
from rent_navigator.index import NOTICE_QUERY as NOTICE_QUERY
from rent_navigator.index import RENT_QUERY as RENT_QUERY
from rent_navigator.models import AskRequest, RuleId, Sha256, StrictModel

Arm = Literal["production", "baseline"]
Mode = Literal["question", "notice", "rent"]
CONTEXT_POLICY: Final = {
    "version": 1,
    "foundation": "union of all validated rule evidence IDs in corpus.chunks order",
    "production_question": "ranked seeds union foundation, unique in corpus.chunks order",
    "production_facts": "original ordered ranked seeds only; no foundation",
    "baseline": "no retrieval, foundation, initial passages or sidecar",
    "seeds": "unchanged original query policy; maximum five unique canonical chunks",
    "citations": "initial context plus actually executed tool rules; baseline rules unchanged",
    "sidecar": "untrusted text; no rank, evidence ID, foundation or citation permission",
    "provenance": "separate required ordered retrieved, foundation and initial context IDs",
}


class ContextProvenance(StrictModel):
    retrieved_evidence_ids: Annotated[list[Sha256], Field(max_length=5)]
    foundation_evidence_ids: list[Sha256]
    initial_context_evidence_ids: list[Sha256]

    @field_validator(
        "retrieved_evidence_ids", "foundation_evidence_ids", "initial_context_evidence_ids"
    )
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Context provenance IDs must be unique")
        return value


def foundation_ids(corpus: Corpus) -> tuple[str, ...]:
    """Use fixed validated rule references; this does not execute any rule."""
    rules = tuple(Rule.model_validate(rule) for rule in corpus.rules)
    if {rule.id for rule in rules} != set(get_args(RuleId)) or len(rules) != len(get_args(RuleId)):
        raise ValueError("Foundation requires every validated rule exactly once")
    identifiers = {identifier for rule in rules for identifier in rule.evidence_ids}
    if len({chunk.id for chunk in corpus.chunks}) != len(corpus.chunks):
        raise ValueError("Canonical corpus chunk IDs must be unique")
    for identifier in identifiers:
        Chunk.model_validate(corpus.chunk(identifier))
    return tuple(chunk.id for chunk in corpus.chunks if chunk.id in identifiers)


def context_provenance(
    mode: Mode, arm: Arm, ordered_seed_ids: tuple[str, ...], corpus: Corpus
) -> ContextProvenance:
    if mode not in ("question", "notice", "rent") or arm not in ("production", "baseline"):
        raise ValueError("Unsupported context configuration")
    provenance = ContextProvenance(
        retrieved_evidence_ids=list(ordered_seed_ids),
        foundation_evidence_ids=[],
        initial_context_evidence_ids=[],
    )
    if arm == "baseline":
        if ordered_seed_ids:
            raise ValueError("Baseline cannot contain retrieval observations")
        return provenance
    for identifier in provenance.retrieved_evidence_ids:
        Chunk.model_validate(corpus.chunk(identifier))
    foundation = foundation_ids(corpus) if mode == "question" else ()
    allowed = set(ordered_seed_ids) | set(foundation)
    initial = (
        tuple(chunk.id for chunk in corpus.chunks if chunk.id in allowed)
        if mode == "question"
        else ordered_seed_ids
    )
    return ContextProvenance(
        retrieved_evidence_ids=list(ordered_seed_ids),
        foundation_evidence_ids=list(foundation),
        initial_context_evidence_ids=list(initial),
    )


def query_for_request(request: AskRequest) -> str:
    if request.mode == "question":
        return request.question
    return NOTICE_QUERY if request.mode == "notice" else RENT_QUERY
