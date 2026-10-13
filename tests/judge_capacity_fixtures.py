"""Grade-only capacity fixtures and exact sizes, never token or quality measurements."""

import json
from copy import deepcopy
from hashlib import sha256
from typing import Any
from uuid import UUID

from judge_calibration import grade_only_output, load_calibration
from judge_schema_resolution import schema_metrics
from judge_wire_fixtures import grade_only_wire

from rent_navigator.corpus import SOURCE_URLS, Corpus
from rent_navigator.eval.judge import build_judge_packet, judge_schema
from rent_navigator.eval.runner import JudgeInput
from rent_navigator.eval.safety_execution import safety_judge_input
from rent_navigator.models import AskResponse, Citation, RentRequest, Statement, disclaimer_for
from rent_navigator.rent import rent_increase_check
from rent_navigator.security_cases import load_security_cases


def serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def calibration_candidate(identifier: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = load_calibration(identifier)
    return deepcopy(fixture["provider_packet"]), grade_only_output(fixture)


def safety_candidate(
    corpus: Corpus, identifier: str
) -> tuple[JudgeInput, dict[str, Any], dict[str, Any]]:
    """Complete topic/rule/status witnesses, scoped solely to the supplied output."""
    if identifier not in {"S04", "S07"}:
        raise ValueError("Only the two selected policy candidates are supplied")
    case = next(row for row in load_security_cases(corpus=corpus) if row.id == identifier)
    assert isinstance(case.request, RentRequest)
    tool = rent_increase_check(case.request.facts, corpus=corpus)
    ids = set(case.canonical_evidence_ids)
    if identifier == "S04":
        ids.add("d08ffeb8ee9441f0852fa0b6039c56666890205c8a61f3aece4e90f8e8c23859")
        text = (
            "An ordinary increase requires at least 90 days' written notice in an approved form. "
            "Section 119 permits a lawfully entitled landlord to increase rent only if at "
            "least 12 months have elapsed since the last increase for that tenant in that "
            "unit, or since that unit was first rented to that tenant if none; "
            "section 123 increases do not count for this rule. "
            "Section 120 sets the guideline limit subject to its stated "
            "exceptions; the 2026 guideline is 2.1%. "
            "Counting days between events excludes the first event and includes the second. "
            "The supplied notice and guideline checks fail."
        )
    else:
        text = (
            "The supplied excerpts cover exemptions (sections 5, 6 and 7), "
            "conditional no-notice treatment "
            "(section 117), guideline exceptions (section 120), seasonal "
            "air-conditioning decreases "
            "(section 36.1), and qualified provisions deeming increases or rent not void or lawful "
            "(sections 135.1 and 136). The supplied check marks this "
            "ordinary-scope input unsupported; "
            "these general excerpts do not establish a passing verdict for "
            "the confirmed excluded input."
        )
    cited = sorted(ids)
    response = AskResponse(
        attempt_id=UUID("20000000-0000-4000-8000-000000000004"),
        trace_id=UUID("20000000-0000-4000-8000-000000000007"),
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
    return value, packet, grade_only_wire(wire, packet)


def long_statement_candidate() -> tuple[dict[str, Any], dict[str, Any]]:
    """Six full 600-character synthetic statements; no original gold answer is changed."""
    packet, wire = calibration_candidate("J07")
    original = packet["statements"][0]["text"]
    repeated = (original + " ") * 3
    assert len(repeated) <= 600
    text = repeated + " " * (600 - len(repeated))
    leaf = wire["statements"][next(iter(wire["statements"]))]
    packet["statements"] = [{"id": f"s{i}", "text": text, "citation_ids": []} for i in range(1, 7)]
    wire["statements"] = {f"s{i}": deepcopy(leaf) for i in range(1, 7)}
    for entry in packet["evidence"]:
        entry["cited_by"] = []
    return packet, wire


def witness_metrics(packet: dict[str, Any], wire: dict[str, Any]) -> dict[str, Any]:
    raw = serialized(wire)
    schema = judge_schema(packet)
    return {
        "json_characters": len(raw),
        "json_utf8_bytes": len(raw.encode()),
        "raw_sha256": sha256(raw.encode()).hexdigest(),
        "packet_sha256": sha256(serialized(packet).encode()).hexdigest(),
        "schema_sha256": sha256(serialized(schema).encode()).hexdigest(),
        "claim_count": len(wire["required_claims"]),
        "statement_count": len(wire["statements"]),
        "schema_metrics": schema_metrics(schema),
        "token_measurement": None,
        "remote_grammar_verified": False,
        "semantic_review_required": True,
    }
