"""Current grade-only controls preserve the fixed packet inputs and label domains."""

from copy import deepcopy
from typing import Any
from uuid import UUID

from judge_calibration import grade_only_output, load_calibration

from rent_navigator.corpus import SOURCE_URLS, Corpus
from rent_navigator.eval.judge import build_judge_packet
from rent_navigator.eval.runner import JudgeInput
from rent_navigator.eval.safety_execution import safety_judge_input
from rent_navigator.models import AskResponse, Citation, RentRequest, Statement, disclaimer_for
from rent_navigator.rent import rent_increase_check
from rent_navigator.security_cases import load_security_cases


def calibration_witness(identifier: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = load_calibration(identifier)
    return deepcopy(fixture["provider_packet"]), grade_only_output(fixture)


def safety_witness(
    corpus: Corpus, identifier: str
) -> tuple[JudgeInput, dict[str, Any], dict[str, Any]]:
    """Scoped output invariant; multi-source statements are representation controls."""
    case = next(row for row in load_security_cases(corpus=corpus) if row.id == identifier)
    assert isinstance(case.request, RentRequest)
    tool = rent_increase_check(case.request.facts, corpus=corpus)
    ids = set(case.canonical_evidence_ids)
    if identifier == "S04":
        ids.add("d08ffeb8ee9441f0852fa0b6039c56666890205c8a61f3aece4e90f8e8c23859")
    cited = sorted(ids)
    text = (
        "The supplied check reports notice and guideline failures."
        if identifier == "S04"
        else "The supplied check reports unsupported scope."
    )
    response = AskResponse(
        attempt_id=UUID("10000000-0000-4000-8000-000000000004"),
        trace_id=UUID("10000000-0000-4000-8000-000000000007"),
        status="answered",
        answer=text,
        statements=[Statement(id="s1", text=text, citation_ids=cited)],
        tool_result=tool,
        citations=[
            Citation(
                id=chunk.id,
                url=SOURCE_URLS[chunk.source_id],
                heading=chunk.heading,
                snapshot_date=corpus.snapshot_date,
            )
            for chunk in (corpus.chunk(i) for i in cited)
        ],
        snapshot_date=corpus.snapshot_date,
        disclaimer=disclaimer_for(corpus.snapshot_date),
    )
    value = safety_judge_input(case, response, tool, corpus)
    packet = build_judge_packet(value)
    wire: dict[str, Any] = {
        "required_claims": [{"id": claim.id, "result": "met"} for claim in value.required_claims],
        "statements": [{"id": "s1", "factual": "supported", "citation_support": "supported"}],
        "false_pass": False,
        "policy_violations": [],
    }
    return value, packet, wire
