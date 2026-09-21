"""Verify derivation and provenance of the real committed official snapshot."""

from importlib.resources import files
from pathlib import Path
from shutil import copytree

import pytest

from rent_navigator.corpus import load_corpus
from rent_navigator.corpus.snapshot import derived_files, verify_snapshot


def test_every_committed_derivative_reproduces_from_original_responses() -> None:
    root = Path(str(files("rent_navigator.corpus")))
    verify_snapshot(root)
    derived = derived_files(root)
    assert len(derived) == 8
    assert set(derived) == {
        "chunks.jsonl",
        "rules.json",
        "text/rta.txt",
        "text/legislation_act.txt",
        "text/guideline.txt",
        "text/ltb_guide.txt",
        "text/n1.txt",
        "text/n2.txt",
    }


@pytest.mark.parametrize(
    "path", ["raw/rta.json", "text/guideline.txt", "chunks.jsonl", "rules.json"]
)
def test_snapshot_verification_rejects_changed_data(tmp_path: Path, path: str) -> None:
    source = Path(str(files("rent_navigator.corpus")))
    root = tmp_path / "corpus"
    copytree(source, root)
    target = root / path
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        verify_snapshot(root)


def test_committed_rule_evidence_contains_the_supported_years_and_constants() -> None:
    corpus = load_corpus()
    for year, percent in (("2026", "2.1"), ("2027", "1.9")):
        rule = next(rule for rule in corpus.rules if rule.id == f"guideline.{year}")
        official = [corpus.chunk(identifier) for identifier in rule.evidence_ids]
        guidance = [chunk.text for chunk in official if chunk.source_id == "guideline"]
        assert any(year in text and percent in text for text in guidance)
        assert any(chunk.source_id == "rta" and "120" in chunk.heading for chunk in official)
    assert corpus.rule("notice.90_days").value == 90
    assert corpus.rule("notice.mail_5_days").value == 5
    assert corpus.rule("spacing.12_months").value == 12
