"""Synthetic structural witnesses; these fixtures do not measure semantic accuracy."""

from copy import deepcopy
from typing import Any

POLICY_LIMITS = [
    "execution_history_not_supplied",
    "tool_arguments_not_supplied",
    "attack_origin_not_supplied",
    "disclaimer_not_supplied",
    "sensitive_sentinels_not_supplied",
]


def source_anchor(entry: dict[str, Any]) -> dict[str, str]:
    return {"reference": entry["reference"], "quote": entry["text"]}


def answer_anchor(statement: dict[str, Any]) -> dict[str, str]:
    return {"statement_id": statement["id"], "quote": statement["text"]}


def scalar_paths(value: object, root: str) -> list[str]:
    """Test-only exact scalar/null pointer enumeration, including RFC6901 escaping."""
    if isinstance(value, dict):
        return [
            pointer
            for key in sorted(value)
            for pointer in scalar_paths(
                value[key], root + "/" + key.replace("~", "~0").replace("/", "~1")
            )
        ]
    if isinstance(value, list):
        return [
            pointer
            for index, child in enumerate(value)
            for pointer in scalar_paths(child, root + "/" + str(index))
        ]
    return [root]


def add_audit_bases(wire: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Add structural wire5 witnesses, preserving supplied grades, keys and IDs.

    Grade-compatible observations are synthetic inputs, not inferred judgments.
    Callers test inconsistent observations by mutating the returned wire explicitly.
    Unknown IDs and malformed grades remain unmodified so the decoder rejects them.
    """
    data = deepcopy(wire)
    statement_ids = [statement["id"] for statement in packet["statements"]]
    statements = {statement["id"]: statement for statement in packet["statements"]}
    claims = {claim["id"]: claim for claim in packet["required_claims"]}
    evidence = packet["evidence"]
    fact_paths = (
        scalar_paths(packet["approved_facts"]["facts"], "/approved_facts/facts")
        if packet.get("approved_facts") is not None
        else []
    )
    tool_paths = [
        pointer
        for root in ("expected_tool_result", "observed_tool_result")
        if packet.get(root) is not None
        for pointer in scalar_paths(packet[root], "/" + root)
    ]
    policy = packet.get("safety_context", {}).get("policy_claims") is True
    for identifier, leaf in data["required_claims"].items():
        if policy:
            observation = {
                "met": "no_observed_conflict",
                "missing": "insufficient_packet_evidence",
                "contradicted": "observed_conflict",
            }.get(
                leaf.get("result") if isinstance(leaf.get("result"), str) else "",
                "no_observed_conflict",
            )
            additions = dict(
                kind="behavior_invariant",
                reviewed_statement_ids=statement_ids,
                observation=observation,
                answer_anchors=[answer_anchor(packet["statements"][0])]
                if observation == "observed_conflict"
                else [],
                context_fields=[],
                limits=list(POLICY_LIMITS),
                basis="Synthetic output-invariant fixture; execution history is not supplied.",
            )
        else:
            observation = {
                "met": "explicit",
                "missing": "omitted",
                "contradicted": "conflicting",
            }.get(leaf.get("result") if isinstance(leaf.get("result"), str) else "", "explicit")
            additions = dict(
                kind="ordinary_coverage",
                parts=[
                    {
                        "text": claims.get(identifier, {}).get("text", "Unbound fixture claim"),
                        "observation": observation,
                        "answer_anchors": [answer_anchor(packet["statements"][0])]
                        if observation != "omitted"
                        else [],
                        "basis": "Synthetic coverage fixture; no semantic conclusion is measured.",
                    }
                ],
            )
        for key, value in additions.items():
            leaf.setdefault(key, value)
    for identifier, leaf in data["statements"].items():
        statement = statements.get(identifier)
        citations = statement["citation_ids"] if statement is not None else []
        own = [entry for entry in evidence if entry["id"] in citations]
        factual = {
            "observation": {
                "supported": "explicit",
                "unsupported": "insufficient",
                "contradicted": "conflicting",
            }.get(leaf.get("factual") if isinstance(leaf.get("factual"), str) else "", "explicit"),
            "source_anchors": [source_anchor(evidence[0])] if evidence else [],
            "tool_fields": tool_paths[:1] if not evidence and not fact_paths else [],
            "fact_fields": fact_paths[:1] if not evidence else [],
            "basis": "Synthetic factual fixture for boundary validation.",
        }
        citation = (
            {
                "observation": "explicit"
                if leaf.get("citation_support") == "supported"
                else "insufficient",
                "source_anchors": [source_anchor(own[0])] if own else [],
                "tool_fields": [],
                "fact_fields": [],
                "basis": "Synthetic own-citation fixture for boundary validation.",
            }
            if citations
            else {
                "observation": "not_applicable",
                "source_anchors": [],
                "tool_fields": [],
                "fact_fields": [],
                "basis": "No citation supplied.",
            }
        )
        leaf.setdefault(
            "parts",
            [
                {
                    "text": statement["text"] if statement else "Unbound fixture statement",
                    "factual_audit": factual,
                    "citation_audit": citation,
                }
            ],
        )
    return data
