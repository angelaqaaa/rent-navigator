"""Synthetic structural audit bases; these fixtures do not measure semantic accuracy."""

from copy import deepcopy
from typing import Any


def add_audit_bases(wire: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Add packet-bound fixture bases while preserving every supplied grade."""
    data = deepcopy(wire)
    statement_ids = [statement["id"] for statement in packet["statements"]]
    references = {entry["id"]: entry["reference"] for entry in packet["evidence"]}
    statements = {statement["id"]: statement for statement in packet["statements"]}
    for leaf in data["required_claims"].values():
        leaf.update(
            answer_statement_ids=statement_ids[:1],
            coverage_basis="Synthetic coverage fixture; no semantic conclusion is measured.",
        )
    for identifier, leaf in data["statements"].items():
        statement = statements.get(identifier)
        citations = statement["citation_ids"] if statement is not None else []
        own = [references[identifier] for identifier in citations if identifier in references]
        leaf["factual_basis"] = {
            "summary": "Synthetic factual basis for boundary validation.",
            "evidence_refs": list(references.values())[:1],
            "tool_fields": [],
        }
        leaf["citation_basis"] = {
            "summary": "Synthetic own-citation basis for boundary validation."
            if citations
            else "No citation supplied.",
            "evidence_refs": own[:1],
            "tool_fields": [],
        }
    return data
