"""Offline loading and sizing for proposed, non-scored calibration packets."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from rent_navigator.corpus import SOURCE_URLS, Corpus
from rent_navigator.eval.judge import JUDGE_SYSTEM, judge_schema
from rent_navigator.eval.models import Claim
from rent_navigator.eval.runner import JudgeInput
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.models import AskResponse, Citation, Statement, ToolResult, disclaimer_for

CALIBRATION_DIRECTORY = Path(__file__).parent / "fixtures" / "judge_calibration"
CALIBRATION_IDS = tuple(f"J{number:02}" for number in range(1, 14))


def compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_calibration(identifier: str) -> dict[str, Any]:
    if identifier not in CALIBRATION_IDS:
        raise ValueError("Unknown calibration fixture")
    value: dict[str, Any] = json.loads((CALIBRATION_DIRECTORY / f"{identifier}.json").read_text())
    return value


def calibration_input(packet: dict[str, Any], corpus: Corpus) -> JudgeInput:
    """Reconstruct the real packet seam through the unchanged public JSON boundary."""
    statements = [Statement.model_validate_json(json.dumps(row)) for row in packet["statements"]]
    cited_ids = sorted({identifier for item in statements for identifier in item.citation_ids})
    actual = (
        ToolResult.model_validate_json(json.dumps(packet["observed_tool_result"]))
        if packet["observed_tool_result"] is not None
        else None
    )
    response = AskResponse(
        attempt_id=UUID("11111111-1111-4111-8111-111111111111"),
        trace_id=UUID("22222222-2222-4222-8222-222222222222"),
        status="answered",
        answer="\n".join(item.text for item in statements),
        statements=statements,
        tool_result=actual,
        citations=[
            Citation(
                id=identifier,
                url=SOURCE_URLS[corpus.chunk(identifier).source_id],
                heading=corpus.chunk(identifier).heading,
                snapshot_date=corpus.snapshot_date,
            )
            for identifier in cited_ids
        ],
        snapshot_date=corpus.snapshot_date,
        disclaimer=disclaimer_for(corpus.snapshot_date),
    )
    response = AskResponse.model_validate_json(response.model_dump_json())
    return JudgeInput(
        required_claims=tuple(
            Claim.model_validate_json(json.dumps(row)) for row in packet["required_claims"]
        ),
        evidence=tuple(corpus.chunk(row["id"]) for row in packet["evidence"] if row["canonical"]),
        expected_tool_result=(
            ToolResult.model_validate_json(json.dumps(packet["expected_tool_result"]))
            if packet["expected_tool_result"] is not None
            else None
        ),
        response=response,
        actual_tool_result=actual,
        cited_evidence=tuple(corpus.chunk(identifier) for identifier in cited_ids),
    )


def tool_leaf_paths(packet: dict[str, Any]) -> list[str]:
    """Enumerate RFC6901 scalar/null paths independently for structural sizing."""
    result: list[str] = []

    def visit(value: object, pointer: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                visit(child, f"{pointer}/{escaped}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}")
        else:
            result.append(pointer)

    for root in ("expected_tool_result", "observed_tool_result"):
        if packet[root] is not None:
            visit(packet[root], f"/{root}")
    return result


def stress_audit_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """A valid shape stress payload, deliberately not a semantic judgment."""
    packet = fixture["provider_packet"]
    output: dict[str, Any] = deepcopy(fixture["representative_audit_output"])
    all_ids = [row["id"] for row in packet["statements"]]
    all_refs = [row["reference"] for row in packet["evidence"]]
    pointers = tool_leaf_paths(packet)
    for leaf in output["required_claims"].values():
        leaf["answer_statement_ids"] = all_ids
        leaf["coverage_basis"] = "x" * 360
    for statement in packet["statements"]:
        leaf = output["statements"][statement["id"]]
        leaf["factual_basis"] = {
            "summary": "x" * 320,
            "evidence_refs": all_refs,
            "tool_fields": pointers,
        }
        if statement["citation_ids"]:
            leaf["citation_basis"] = {
                "summary": "x" * 320,
                "evidence_refs": [
                    row["reference"]
                    for row in packet["evidence"]
                    if row["id"] in statement["citation_ids"]
                ],
                "tool_fields": pointers,
            }
    return output


def calibration_sizes(fixture: dict[str, Any]) -> dict[str, int]:
    packet = fixture["provider_packet"]
    schema = judge_schema(packet)
    payload = {
        "model": JUDGE_MODEL,
        "system": JUDGE_SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": json.dumps(packet, sort_keys=True, separators=(",", ":")),
            }
        ],
        "thinking": {"type": "disabled"},
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    stress_packet = deepcopy(packet)
    for statement in stress_packet["statements"]:
        statement["text"] = "x" * 600
    stress_payload = deepcopy(payload)
    stress_payload["messages"] = [
        {
            "role": "user",
            "content": json.dumps(stress_packet, sort_keys=True, separators=(",", ":")),
        }
    ]
    inputs = {
        "packet": packet,
        "schema": schema,
        "count_payload_without_timeout": payload,
        "structural_stress_count_payload_without_timeout": stress_payload,
        "representative_output": fixture["representative_audit_output"],
        "structural_stress_output": stress_audit_output(fixture),
    }
    return {
        f"{name}_{unit}": len(text.encode()) if unit == "utf8_bytes" else len(text)
        for name, value in inputs.items()
        for text in (compact_json(value),)
        for unit in ("characters", "utf8_bytes")
    }
