"""Frozen wire7 structural budgets with the rejected wire6 schema as a control."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from judge_calibration import CALIBRATION_IDS, load_calibration
from judge_schema_resolution import INITIAL_GRADE_LIMITS, lint_grade_schema, schema_metrics

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import build_judge_packet, build_smoke_input, judge_schema


@pytest.mark.parametrize("packet_id", [*CALIBRATION_IDS, "R04"])
def test_every_fixed_shape_meets_initial_grade_schema_limits(packet_id: str) -> None:
    if packet_id == "R04":
        corpus = load_corpus()
        case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
        packet = build_judge_packet(build_smoke_input(case, corpus))
    else:
        packet = load_calibration(packet_id)["provider_packet"]
    metrics = lint_grade_schema(judge_schema(packet))
    assert metrics["unions"] == metrics["optional"] == 0
    assert metrics["depth"] <= 5
    assert metrics["objects"] == 7


def test_rejected_positive_control_exceeds_all_three_budgets() -> None:
    schema = json.loads(Path("tests/fixtures/rejected_wire6_J01_schema.json").read_bytes())
    metrics = schema_metrics(schema)
    assert metrics["bytes"] == 14071
    assert metrics["enum_members"] == 94
    assert metrics["depth"] == 14
    assert all(metrics[key] > limit for key, limit in INITIAL_GRADE_LIMITS.items())
    with pytest.raises(ValueError, match="depth, enum_members, bytes"):
        lint_grade_schema(schema)


@pytest.mark.parametrize(
    "keyword",
    [
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "multipleOf",
        "uniqueItems",
        "maxItems",
        "pattern",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "unsupportedKeyword",
    ],
)
def test_lint_rejects_unsupported_keyword(keyword: str) -> None:
    schema = judge_schema(load_calibration("J01")["provider_packet"])
    schema["$defs"]["claim_result"][keyword] = 1
    with pytest.raises(ValueError):
        lint_grade_schema(schema)


@pytest.mark.parametrize(
    "mutation", ["union", "optional", "min_items", "all_of", "open", "foreign_ref", "missing_ref"]
)
def test_lint_rejects_forbidden_shapes_and_references(mutation: str) -> None:
    schema = deepcopy(judge_schema(load_calibration("J01")["provider_packet"]))
    if mutation == "union":
        schema["$defs"]["claim_result"]["type"] = ["string", "null"]
    elif mutation == "optional":
        schema["required"].pop()
    elif mutation == "min_items":
        schema["properties"]["statements"]["minItems"] = 2
    elif mutation == "all_of":
        schema["properties"]["false_pass"] = {"allOf": [{"$ref": "#/$defs/claim_result"}]}
    elif mutation == "open":
        schema["$defs"]["claim"].pop("additionalProperties")
    else:
        schema["properties"]["statements"]["properties"]["s1"]["$ref"] = (
            "https://example.invalid/schema" if mutation == "foreign_ref" else "#/$defs/absent"
        )
    with pytest.raises(ValueError):
        lint_grade_schema(schema)
