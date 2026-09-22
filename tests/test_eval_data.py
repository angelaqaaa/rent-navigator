"""Read-only approval and provenance loading using isolated toy datasets."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from eval_fixtures import write_toy_data

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.models import CASE_IDS


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def data_dir(tmp_path: Path, corpus: Corpus) -> Path:
    return write_toy_data(tmp_path / "toy-data", corpus)


def _rewrite_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    content = "".join(json.dumps(row) + "\n" for row in rows).encode()
    (path / "gold.jsonl").write_bytes(content)
    approval_path = path / "gold-approval.json"
    approval = json.loads(approval_path.read_bytes())
    approval["gold_sha256"] = sha256(content).hexdigest()
    approval_path.write_text(json.dumps(approval))


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (path / "gold.jsonl").read_bytes().splitlines()]


def test_approved_toy_dataset_has_exact_order_hashes_and_unchanged_input(
    data_dir: Path, corpus: Corpus
) -> None:
    before = {file.name: file.read_bytes() for file in data_dir.iterdir()}
    dataset = load_gold(data_dir, corpus)
    assert tuple(case.id for case in dataset.cases) == CASE_IDS
    assert dataset.gold_hash == sha256(before["gold.jsonl"]).hexdigest()
    assert dataset.approval_hash == sha256(before["gold-approval.json"]).hexdigest()
    assert dataset.approval.status == "approved"
    assert dataset.activation.baseline_phase == "pending"
    assert {file.name: file.read_bytes() for file in data_dir.iterdir()} == before


def test_draft_is_only_available_through_explicit_validation(
    tmp_path: Path, corpus: Corpus
) -> None:
    path = write_toy_data(tmp_path / "draft", corpus, approved=False)
    with pytest.raises(ValueError, match="explicit approval"):
        load_gold(path, corpus)
    dataset = load_gold(path, corpus, require_approved=False)
    assert dataset.approval.status == "draft"


@pytest.mark.parametrize("filename", ["gold.jsonl", "gold-approval.json", "activation.json"])
def test_all_data_files_are_required(data_dir: Path, corpus: Corpus, filename: str) -> None:
    (data_dir / filename).unlink()
    with pytest.raises(FileNotFoundError):
        load_gold(data_dir, corpus)


@pytest.mark.parametrize("require_approved", [True, False])
@pytest.mark.parametrize("field", ["gold_sha256", "corpus_hash"])
def test_hash_mismatch_fails_even_draft_validation(
    data_dir: Path, corpus: Corpus, field: str, require_approved: bool
) -> None:
    path = data_dir / "gold-approval.json"
    value = json.loads(path.read_bytes())
    value[field] = "0" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="hashes"):
        load_gold(data_dir, corpus, require_approved=require_approved)


def test_any_gold_byte_change_invalidates_approval(data_dir: Path, corpus: Corpus) -> None:
    path = data_dir / "gold.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hashes"):
        load_gold(data_dir, corpus)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reorder", "unknown"])
def test_exact_sixteen_ids_in_fixed_order(data_dir: Path, corpus: Corpus, change: str) -> None:
    rows = _rows(data_dir)
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(rows[-1])
    elif change == "reorder":
        rows[0], rows[1] = rows[1], rows[0]
    else:
        rows[-1]["id"] = "Q07"
    _rewrite_rows(data_dir, rows)
    with pytest.raises(ValueError):
        load_gold(data_dir, corpus)


@pytest.mark.parametrize("field", ["evidence_ids", "relevance"])
def test_unresolved_evidence_fails_before_execution(
    data_dir: Path, corpus: Corpus, field: str
) -> None:
    rows = _rows(data_dir)
    if field == "evidence_ids":
        rows[10][field].append("0" * 64)
    else:
        rows[10][field]["0" * 64] = 1
    _rewrite_rows(data_dir, rows)
    with pytest.raises(ValueError, match="resolve"):
        load_gold(data_dir, corpus)


def test_evidence_and_relevance_need_not_be_equal(data_dir: Path, corpus: Corpus) -> None:
    rows = _rows(data_dir)
    rows[10]["evidence_ids"] = rows[10]["evidence_ids"][:1]
    _rewrite_rows(data_dir, rows)
    case = load_gold(data_dir, corpus).cases[10]
    assert set(case.evidence_ids) < set(case.relevance)


def test_at_least_two_questions_have_both_grades(data_dir: Path, corpus: Corpus) -> None:
    rows = _rows(data_dir)
    for row in rows[11:]:
        row["relevance"] = {identifier: 2 for identifier in row["relevance"]}
    _rewrite_rows(data_dir, rows)
    with pytest.raises(ValueError, match="at least two"):
        load_gold(data_dir, corpus)


def test_malformed_and_blank_rows_are_not_silently_removed(data_dir: Path, corpus: Corpus) -> None:
    path = data_dir / "gold.jsonl"
    content = path.read_bytes() + b"\n"
    path.write_bytes(content)
    approval_path = data_dir / "gold-approval.json"
    approval = json.loads(approval_path.read_bytes())
    approval["gold_sha256"] = sha256(content).hexdigest()
    approval_path.write_text(json.dumps(approval))
    with pytest.raises(ValueError):
        load_gold(data_dir, corpus)


def test_active_never_silently_falls_back_when_baseline_is_missing(
    data_dir: Path, corpus: Corpus
) -> None:
    (data_dir / "activation.json").write_text(
        json.dumps({"schema_version": 1, "baseline_phase": "active"})
    )
    with pytest.raises(ValueError, match="baseline.json"):
        load_gold(data_dir, corpus)


def test_missing_nullable_field_in_a_single_row_is_rejected(data_dir: Path, corpus: Corpus) -> None:
    rows = _rows(data_dir)
    del rows[0]["letter"]
    _rewrite_rows(data_dir, rows)
    with pytest.raises(ValueError):
        load_gold(data_dir, corpus)
