"""Offline acceptance against isolated toy cases and an isolated toy index."""

import json
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from eval_fixtures import TOY_SOURCE_SHA, write_toy_data
from pydantic import ValidationError
from test_eval_security import synthetic_security_xml

from rent_navigator.corpus import Corpus, chunk_section, load_corpus
from rent_navigator.eval import offline
from rent_navigator.eval.models import Activation
from rent_navigator.eval.offline import (
    Baseline,
    OfflineReport,
    file_hash,
    run_offline,
    verify_offline,
)
from rent_navigator.models import NoticeFacts, ToolResult
from rent_navigator.notice import notice_deadline_check


@pytest.fixture
def toy_corpus() -> Corpus:
    chunks = tuple(
        chunk_section(
            "rta",
            "Synthetic isolated evidence",
            "Synthetic isolated question.\n\nSynthetic supporting token.",
        )
    )
    # Two distinct toy chunks are needed for independently authored relevance grades.
    chunks += tuple(
        chunk_section("guideline", "Synthetic additional evidence", "Synthetic isolated support.")
    )
    return replace(
        load_corpus(),
        chunks=chunks,
        corpus_hash=sha256(b"isolated offline test corpus").hexdigest(),
    )


@pytest.fixture
def offline_inputs(tmp_path: Path, toy_corpus: Corpus) -> tuple[Path, Path, Path]:
    data = write_toy_data(tmp_path / "data", toy_corpus)
    xml = synthetic_security_xml(tmp_path / "security.xml")
    lock = tmp_path / "uv.lock"
    lock.write_text("synthetic lock identity\n")
    return data, xml, lock


def execute(tmp_path: Path, corpus: Corpus, inputs: tuple[Path, Path, Path]) -> Path:
    data, xml, lock = inputs
    output = tmp_path / "output"
    run_offline(
        data, output, corpus=corpus, source_sha=TOY_SOURCE_SHA, security_report=xml, lock_path=lock
    )
    return output


def verify(output: Path, corpus: Corpus, inputs: tuple[Path, Path, Path], **changes: Any) -> None:
    data, _, lock = inputs
    options: dict[str, Any] = {
        "corpus": corpus,
        "source_sha": TOY_SOURCE_SHA,
        "expected_manifest_hash": file_hash(output / "manifest.json"),
        "prerequisites": {"checks": "success", "docker": "success", "offline-eval-run": "success"},
        "lock_path": lock,
    }
    options.update(changes)
    assert verify_offline(data, output, **options).offline_complete


def test_toy_offline_covers_sixteen_cases_ten_tools_and_six_rankings(
    tmp_path: Path, toy_corpus: Corpus, offline_inputs: tuple[Path, Path, Path]
) -> None:
    output = execute(tmp_path, toy_corpus, offline_inputs)
    report = OfflineReport.model_validate_json((output / "report.json").read_bytes())
    assert len(report.case_results) == 16
    assert sum(case.actual_tool_result is not None for case in report.case_results) == 10
    assert report.production_retrieval.qa_count == 6
    assert report.baseline_retrieval.mrr_at_5 == report.baseline_retrieval.ndcg_at_5 == 0
    assert report.baseline_comparison == "pending"
    assert report.offline_complete
    assert "latency" not in (output / "report.json").read_text()
    verify(output, toy_corpus, offline_inputs)


