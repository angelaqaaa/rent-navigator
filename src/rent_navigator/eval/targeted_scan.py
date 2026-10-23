"""Offline lexical candidates for denial of supplied computed values; never a grader."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rent_navigator.eval.metrics import classify
from rent_navigator.eval.models import ResultRow
from rent_navigator.models import AskResponse, ToolResult

RULE_VERSION = "computed-value-availability-v1"
TARGET_CASES = ("N04", "R06")
ARMS = ("production", "baseline")
# Specific date phrases precede their shorter contained phrases.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "latest_deemed_service_on": (
        r"latest_deemed_service_on",
        r"latest (?:deemed )?service date",
        r"latest date (?:for|of) (?:deemed )?service",
    ),
    "latest_dispatch_on": (
        r"latest_dispatch_on",
        r"latest (?:dispatch|mailing|sending) date",
        r"(?:dispatch|mailing|sending) deadline",
        r"latest date to (?:dispatch|mail|send)",
    ),
    "earliest_spacing_on": (
        r"earliest_spacing_on",
        r"(?:earliest )?spacing date",
        r"earliest (?:next )?(?:rent )?increase date (?:under|for) (?:the )?spacing rule",
    ),
    "earliest_notice_on": (
        r"earliest_notice_on",
        r"earliest (?:permitted |possible |allowable )?(?:effective|notice) date",
        r"earliest (?:rent )?increase date (?:under|for) (?:the )?notice rule",
    ),
    "deemed_served_on": (
        r"deemed_served_on",
        r"deemed service date",
        r"deemed served date",
        r"date of deemed service",
        r"date (?:the )?notice (?:is|was) deemed served",
    ),
    "notice_days": (
        r"notice_days",
        r"(?:actual )?notice (?:days|interval|length|period)",
        r"(?:number of |length of )?(?:calendar )?days of notice",
    ),
    "guideline_percent": (
        r"guideline_percent",
        r"guideline (?:percentage|percent|rate)",
    ),
    "cap_cents_exact": (
        r"cap_cents_exact",
        r"(?:exact )?(?:new |total )?rent (?:cap|ceiling|limit)",
        r"(?:maximum|permitted|allowable) (?:new |total )?rent(?: amount)?",
    ),
}
_ACTIVE = r"(?:determine|calculate|compute|establish|identify|ascertain|confirm)"
_PASSIVE = r"(?:determined|calculated|computed|established|identified|ascertained|confirmed)"
_CANNOT = r"(?:cannot|can['\u2019]t|can not)"
_ADVERB = r"(?:(?:currently|presently|still|yet|now)\s+)*"
_WORD = r"(?:unknown|undetermined|unavailable)"
_SUBJECT_PREDICATE = (
    rf"(?:{_WORD}|not\s+{_ADVERB}(?:known|available|{_PASSIVE})|"
    rf"{_CANNOT}\s+{_ADVERB}be\s+{_PASSIVE})"
)
_OBJECT_PREDICATE = (
    rf"(?:{_CANNOT}\s+{_ADVERB}{_ACTIVE}|unable to {_ACTIVE}|"
    rf"(?:not enough|insufficient) (?:information|facts|data) to {_ACTIVE})"
)
_CONDITIONAL = r"\b(?:if|unless|suppos(?:e|ing)|hypothetically|would|could|might)\b"
_NEGATED_FRAME = (
    r"\b(?:(?:does|do|did) not (?:mean|imply|say|claim)|"
    r"not true that|(?:wrong|incorrect|false) to (?:say|claim)|never (?:said|claimed))\b"
)
_NEGATED_PREFIX = r"\b(?:not|never)\s+(?:(?:really|actually|necessarily)\s+)*$"
_NONVALUE_SUFFIX = r"\s+(?:to be|whether|if)\b"
_NONVALUE_FIELD_SUFFIX = (
    r"(?:['\u2019]s)?\s+(?:validity|applicability|compliance|requirements?|eligibility|status)\b"
)
_FIELD = "(?:" + "|".join(pattern for pats in FIELD_ALIASES.values() for pattern in pats) + ")"
_MARKUP = "(?:[*_]{1,2}|" + chr(96) + ")?"
_TOKEN = rf"{_MARKUP}(?<!\w){_FIELD}(?!\w){_MARKUP}"
_GROUP = rf"{_TOKEN}(?:\s*(?:,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+){_TOKEN})*"
_SUBJECT_LINK = rf"\s*(?::|=)?\s*(?:(?:is|are|was|were|remains?|stays?)\s+)?{_ADVERB}"
_RELATIONS = {
    "field_subject": (
        rf"(?P<fields>{_GROUP}){_SUBJECT_LINK}"
        rf"(?P<predicate>{_MARKUP}{_SUBJECT_PREDICATE}\b{_MARKUP})"
    ),
    "predicate_object": rf"\b(?P<predicate>{_OBJECT_PREDICATE})\s+(?:the\s+)?(?P<fields>{_GROUP})",
    "adjective": rf"\b(?P<predicate>{_WORD})\s+(?:the\s+)?(?P<fields>{_GROUP})",
}
_FIELD_RE = re.compile(
    "|".join(
        rf"(?P<f{n}>(?<!\w)(?:{'|'.join(patterns)})(?!\w))"
        for n, patterns in enumerate(FIELD_ALIASES.values())
    ),
    re.IGNORECASE,
)
_FIELDS = tuple(FIELD_ALIASES)
_CLAUSE_END = re.compile(r"[.!?;\n]")


def rule_manifest() -> dict[str, Any]:
    """Return the finite matching policy without any retained answer content."""
    return {
        "version": RULE_VERSION,
        "cases": list(TARGET_CASES),
        "eligibility": "existing classify task-success; actual gold claim IDs; all arms/repeats",
        "authority": "row.actual_tool_result; non-null test, including zero",
        "field_aliases": FIELD_ALIASES,
        "relations": _RELATIONS,
        "conditional_guard": _CONDITIONAL,
        "negated_frame_guard": _NEGATED_FRAME,
        "negated_prefix_guard": _NEGATED_PREFIX,
        "nonvalue_suffix_guard": _NONVALUE_SUFFIX,
        "nonvalue_field_suffix_guard": _NONVALUE_FIELD_SUFFIX,
        "clause_boundaries": _CLAUSE_END.pattern,
        "flags": "IGNORECASE; original Unicode code-point offsets, end exclusive",
        "limits": (
            "Finite English aliases and direct predicates only; no pronoun/coreference, "
            "implicit denial, generic deadline/amount, ambiguous earliest-increase date, "
            "or semantic inference. Coordinated subjects must each name a known field. "
            "Conditional clauses and recognised negated/reporting frames are excluded. "
            "Other quotations, unusual negation and paraphrases can be missed or overflagged. "
            "A candidate is not an error or an audited correction."
        ),
    }


def rule_hash() -> str:
    return hashlib.sha256(
        json.dumps(rule_manifest(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class FieldHit:
    field: str
    observed_value: str | int
    field_span: tuple[int, int]
    predicate_span: tuple[int, int]
    relation_span: tuple[int, int]
    relation: str
    reason: str = "observed_non_null_value_claimed_unavailable"


def scan_statement(text: str, observed: ToolResult) -> tuple[FieldHit, ...]:
    """Find direct availability-denial candidates without modifying text or result."""
    values = observed.model_dump(mode="json")
    found: dict[tuple[str, tuple[int, int], tuple[int, int]], FieldHit] = {}
    boundaries = [match.start() for match in _CLAUSE_END.finditer(text)]
    for relation, pattern in _RELATIONS.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start = max((pos + 1 for pos in boundaries if pos < match.start()), default=0)
            end = min((pos for pos in boundaries if pos >= match.end()), default=len(text))
            prefix, clause = text[start : match.start()], text[start:end]
            if (
                re.search(_CONDITIONAL, clause, re.IGNORECASE)
                or re.search(_NEGATED_FRAME, prefix, re.IGNORECASE)
                or re.search(_NEGATED_PREFIX, prefix, re.IGNORECASE)
                or re.match(_NONVALUE_SUFFIX, text[match.end("predicate") : end], re.IGNORECASE)
                or re.match(_NONVALUE_FIELD_SUFFIX, text[match.end("fields") : end], re.IGNORECASE)
            ):
                continue
            for field_match in _FIELD_RE.finditer(match.group("fields")):
                assert field_match.lastgroup is not None
                field = _FIELDS[int(field_match.lastgroup[1:])]
                value = values[field]
                if value is None:
                    continue
                if type(value) not in (str, int):
                    raise ValueError("Unexpected computed field wire type")
                offset = match.start("fields")
                span = (offset + field_match.start(), offset + field_match.end())
                predicate = match.span("predicate")
                found[(field, span, predicate)] = FieldHit(
                    field, value, span, predicate, match.span(), relation
                )
    return tuple(sorted(found.values(), key=lambda hit: (*hit.field_span, hit.field)))


def scan_rows(
    rows: Sequence[tuple[int, bytes, ResultRow]],
    claim_ids: Mapping[str, tuple[str, ...]],
    bindings: Mapping[str, Any],
) -> dict[str, Any]:
    """Select existing judge-success rows, examine every statement, and retain candidates."""
    target = [item for item in rows if item[2].case_id in TARGET_CASES]
    target.sort(
        key=lambda item: (
            TARGET_CASES.index(item[2].case_id),
            ARMS.index(item[2].arm),
            item[2].repeat,
        )
    )
    seen: set[tuple[str, str, int]] = set()
    eligible: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for case in TARGET_CASES:
        for arm in ARMS:
            coverage.append(
                {
                    "case_id": case,
                    "arm": arm,
                    "target_rows": 0,
                    "eligible_repeats": [],
                    "statements_examined": 0,
                }
            )
    for line_number, raw_line, row in target:
        identity = (row.case_id, row.arm, row.repeat)
        if identity in seen:
            raise ValueError("Duplicate targeted case/arm/repetition")
        seen.add(identity)
        group = next(
            item for item in coverage if item["case_id"] == row.case_id and item["arm"] == row.arm
        )
        group["target_rows"] += 1
        decision = classify(
            row.response,
            row.deterministic_assertions,
            row.judge,
            expected_claim_ids=claim_ids[row.case_id],
            arm=row.arm,
        )
        if decision.classification != row.classification:
            raise ValueError("Retained classification differs from existing classifier")
        if decision.classification != "success":
            continue
        if (
            not isinstance(row.response, AskResponse)
            or row.actual_tool_result is None
            or row.judge is None
        ):
            raise ValueError("Eligible targeted answer lacks observed result or judgment")
        audit_id = f"run-3-{row.case_id.lower()}-{row.arm}-repeat-{row.repeat}"
        raw = json.loads(raw_line)
        common = {
            "audit_id": audit_id,
            "attempt_id": str(row.attempt_id),
            "case_id": row.case_id,
            "arm": row.arm,
            "repeat": row.repeat,
            "source_jsonl_line": line_number,
            "source_line_sha256": hashlib.sha256(raw_line).hexdigest(),
        }
        eligible.append(
            {**common, "statement_ids": [statement.id for statement in row.response.statements]}
        )
        group["eligible_repeats"].append(row.repeat)
        group["statements_examined"] += len(row.response.statements)
        before = len(hits)
        grades = {item["id"]: item for item in raw["judge"]["statements"]}
        for statement in row.response.statements:
            for hit in scan_statement(statement.text, row.actual_tool_result):
                hits.append(
                    {
                        **common,
                        **asdict(hit),
                        "statement_id": statement.id,
                        "statement": next(
                            item
                            for item in raw["response"]["statements"]
                            if item["id"] == statement.id
                        ),
                        "original_judge_grade": grades[statement.id],
                        "source_bindings": dict(bindings),
                        "owner_read": None,
                        "owner_audited_verdict": None,
                    }
                )
        if len(hits) != before:
            answers.append(
                {
                    **common,
                    "response": raw["response"],
                    "actual_tool_args": raw["actual_tool_args"],
                    "actual_tool_result": raw["actual_tool_result"],
                    "original_judge": raw["judge"],
                    "owner_read": None,
                    "owner_audited_verdict": None,
                }
            )
    return {
        "rule_version": RULE_VERSION,
        "rule_hash": rule_hash(),
        "source_bindings": dict(bindings),
        "target_rows": len(target),
        "eligible_answers": len(eligible),
        "statements_examined": sum(group["statements_examined"] for group in coverage),
        "coverage": coverage,
        "eligible": eligible,
        "field_hits": len(hits),
        "flagged_statements": len({(hit["audit_id"], hit["statement_id"]) for hit in hits}),
        "flagged_answers": len(answers),
        "hits": hits,
        "answers": answers,
        "owner_reads_complete": None,
        "audited_verdicts": None,
    }


def _inventory(directory: Path, expected: str) -> dict[str, str]:
    data = (directory / "SHA256SUMS").read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Owner record inventory hash mismatch")
    values: dict[str, str] = {}
    for line in data.decode().splitlines():
        digest, name = line.split("  ", 1)
        if Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid owner record inventory entry")
        if name in values or hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Owner record file hash mismatch")
        values[name] = digest
    if set(values) | {"SHA256SUMS"} != {path.name for path in directory.iterdir()}:
        raise ValueError("Owner record file membership mismatch")
    return {**values, "SHA256SUMS": expected}


def load_inputs(
    collection: Path,
    record: Path,
    *,
    source_commit: str,
    manifest_sha256: str,
    results_sha256: str,
    record_inventory_sha256: str,
    gold_dir: Path,
) -> tuple[list[tuple[int, bytes, ResultRow]], dict[str, tuple[str, ...]], dict[str, Any]]:
    """Verify exact retained bindings; do not replay provider evidence or audit decisions."""
    from rent_navigator.corpus import load_corpus
    from rent_navigator.eval.collection import REAL_ARTIFACT_FILES, RealCollectionManifest
    from rent_navigator.eval.data import load_gold
    from rent_navigator.eval.judge import judge_config_hash
    from rent_navigator.eval.models import CASE_IDS
    from rent_navigator.eval.runner import config_map, protocol_hash, row_config_hash

    raw_manifest = (collection / "manifest.json").read_bytes()
    raw_results = (collection / "results.jsonl").read_bytes()
    if hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
        raise ValueError("Collection manifest hash mismatch")
    if hashlib.sha256(raw_results).hexdigest() != results_sha256:
        raise ValueError("Collection results hash mismatch")
    manifest = RealCollectionManifest.model_validate_json(raw_manifest)
    if (
        manifest.source_sha != source_commit
        or not manifest.collection_complete
        or not manifest.evaluation_complete
        or not manifest.reportable
        or manifest.judge_config_hash != judge_config_hash()
        or manifest.config_map != config_map()
        or manifest.protocol_hash != protocol_hash()
        or set(manifest.files) != set(REAL_ARTIFACT_FILES)
    ):
        raise ValueError("Collection identity or completion mismatch")
    for name, digest in manifest.files.items():
        if hashlib.sha256((collection / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Collection artifact hash mismatch")
    record_files = _inventory(record, record_inventory_sha256)
    ledger_hash = hashlib.sha256((collection.parent / "batch-ledger.json").read_bytes()).hexdigest()
    corpus = load_corpus()
    dataset = load_gold(gold_dir, corpus)
    if (
        dataset.gold_hash != manifest.gold_hash
        or dataset.approval_hash != manifest.approval_hash
        or corpus.corpus_hash != manifest.corpus_hash
    ):
        raise ValueError("Approved gold or corpus identity mismatch")
    cases = {case.id: case for case in dataset.cases}
    rows = [
        (number, line, ResultRow.model_validate_json(line))
        for number, line in enumerate(raw_results.splitlines(keepends=True), 1)
    ]
    expected = {(case, arm, repeat) for case in CASE_IDS for arm in ARMS for repeat in range(5)}
    if (
        len(rows) != 160
        or {(row.case_id, row.arm, row.repeat) for _, _, row in rows} != expected
        or len({row.attempt_id for _, _, row in rows}) != 160
    ):
        raise ValueError("Measured case/arm/repetition coverage mismatch")
    for _, _, row in rows:
        if (
            row.run_id != manifest.run_id
            or row.source_sha != source_commit
            or row.gold_hash != manifest.gold_hash
            or row.corpus_hash != manifest.corpus_hash
            or row.pricing_hash != manifest.pricing_hash
            or row.config_hash != row_config_hash(cases[row.case_id], row.arm)
        ):
            raise ValueError("Retained row identity mismatch")
    bindings = {
        "serving_source_commit": source_commit,
        "manifest_sha256": manifest_sha256,
        "results_sha256": results_sha256,
        "collection_files_sha256": dict(manifest.files),
        "batch_ledger_sha256": ledger_hash,
        "owner_record_inventory_sha256": record_inventory_sha256,
        "owner_record_files_sha256": record_files,
        "judge_config_hash": manifest.judge_config_hash,
        "config_map": dict(manifest.config_map),
        "collection_config_hash": manifest.config_hash,
        "protocol_hash": manifest.protocol_hash,
        "gold_hash": manifest.gold_hash,
        "corpus_hash": manifest.corpus_hash,
        "pricing_hash": manifest.pricing_hash,
        "lock_hash": manifest.lock_hash,
    }
    return (
        rows,
        {case.id: tuple(claim.id for claim in case.required_claims) for case in dataset.cases},
        bindings,
    )
