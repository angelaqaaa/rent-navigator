"""Run the bounded offline computed-value scan over explicitly bound retained inputs."""

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from rent_navigator.eval.targeted_scan import load_inputs, rule_manifest, scan_rows


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def render_answers(report: dict[str, Any]) -> str:
    parts = [
        "# Run-3 targeted scan candidates\n\n",
        "Flags are lexical candidates, not errors or audited corrections. All owner reads and "
        "audited verdicts remain pending/null. Only retained judge-successful N04/R06 answers "
        "are scanned, across both arms and all five repetitions.\n\n",
        "Serving source: " + report["source_bindings"]["serving_source_commit"] + ".\n\n",
        f"Eligible answers: {report['eligible_answers']}; statements examined: "
        f"{report['statements_examined']}; flagged answers: {report['flagged_answers']}; "
        f"flagged statements: {report['flagged_statements']}.\n\n",
    ]
    for answer in report["answers"]:
        audit_id = answer["audit_id"]
        text = html.escape(answer["response"]["answer"]).replace(chr(0x2014), "&#8212;")
        parts.extend(
            [
                f'\n<a id="{audit_id}"></a>\n\n## {audit_id}\n\n',
                f"Attempt: {answer['attempt_id']}; original results.jsonl line "
                f"{answer['source_jsonl_line']}. Owner read: pending.\n\n",
                "### Full original answer\n\n<pre>" + text + "</pre>\n\n",
                "### Exact candidate statements, fields, values and spans\n\n~~~json\n",
                _json([hit for hit in report["hits"] if hit["audit_id"] == audit_id]),
                "~~~\n\n### Actual retained tool arguments and result, "
                "and original judge\n\n~~~json\n",
                _json(answer),
                "~~~\n",
            ]
        )
    if not report["answers"]:
        parts.append("No candidates matched this finite rule. This is not a semantic clearance.\n")
    return "".join(parts)


def render_rule() -> str:
    return (
        "# Deterministic computed-value availability rule\n\n"
        "Scan only existing classify task-success answers for N04/R06, using the actual gold "
        "claim IDs, both arms and all five repetitions. Exclude warmups. Inspect every original "
        "statement independently. Its citations, answer text and judge grades "
        "are never changed.\n\n"
        "A candidate requires a listed field alias directly joined to an availability predicate "
        "as subject, predicate object or adjective. A coordinated list may name several fields. "
        "Each matched field is checked against that same field in row.actual_tool_result; null "
        "never flags and zero is non-null. Gold expected results/status and owner corrections "
        "are not value authorities or eligibility overrides.\n\n"
        "The machine rule below enumerates every alias, relation and guard. Specific latest-date "
        "aliases precede contained shorter aliases. Whole-sentence punctuation bounds the local "
        "guard: recognised conditional, negated/reporting and non-value complement forms are "
        "excluded. No field is inferred from another field, a generic unknown word, a pronoun, "
        "a date/amount or a different statement. Offsets are original Unicode code points with "
        "exclusive ends. Lexical matches are candidates for the owner, not semantic verdicts.\n\n"
        "The finite aliases cannot exhaust English. Ambiguous bare deadline/amount/earliest "
        "increase date and unlisted paraphrases are not matched. Unusual quotations/negation "
        "can still produce false positives or misses. Zero hits would not establish semantic "
        "correctness. The owner reads every flagged full answer before any audited count.\n\n"
        "Serving source and all input hashes remain distinct from the scan implementation "
        "file hashes in scan.json. No calibration, serving configuration or original evidence "
        "is rebound to this audit-code commit. Output contains only flagged answer material, "
        "coverage and binding metadata, never raw provider messages or owner-record copies.\n\n"
        "~~~json\n" + _json(rule_manifest()) + "~~~\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-dir", type=Path, required=True)
    parser.add_argument("--owner-record-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--results-sha256", required=True)
    parser.add_argument("--record-inventory-sha256", required=True)
    parser.add_argument("--gold-dir", type=Path, default=Path("eval"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows, claims, bindings = load_inputs(
            args.collection_dir,
            args.owner_record_dir,
            source_commit=args.source_commit,
            manifest_sha256=args.manifest_sha256,
            results_sha256=args.results_sha256,
            record_inventory_sha256=args.record_inventory_sha256,
            gold_dir=args.gold_dir,
        )
        from rent_navigator.eval import targeted_scan

        bindings["audit_implementation_files_sha256"] = {
            "src/rent_navigator/eval/targeted_scan.py": hashlib.sha256(
                Path(targeted_scan.__file__).read_bytes()
            ).hexdigest(),
            "scripts/scan_computed_fields.py": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        }
        report = scan_rows(rows, claims, bindings)
        files = {
            "RULE.md": render_rule(),
            "rule.json": _json(rule_manifest()),
            "scan.json": _json(report),
            "flagged-answers.md": render_answers(report),
        }
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n"
            for name, text in sorted(files.items())
        )
        args.output_dir.mkdir(parents=True, exist_ok=False)
        for name, text in files.items():
            with (args.output_dir / name).open("x") as stream:
                stream.write(text)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Targeted scan failed input/output validation; source content suppressed.\n")
    print(
        json.dumps(
            {
                name: report[name]
                for name in (
                    "rule_hash",
                    "eligible_answers",
                    "statements_examined",
                    "field_hits",
                    "flagged_statements",
                    "flagged_answers",
                )
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