def test_draft_gate_prevents_tools_retrieval_and_keeps_failure_artifact(
    tmp_path: Path,
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, xml, lock = offline_inputs
    write_toy_data(data, toy_corpus, approved=False)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("draft gold must never execute")

    monkeypatch.setattr(offline, "build_index", forbidden)
    monkeypatch.setattr(offline, "notice_deadline_check", forbidden)
    monkeypatch.setattr(offline, "rent_increase_check", forbidden)
    with pytest.raises(ValueError, match="explicit approval"):
        run_offline(
            data,
            tmp_path / "output",
            corpus=toy_corpus,
            source_sha=TOY_SOURCE_SHA,
            security_report=xml,
            lock_path=lock,
        )
    assert (
        json.loads((tmp_path / "output" / "failure.json").read_text())["offline_complete"] is False
    )
    assert not (tmp_path / "output" / "report.json").exists()


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled", "missing", "neutral"])
def test_nonpassing_prerequisites_cannot_be_green(
    tmp_path: Path, toy_corpus: Corpus, offline_inputs: tuple[Path, Path, Path], result: str
) -> None:
    output = execute(tmp_path, toy_corpus, offline_inputs)
    with pytest.raises(ValueError, match="prerequisite"):
        verify(
            output,
            toy_corpus,
            offline_inputs,
            prerequisites={"checks": "success", "docker": result, "offline-eval-run": "success"},
        )


@pytest.mark.parametrize(
    "mutation", ["stale", "digest", "payload", "missing", "extra", "lock", "metric"]
)
def test_stale_or_tampered_artifacts_fail_closed(
    tmp_path: Path, toy_corpus: Corpus, offline_inputs: tuple[Path, Path, Path], mutation: str
) -> None:
    output = execute(tmp_path, toy_corpus, offline_inputs)
    changes: dict[str, Any] = {}
    if mutation == "stale":
        changes["source_sha"] = "f" * 40
    elif mutation == "digest":
        changes["expected_manifest_hash"] = "f" * 64
    elif mutation == "missing":
        (output / "security.xml").unlink()
    elif mutation == "extra":
        (output / "unplanned.json").write_text("{}")
    elif mutation == "lock":
        offline_inputs[2].write_text("changed synthetic lock")
    elif mutation == "payload":
        (output / "report.json").write_text("{}")
    else:
        report = json.loads((output / "report.json").read_text())
        report["production_retrieval"]["mrr_at_5"] = 0.0
        (output / "report.json").write_text(json.dumps(report))
        manifest = json.loads((output / "manifest.json").read_text())
        manifest["payload_sha256"]["report.json"] = file_hash(output / "report.json")
        (output / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        verify(output, toy_corpus, offline_inputs, **changes)


def test_existing_output_never_overwritten(
    tmp_path: Path, toy_corpus: Corpus, offline_inputs: tuple[Path, Path, Path]
) -> None:
    output = execute(tmp_path, toy_corpus, offline_inputs)
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        execute(tmp_path, toy_corpus, offline_inputs)
    assert (output / "manifest.json").read_bytes() == before


def baseline_fixture(data: Path, corpus: Corpus, *, score: float = 0.0) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_sha": "3" * 40,
        "corpus_hash": corpus.corpus_hash,
        "gold_hash": file_hash(data / "gold.jsonl"),
        "config_hash": "4" * 64,
        "mrr_at_5": score,
        "ndcg_at_5": score,
        "B": 28,
        "bootstrap_run_id": "00000000-0000-4000-8000-000000000084",
        "bootstrap_manifest_sha256": "5" * 64,
    }


def test_active_comparison_accepts_historical_source_and_configuration(
    tmp_path: Path, toy_corpus: Corpus, offline_inputs: tuple[Path, Path, Path]
) -> None:
    data = offline_inputs[0]
    (data / "activation.json").write_text('{"schema_version":1,"baseline_phase":"active"}')
    (data / "baseline.json").write_text(json.dumps(baseline_fixture(data, toy_corpus)))
    output = execute(tmp_path, toy_corpus, offline_inputs)
    assert json.loads((output / "report.json").read_text())["baseline_comparison"] == "passed"
    verify(output, toy_corpus, offline_inputs)
    with pytest.raises(ValueError, match="return to pending"):
        offline.check_activation(
            Activation(schema_version=1, baseline_phase="pending"),
            Activation(schema_version=1, baseline_phase="active"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("B", True),
        ("B", 27),
        ("B", 33),
        ("mrr_at_5", -0.1),
        ("ndcg_at_5", float("inf")),
        ("bootstrap_run_id", "invalid"),
    ],
)
def test_baseline_schema_rejects_invalid_bootstrap_identity(
    tmp_path: Path,
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
    field: str,
    value: Any,
) -> None:
    value_map = baseline_fixture(offline_inputs[0], toy_corpus)
    value_map[field] = value
    with pytest.raises(ValidationError):
        Baseline.model_validate_json(json.dumps(value_map))


def test_base_activation_is_parsed_from_exact_commit(tmp_path: Path) -> None:
    repo = tmp_path / "history"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    directory = repo / "eval"
    directory.mkdir()
    (directory / "activation.json").write_text('{"schema_version":1,"baseline_phase":"active"}')
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "test: synthetic activation",
        ],
        cwd=repo,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    prior = offline.activation_at_revision(repo, sha)
    assert prior is not None and prior.baseline_phase == "active"


