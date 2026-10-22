"""Synthetic lexical, eligibility, retained-binding and keyless CLI controls."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from eval_fixtures import TOY_SOURCE_SHA, write_toy_data
from test_eval_models import result_row
from test_eval_real_collection import SyntheticRealCompositionPort, run
from test_models import tool_result

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.models import ResultRow
from rent_navigator.eval.targeted_scan import (
    FIELD_ALIASES,
    load_inputs,
    rule_hash,
    scan_rows,
    scan_statement,
)
from rent_navigator.models import ToolResult, disclaimer_for

VALUES: dict[str, str | int] = {
    "deemed_served_on": "2030-01-02",
    "notice_days": 0,
    "earliest_notice_on": "2030-04-02",
    "latest_deemed_service_on": "2030-01-03",
    "latest_dispatch_on": "2029-12-29",
    "earliest_spacing_on": "2030-06-01",
    "guideline_percent": "2.1",
    "cap_cents_exact": "204200",
}


def observed(**changes: object) -> ToolResult:
    data = {**tool_result(rent=True), **VALUES, **changes}
    return ToolResult.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("field", list(VALUES))
@pytest.mark.parametrize("template", ["The {field} is unknown.", "Cannot determine the {field}."])
def test_every_computed_field_uses_its_own_observed_value(field: str, template: str) -> None:
    result = observed()
    text = template.format(field=field)
    before = result.model_dump_json()
    hits = scan_statement(text, result)
    assert len(hits) == 1 and hits[0].field == field
    assert hits[0].observed_value == VALUES[field]
    assert text[slice(*hits[0].field_span)] == field
    assert text[slice(*hits[0].predicate_span)]
    assert result.model_dump_json() == before
    assert not scan_statement(text, observed(**{field: None}))


@pytest.mark.parametrize(
    "text,field",
    [
        ("The deemed service date remains undetermined.", "deemed_served_on"),
        ("The actual notice interval is unavailable.", "notice_days"),
        ("The earliest effective date cannot be calculated.", "earliest_notice_on"),
        ("The latest deemed service date is not known.", "latest_deemed_service_on"),
        ("Unable to calculate the mailing deadline.", "latest_dispatch_on"),
        ("The earliest spacing date is not available.", "earliest_spacing_on"),
        ("An unknown guideline percentage remains an issue.", "guideline_percent"),
        ("Insufficient information to determine the exact new rent ceiling.", "cap_cents_exact"),
        ("**notice_days**: **unknown**.", "notice_days"),
        ("The cap_cents_exact can't be determined.", "cap_cents_exact"),
    ],
)
def test_general_aliases_and_predicate_forms(text: str, field: str) -> None:
    assert {hit.field for hit in scan_statement(text, observed())} == {field}


@pytest.mark.parametrize(
    "text",
    [
        "The ordinary scope is unknown; the notice_days value is 0.",
        "The notice_days value is 0, but served_on is unknown.",
        "The period-start status is unknown while the rent cap is supplied.",
        "Unknown service method prevents confirming legality, not a computed amount.",
        "The deemed service date is known; scope is unavailable.",
        "The deemed service date is not unknown.",
        "The rent cap is not unavailable.",
        "The notice_days are not undetermined.",
        "We are not unable to determine the rent cap.",
        "It is not true that the rent cap is unknown.",
        "The unknown scope does not mean the notice_days are unknown.",
        "It is wrong to say the guideline percentage is unknown.",
        "If the rent cap is unknown, ask for more facts.",
        "The notice_days would be unknown without the observed inputs.",
        "Cannot determine whether the deemed service date is legally valid.",
        "The notice_days are not known to be legally sufficient.",
        "Cannot confirm the notice period requirements.",
        "Cannot determine the rent cap's applicability.",
        "Unknown guideline rate status is a scope issue.",
        "The earliest next increase date is unknown.",
        "The amount is unknown.",
        "The deadline is undetermined.",
        "It is unknown.",
    ],
)
def test_negation_scope_wrong_value_and_ambiguous_shorthand_do_not_flag(text: str) -> None:
    assert not scan_statement(text, observed())


def test_coordinated_fields_check_each_value_and_specific_date_alias() -> None:
    text = (
        "The deemed service date and notice_days are unknown; latest service date is unavailable."
    )
    hits = scan_statement(text, observed(notice_days=None))
    assert [hit.field for hit in hits] == ["deemed_served_on", "latest_deemed_service_on"]
    assert len({(hit.field, hit.field_span, hit.predicate_span) for hit in hits}) == 2
    assert not scan_statement("The rent cap is unknown.", observed(cap_cents_exact=None))


def row_for(
    case: str, arm: str, repeat: int, *, eligible: bool = True
) -> tuple[int, bytes, ResultRow]:
    data = result_row()
    number = 1 + (0 if case == "N04" else 10) + (0 if arm == "production" else 5) + repeat
    attempt = str(UUID(int=number))
    texts = ["notice_days is unknown.", "Scope is unknown; the deemed service date is supplied."]
    data.update(
        case_id=case,
        arm=arm,
        repeat=repeat,
        attempt_id=attempt,
        actual_tool_result=observed().model_dump(mode="json"),
        classification="success" if eligible else "incorrect",
        deterministic_assertions=[{"id": "synthetic", "passed": True}],
        input_tokens=1000,
        output_tokens=100,
        serving_cost_usd="0.0015",
        judge_cost_usd="0.0007",
        usage_complete=True,
    )
    data["response"] = {
        "attempt_id": attempt,
        "trace_id": data["trace_ids"][0],
        "status": "answered",
        "answer": "\n".join(texts),
        "statements": [
            {"id": f"s{i}", "text": text, "citation_ids": []} for i, text in enumerate(texts, 1)
        ],
        "tool_result": data["actual_tool_result"],
        "citations": [],
        "snapshot_date": "2026-09-21",
        "disclaimer": disclaimer_for(load_corpus().snapshot_date),
    }
    data["judge"] = {
        "required_claims": [{"id": "c1", "result": "met" if eligible else "missing"}],
        "statements": [
            {"id": f"s{i}", "factual": "supported", "citation_support": "not_applicable"}
            for i in (1, 2)
        ],
        "false_pass": False,
        "policy_violations": [],
    }
    raw = (json.dumps(data) + "\n").encode()
    return number, raw, ResultRow.model_validate_json(raw)


def test_all_arms_repetitions_eligibility_statement_coverage_and_order() -> None:
    rows = [
        row_for(case, arm, repeat, eligible=repeat != 4)
        for case in ("N04", "R06")
        for arm in ("production", "baseline")
        for repeat in range(5)
    ]
    before = [row.model_dump_json() for _, _, row in rows]
    report = scan_rows(rows, {"N04": ("c1",), "R06": ("c1",)}, {})
    assert report == scan_rows(list(reversed(rows)), {"N04": ("c1",), "R06": ("c1",)}, {})
    assert report["target_rows"] == 20 and report["eligible_answers"] == 16
    assert report["statements_examined"] == 32 and report["flagged_answers"] == 16
    assert report["field_hits"] == report["flagged_statements"] == 16
    assert all(
        group["target_rows"] == 5 and group["eligible_repeats"] == [0, 1, 2, 3]
        for group in report["coverage"]
    )
    assert all(
        hit["owner_read"] is None and hit["owner_audited_verdict"] is None for hit in report["hits"]
    )
    assert [row.model_dump_json() for _, _, row in rows] == before
    assert all(hit["statement_id"] == "s1" for hit in report["hits"])
    assert set(FIELD_ALIASES) == set(VALUES)
    assert report["rule_hash"] == rule_hash()


def test_expected_only_value_and_citation_material_are_not_authorities() -> None:
    number, raw, row = row_for("R06", "baseline", 0)
    data = json.loads(raw)
    data["actual_tool_result"]["notice_days"] = None
    data["response"]["tool_result"]["notice_days"] = None
    parsed = ResultRow.model_validate_json(json.dumps(data))
    before = parsed.model_dump_json()
    report = scan_rows(
        [(number, json.dumps(data).encode(), parsed)],
        {"R06": ("c1",)},
        {"synthetic_expected_tool_result": {"notice_days": 90}},
    )
    assert report["field_hits"] == 0 and parsed.model_dump_json() == before
    assert not scan_statement("The notice_days are supplied. Citation: unknown scope.", observed())


@pytest.fixture(scope="module")
def bound_collection(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    base = tmp_path_factory.mktemp("synthetic-targeted-bindings")
    corpus = load_corpus()
    gold_dir = write_toy_data(base / "gold", corpus)
    dataset = load_gold(gold_dir, corpus)
    collection = base / "collection"
    run(collection, dataset, corpus, SyntheticRealCompositionPort())
    # A synthetic declaration exercises this input guard, not measurement validity.
    manifest_path = collection / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["collection_complete"] and manifest["evaluation_complete"]
    assert not manifest["reportable"]
    manifest["reportable"] = True
    manifest_path.write_text(json.dumps(manifest))
    (base / "batch-ledger.json").write_text('{"synthetic":true}\n')
    record = base / "owner-record"
    record.mkdir()
    (record / "owner-record.json").write_text('{"synthetic":true}\n')
    digest = sha256((record / "owner-record.json").read_bytes()).hexdigest()
    (record / "SHA256SUMS").write_text(digest + "  owner-record.json\n")
    return {
        "collection": collection,
        "record": record,
        "source_commit": TOY_SOURCE_SHA,
        "manifest_sha256": sha256((collection / "manifest.json").read_bytes()).hexdigest(),
        "results_sha256": sha256((collection / "results.jsonl").read_bytes()).hexdigest(),
        "record_inventory_sha256": sha256((record / "SHA256SUMS").read_bytes()).hexdigest(),
        "gold_dir": gold_dir,
    }


def test_bound_inputs_and_repeatable_keyless_cli(
    bound_collection: dict[str, Any], tmp_path: Path
) -> None:
    rows, claims, bindings = load_inputs(**bound_collection)
    assert len(rows) == 160
    report = scan_rows(rows, claims, bindings)
    assert report["eligible_answers"] == 20 and report["statements_examined"] == 20
    assert report["field_hits"] == 0 and report["owner_reads_complete"] is None
    outputs = []
    for n in (1, 2):
        dest = tmp_path / str(n)
        args = [sys.executable, "-B", "scripts/scan_computed_fields.py", "--output-dir", str(dest)]
        for name, value in bound_collection.items():
            flag = {"collection": "collection-dir", "record": "owner-record-dir"}.get(
                name, name.replace("_", "-")
            )
            args.extend(["--" + flag, str(value)])
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        outputs.append({path.name: path.read_bytes() for path in dest.iterdir()})
        assert str(tmp_path).encode() not in b"".join(outputs[-1].values())
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "key", ["manifest_sha256", "results_sha256", "record_inventory_sha256", "source_commit"]
)
def test_wrong_input_binding_fails_closed(bound_collection: dict[str, Any], key: str) -> None:
    args = dict(bound_collection)
    args[key] = "0" * len(str(args[key]))
    with pytest.raises(ValueError):
        load_inputs(**args)


def test_render_preserves_citations_and_escapes_answer_markup() -> None:
    import importlib.util
    import re

    spec = importlib.util.spec_from_file_location(
        "scan_script_test", "scripts/scan_computed_fields.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    number, raw, _ = row_for("R06", "baseline", 0)
    data = json.loads(raw)
    text = "notice_days is unknown. <script>synthetic_markup()</script>"
    data["response"]["statements"][0]["text"] = text
    data["response"]["answer"] = text + "\n" + data["response"]["statements"][1]["text"]
    row = ResultRow.model_validate_json(json.dumps(data))
    before = row.model_dump_json()
    report = scan_rows(
        [(number, json.dumps(data).encode(), row)],
        {"R06": ("c1",)},
        {"serving_source_commit": TOY_SOURCE_SHA},
    )
    rendered = module.render_answers(report)
    visible = re.sub(r"~~~json\n.*?\n~~~\n", "", rendered, flags=re.S)
    assert "<script>" not in visible and "&lt;script&gt;" in visible
    blocks = [json.loads(value) for value in re.findall(r"~~~json\n(.*?)\n~~~\n", rendered, re.S)]
    assert blocks[-1]["response"] == data["response"]
    assert blocks[-1]["original_judge"] == data["judge"]
    assert report["hits"][0]["statement"] == data["response"]["statements"][0]
    assert row.model_dump_json() == before
