"""Isolated, hand-authored toy evaluation data; never the approved case oracle."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rent_navigator.corpus import Corpus
from rent_navigator.eval.models import CASE_IDS, GoldCase
from rent_navigator.index import SearchHit, build_index, search

TOY_ATTEMPT_ID = "00000000-0000-4000-8000-000000000081"
TOY_RUN_ID = "00000000-0000-4000-8000-000000000082"
TOY_TRACE_ID = "00000000-0000-4000-8000-000000000083"
TOY_SOURCE_SHA = "1" * 40
TOY_HASH = "2" * 64

_RETRIEVAL_FIXTURES: dict[tuple[str, str], tuple[SearchHit, ...]] = {}


def canonical_hits(corpus: Corpus, query: str) -> tuple[SearchHit, ...]:
    """Use the real fixed index for tests exercising accepted-evidence verification."""
    key = (corpus.corpus_hash, query)
    if key not in _RETRIEVAL_FIXTURES:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite"
            build_index(path, corpus)
            _RETRIEVAL_FIXTURES[key] = search(path, query, expected_corpus_hash=corpus.corpus_hash)
    return _RETRIEVAL_FIXTURES[key]


def toy_gold_rows(corpus: Corpus) -> list[dict[str, Any]]:
    """All dates/amounts are absent: expected unknowns are authored directly."""
    evidence = [chunk.id for chunk in corpus.chunks[:2]]
    rows: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        kind = {"R": "rent", "N": "notice", "Q": "qa"}[case_id[0]]
        row: dict[str, Any] = {
            "id": case_id,
            "kind": kind,
            "request": {
                "mode": "question",
                "attempt_id": TOY_ATTEMPT_ID,
                "question": f"Synthetic isolated question {case_id}.",
            },
            "letter": None,
            "expected_extract": None,
            "expected_status": "answered",
            "expected_tool": None,
            "expected_tool_args": None,
            "expected_tool_result": None,
            "required_claims": [{"id": "c1", "text": "Synthetic required statement."}],
            "evidence_ids": evidence.copy(),
            "relevance": {evidence[0]: 2, evidence[1]: 1} if kind == "qa" else {},
        }
        if kind != "qa":
            facts: dict[str, Any] = {
                "scope": {"ordinary": "unknown", "period_start": "unknown"},
                "effective_on": None,
                "served_on": None,
                "service_method": "unknown",
            }
            check_rules = {
                "scope": ["scope.ordinary"],
                "supported_year": [],
                "period_start": ["scope.ordinary"],
                "notice": ["calendar.s89", "notice.90_days"],
            }
            if kind == "rent":
                facts.update(
                    current_cents=None,
                    proposed_cents=None,
                    tenancy_start=None,
                    last_increase={"state": "unknown", "date": None},
                    guideline_status="unknown",
                    form="unknown",
                )
                check_rules.update(
                    spacing=["calendar.s89", "spacing.12_months"],
                    guideline=["exemption.s6_1"],
                    form=["form.N1", "form.N2"],
                )
            tool = "rent_increase_check" if kind == "rent" else "notice_deadline_check"
            row["request"] = {
                "mode": kind,
                "attempt_id": TOY_ATTEMPT_ID,
                "confirmed": True,
                "facts": facts,
            }
            row["expected_tool"] = tool
            row["expected_tool_args"] = facts.copy()
            row["expected_status"] = "cannot_determine"
            row["expected_tool_result"] = {
                "tool": tool,
                "status": "cannot_determine",
                "checks": [
                    {"id": check, "status": "unknown", "reason": "missing_fact", "rule_ids": ids}
                    for check, ids in check_rules.items()
                ],
                "deemed_served_on": None,
                "notice_days": None,
                "earliest_notice_on": None,
                "latest_deemed_service_on": None,
                "latest_dispatch_on": None,
                "earliest_spacing_on": None,
                "guideline_percent": None,
                "cap_cents_exact": None,
                "rule_ids": sorted({rule for ids in check_rules.values() for rule in ids}),
            }
            if case_id in {"R02", "R04"}:
                row["letter"] = f"Synthetic isolated letter {case_id}, no amounts or dates."
                row["expected_extract"] = {
                    "current_cents": None,
                    "proposed_cents": None,
                    "effective_on": None,
                }
        rows.append(row)
    return rows


def toy_cases(corpus: Corpus) -> tuple[GoldCase, ...]:
    return tuple(GoldCase.model_validate_json(json.dumps(row)) for row in toy_gold_rows(corpus))


def write_toy_data(path: Path, corpus: Corpus, *, approved: bool = True) -> Path:
    """Write a test-only approval fixture to an explicitly isolated directory."""
    path.mkdir(parents=True, exist_ok=True)
    gold_bytes = "".join(json.dumps(row) + "\n" for row in toy_gold_rows(corpus)).encode()
    (path / "gold.jsonl").write_bytes(gold_bytes)
    (path / "gold-approval.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "approved" if approved else "draft",
                "gold_sha256": sha256(gold_bytes).hexdigest(),
                "corpus_hash": corpus.corpus_hash,
                "approved_by": "Angela" if approved else None,
                "approved_at_utc": "2020-01-01T00:00:00Z" if approved else None,
            }
        )
    )
    (path / "activation.json").write_text(
        json.dumps({"schema_version": 1, "baseline_phase": "pending"})
    )
    return path