def test_changed_actual_tool_result_preserves_failed_complete_case_inventory(
    tmp_path: Path,
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = notice_deadline_check

    def changed(facts: NoticeFacts, *, corpus: Corpus) -> ToolResult:
        return original(facts, corpus=corpus).model_copy(update={"notice_days": 1})

    monkeypatch.setattr(offline, "notice_deadline_check", changed)
    with pytest.raises(ValueError, match="exact checks"):
        execute(tmp_path, toy_corpus, offline_inputs)
    report = OfflineReport.model_validate_json((tmp_path / "output" / "report.json").read_bytes())
    assert len(report.case_results) == 16
    assert sum(not result.exact_match for result in report.case_results) == 4
    assert report.offline_complete is False


@pytest.mark.parametrize("frozen,passes", [(5e-13, True), (2e-12, False)])
def test_active_retrieval_comparison_uses_only_frozen_tolerance(
    tmp_path: Path,
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    frozen: float,
    passes: bool,
) -> None:
    data = offline_inputs[0]
    (data / "activation.json").write_text('{"schema_version":1,"baseline_phase":"active"}')
    (data / "baseline.json").write_text(
        json.dumps(baseline_fixture(data, toy_corpus, score=frozen))
    )
    monkeypatch.setattr(offline, "search", lambda *args, **kwargs: ())
    if passes:
        output = execute(tmp_path, toy_corpus, offline_inputs)
        assert OfflineReport.model_validate_json(
            (output / "report.json").read_bytes()
        ).offline_complete
    else:
        with pytest.raises(ValueError, match="baseline comparison"):
            execute(tmp_path, toy_corpus, offline_inputs)


def test_active_phase_without_baseline_and_removed_evidence_fail_before_execution(
    tmp_path: Path,
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
) -> None:
    data = offline_inputs[0]
    (data / "activation.json").write_text('{"schema_version":1,"baseline_phase":"active"}')
    with pytest.raises(ValueError, match="baseline.json"):
        execute(tmp_path, toy_corpus, offline_inputs)
    (data / "activation.json").write_text('{"schema_version":1,"baseline_phase":"pending"}')
    missing = replace(toy_corpus, chunks=toy_corpus.chunks[:1])
    with pytest.raises(ValueError, match="do not resolve"):
        execute(tmp_path / "another", missing, offline_inputs)


@pytest.mark.parametrize("mutation", ["extra", "missing", "bool_version"])
def test_baseline_schema_requires_exact_provenance_fields(
    toy_corpus: Corpus,
    offline_inputs: tuple[Path, Path, Path],
    mutation: str,
) -> None:
    values = baseline_fixture(offline_inputs[0], toy_corpus)
    if mutation == "extra":
        values["approved"] = True
    elif mutation == "missing":
        del values["bootstrap_manifest_sha256"]
    else:
        values["schema_version"] = True
    with pytest.raises(ValidationError):
        Baseline.model_validate_json(json.dumps(values))
