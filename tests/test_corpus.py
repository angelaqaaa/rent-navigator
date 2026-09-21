"""Synthetic integrity fixtures and checks of the committed official snapshot."""

import json
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from rent_navigator.corpus import (
    DATA_PATHS,
    GUIDELINE_HEADINGS,
    LTB_HEADINGS,
    RAW_EXTENSIONS,
    RTA_SECTIONS,
    SOURCE_URLS,
    Chunk,
    Rule,
    Source,
    SourceId,
    chunk_id,
    chunk_section,
    corpus_hash,
    load_corpus,
    normalize_text,
)
from rent_navigator.models import RuleId


def _json_file(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def _chunks_file(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(chunk) + "\n" for chunk in chunks), encoding="utf-8")


def _chunk_rows(root: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / "chunks.jsonl").read_text().splitlines()]


@pytest.fixture
def synthetic_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic_corpus"
    (root / "raw").mkdir(parents=True)
    (root / "text").mkdir()
    chunks: list[Chunk] = []
    source_rows: list[dict[str, object]] = []
    by_section: dict[str, str] = {}
    for source_id in get_args(SourceId):
        if source_id == "rta":
            headings = [f"RTA s. {section}" for section in sorted(RTA_SECTIONS)]
        elif source_id == "legislation_act":
            headings = ["Legislation Act s. 89"]
        elif source_id == "guideline":
            headings = sorted(GUIDELINE_HEADINGS)
        elif source_id == "ltb_guide":
            headings = sorted(LTB_HEADINGS)
        else:
            headings = [f"Synthetic {source_id} instructions"]
        full_text: list[str] = []
        for heading in headings:
            text = f"Synthetic passage for {heading}; fixture only."
            chunk = chunk_section(source_id, heading, text)[0]
            chunks.append(chunk)
            by_section[heading] = chunk.id
            full_text.extend((heading, text))
        raw = f"Synthetic original for {source_id}; not official source material.".encode()
        text_bytes = ("\n\n".join(full_text) + "\n").encode()
        (root / "raw" / f"{source_id}.{RAW_EXTENSIONS[source_id]}").write_bytes(raw)
        (root / "text" / f"{source_id}.txt").write_bytes(text_bytes)
        source_rows.append(
            {
                "source_id": source_id,
                "canonical_url": SOURCE_URLS[source_id],
                "retrieved_url": SOURCE_URLS[source_id],
                "title": f"Synthetic {source_id} source",
                "fetched_at_utc": (
                    "2026-09-20T23:59:59Z" if source_id == "rta" else "2026-09-21T00:00:01Z"
                ),
                "consolidation_period": (
                    "Synthetic operative period"
                    if source_id in {"rta", "legislation_act"}
                    else None
                ),
                "raw_sha256": sha256(raw).hexdigest(),
                "text_sha256": sha256(text_bytes).hexdigest(),
            }
        )
    _json_file(root / "manifest.json", {"snapshot_date": "2026-09-20", "sources": source_rows})
    _chunks_file(root / "chunks.jsonl", [chunk.model_dump(mode="json") for chunk in chunks])
    rule_data: list[tuple[RuleId, int | str, str, list[str]]] = [
        ("notice.90_days", 90, "calendar_days", ["RTA s. 116"]),
        ("notice.mail_5_days", 5, "calendar_days", ["RTA s. 191"]),
        ("spacing.12_months", 12, "calendar_months", ["RTA s. 119"]),
        ("guideline.2026", "2.1", "percent", ["RTA s. 120", "Rent increase guideline"]),
        ("guideline.2027", "1.9", "percent", ["RTA s. 120", "Rent increase guideline"]),
        ("exemption.s6_1", "confirmed_only", "semantic", ["RTA s. 6.1"]),
        ("calendar.s89", "calendar", "semantic", ["Legislation Act s. 89"]),
        ("form.N1", "N1", "form", ["Synthetic n1 instructions"]),
        ("form.N2", "N2", "form", ["Synthetic n2 instructions"]),
        ("scope.ordinary", "confirmed_only", "semantic", ["RTA s. 5", "RTA s. 6", "RTA s. 7"]),
    ]
    _json_file(
        root / "rules.json",
        [
            {
                "id": rule_id,
                "value": value,
                "unit": unit,
                "description": "Synthetic rule evidence fixture.",
                "evidence_ids": [by_section[heading] for heading in headings],
            }
            for rule_id, value, unit, headings in rule_data
        ],
    )
    return root


def test_normalize_nfc_lf_horizontal_space_preserves_legal_paragraphs() -> None:
    raw = "  Section\t1\r\n\r\n(1) Cafe\u0301\u00a0  rent. \r(2)\tNext sentence.  "
    expected = "Section 1\n\n(1) Café rent.\n(2) Next sentence."
    assert normalize_text(raw) == expected
    assert normalize_text(expected) == expected


def test_chunk_identity_exact_utf8_lf_protocol() -> None:
    chunk = chunk_section("rta", "RTA s. 116", "(1) Café notice.")[0]
    expected = "https://www.ontario.ca/laws/statute/06r17\nRTA s. 116\n1\n(1) Café notice."
    assert chunk.id == sha256(expected.encode("utf-8")).hexdigest()
    assert len(chunk.id) == 64
    assert chunk.word_count == 3
    assert chunk.id != chunk_id(SOURCE_URLS["rta"], chunk.heading, 2, chunk.text)


def test_chunk_packs_whole_paragraphs_without_crossing_sections() -> None:
    first = " ".join(f"first{index}" for index in range(250))
    second = " ".join(f"second{index}" for index in range(251))
    third = " ".join(f"third{index}" for index in range(249))
    chunks = chunk_section("rta", "RTA s. 116", f"{first}\n\n{second}\n\n{third}")
    assert [chunk.word_count for chunk in chunks] == [250, 500]
    assert [chunk.part for chunk in chunks] == [1, 2]
    assert chunks[0].text == first
    assert chunks[1].text == second + "\n\n" + third
    assert chunk_section("rta", "RTA s. 119", third)[0].part == 1


@pytest.mark.parametrize(
    "word_count,expected", [(499, [499]), (500, [500]), (501, [500, 1]), (1001, [500, 500, 1])]
)
def test_oversized_paragraph_slices_are_contiguous(word_count: int, expected: list[int]) -> None:
    words = [f"word{index}" for index in range(word_count)]
    chunks = chunk_section("rta", "RTA s. 116", " ".join(words))
    assert [chunk.word_count for chunk in chunks] == expected
    assert [word for chunk in chunks for word in chunk.text.split()] == words
    assert [chunk.part for chunk in chunks] == list(range(1, len(chunks) + 1))


def test_oversized_paragraph_does_not_absorb_adjacent_paragraph() -> None:
    text = "Earlier paragraph.\n\n" + " ".join(["word"] * 501) + "\n\nLater paragraph."
    assert [chunk.word_count for chunk in chunk_section("rta", "RTA s. 116", text)] == [
        2,
        500,
        1,
        2,
    ]


def test_empty_section_has_no_fabricated_chunk() -> None:
    assert chunk_section("rta", "RTA s. 116", " \r\n\t") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "0" * 64),
        ("id", "0" * 63),
        ("part", 0),
        ("part", True),
        ("part", "1"),
        ("word_count", 500),
        ("word_count", True),
        ("source_id", "unofficial"),
        ("text", "Café\trules."),
        ("text", "Cafe\u0301 rules."),
        ("text", ""),
        ("heading", "RTA s. 116\nAnother heading"),
        ("extra", "unexpected"),
    ],
)
def test_chunk_json_rejects_invalid_structure(field: str, value: object) -> None:
    payload = chunk_section("rta", "RTA s. 116", "Café rules.")[0].model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        Chunk.model_validate_json(json.dumps(payload))


def test_chunk_and_rule_values_are_frozen() -> None:
    chunk = chunk_section("rta", "RTA s. 116", "Synthetic notice text.")[0]
    with pytest.raises(ValidationError, match="frozen"):
        chunk.__setattr__("text", "Changed text.")
    rule = Rule(
        id="notice.90_days",
        value=90,
        unit="calendar_days",
        description="Synthetic rule.",
        evidence_ids=(chunk.id,),
    )
    with pytest.raises(ValidationError, match="frozen"):
        rule.__setattr__("value", 91)


def test_synthetic_corpus_resolves_immutable_rules_and_canonical_citations(
    synthetic_corpus: Path,
) -> None:
    corpus = load_corpus(synthetic_corpus)
    assert corpus.snapshot_date == date(2026, 9, 20)
    assert len(corpus.sources) == 6
    assert {rule.id for rule in corpus.rules} == set(get_args(RuleId))
    for rule in corpus.rules:
        assert corpus.rule(rule.id) == rule
        for evidence in rule.evidence_ids:
            chunk = corpus.chunk(evidence)
            citation = corpus.citation(evidence)
            assert citation.id == evidence
            assert citation.url == SOURCE_URLS[chunk.source_id]
            assert citation.heading == chunk.heading
            assert citation.snapshot_date == corpus.snapshot_date
    with pytest.raises(KeyError):
        corpus.chunk("0" * 64)
    with pytest.raises(KeyError):
        corpus.citation("0" * 64)


def test_corpus_and_source_metadata_cannot_be_mutated(synthetic_corpus: Path) -> None:
    corpus = load_corpus(synthetic_corpus)
    with pytest.raises(FrozenInstanceError):
        corpus.__setattr__("snapshot_date", date(2020, 1, 1))
    with pytest.raises(ValidationError, match="frozen"):
        corpus.sources[0].__setattr__("title", "Changed source title")
    with pytest.raises(ValidationError, match="frozen"):
        corpus.rules[0].__setattr__("evidence_ids", ("0" * 64,))
    assert isinstance(corpus.sources, tuple)
    assert isinstance(corpus.chunks, tuple)
    assert isinstance(corpus.rules, tuple)
    assert isinstance(corpus.rules[0].evidence_ids, tuple)


def test_hash_exact_sorted_posix_protocol(synthetic_corpus: Path) -> None:
    expected = "".join(
        path + "\n" + sha256((synthetic_corpus / path).read_bytes()).hexdigest() + "\n"
        for path in sorted(DATA_PATHS)
    )
    assert len(DATA_PATHS) == 15
    assert corpus_hash(synthetic_corpus) == sha256(expected.encode("utf-8")).hexdigest()
    assert load_corpus(synthetic_corpus).corpus_hash == corpus_hash(synthetic_corpus)


@pytest.mark.parametrize("relative_path", DATA_PATHS)
def test_each_formal_file_changes_corpus_hash(synthetic_corpus: Path, relative_path: str) -> None:
    before = corpus_hash(synthetic_corpus)
    target = synthetic_corpus / relative_path
    target.write_bytes(target.read_bytes() + b"\n")
    assert corpus_hash(synthetic_corpus) != before


def test_hash_excludes_derived_sqlite_cache_code_and_hash_output(synthetic_corpus: Path) -> None:
    before = corpus_hash(synthetic_corpus)
    for filename in ("__init__.py", "snapshot.py", "corpus_hash.txt"):
        (synthetic_corpus / filename).write_text("Synthetic derived bytes.")
    (synthetic_corpus / "cache").mkdir()
    (synthetic_corpus / "cache" / "entry.json").write_text('{"synthetic":true}')
    with sqlite3.connect(synthetic_corpus / "index.sqlite3") as connection:
        connection.execute("CREATE TABLE synthetic (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO synthetic VALUES (1)")
    assert corpus_hash(synthetic_corpus) == before


@pytest.mark.parametrize("kind", ["raw", "text"])
def test_manifest_hash_detects_corruption(synthetic_corpus: Path, kind: str) -> None:
    suffix = "json" if kind == "raw" else "txt"
    (synthetic_corpus / kind / f"rta.{suffix}").write_bytes(b"Changed synthetic data.")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_corpus(synthetic_corpus)


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_url", "https://example.com/unofficial"),
        ("retrieved_url", "https://ontario.ca.example.com/fake"),
        ("retrieved_url", "https://example.com@ontario.ca/fake"),
        ("retrieved_url", "http://www.ontario.ca/fake"),
        ("retrieved_url", "https://www.ontario.ca:1234/fake"),
        ("fetched_at_utc", "2026-09-21T00:00:00"),
        ("fetched_at_utc", "2026-09-21T00:00:00-04:00"),
        ("consolidation_period", None),
    ],
)
def test_source_json_rejects_bad_provenance(
    synthetic_corpus: Path, field: str, value: object
) -> None:
    manifest = json.loads((synthetic_corpus / "manifest.json").read_text())
    source = next(row for row in manifest["sources"] if row["source_id"] == "rta")
    source[field] = value
    with pytest.raises(ValidationError):
        Source.model_validate_json(json.dumps(source))


def test_official_statute_api_transport_is_distinct_from_citation_url(
    synthetic_corpus: Path,
) -> None:
    manifest = json.loads((synthetic_corpus / "manifest.json").read_text())
    source = next(row for row in manifest["sources"] if row["source_id"] == "rta")
    source["retrieved_url"] = (
        "https://www.ontario.ca/laws/api/v2/legislation/en/doc-search/statute/06r17"
    )
    value = Source.model_validate_json(json.dumps(source))
    assert value.canonical_url != value.retrieved_url


@pytest.mark.parametrize(
    "mutation", ["duplicate_source", "missing_source", "date", "consolidation"]
)
def test_manifest_rejects_missing_or_inconsistent_metadata(
    synthetic_corpus: Path, mutation: str
) -> None:
    path = synthetic_corpus / "manifest.json"
    manifest = json.loads(path.read_text())
    if mutation == "duplicate_source":
        manifest["sources"][-1] = manifest["sources"][0]
    elif mutation == "missing_source":
        manifest["sources"].pop()
    elif mutation == "date":
        manifest["snapshot_date"] = "2026-09-21"
    else:
        next(row for row in manifest["sources"] if row["source_id"] == "n1")[
            "consolidation_period"
        ] = "Not a legislative source"
    _json_file(path, manifest)
    with pytest.raises(ValidationError):
        load_corpus(synthetic_corpus)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing_section",
        "missing_source",
        "part",
        "unselected_rta",
        "unselected_ltb",
        "unselected_guideline",
        "fabricated_text",
    ],
)
def test_loader_rejects_invalid_chunk_collection(synthetic_corpus: Path, mutation: str) -> None:
    chunks = _chunk_rows(synthetic_corpus)
    if mutation == "duplicate":
        chunks.append(chunks[0])
    elif mutation == "missing_section":
        chunks = [chunk for chunk in chunks if chunk["heading"] != "RTA s. 116"]
    elif mutation == "missing_source":
        chunks = [chunk for chunk in chunks if chunk["source_id"] != "n1"]
    else:
        source = {"unselected_ltb": "ltb_guide", "unselected_guideline": "guideline"}.get(
            mutation, "rta"
        )
        row = next(chunk for chunk in chunks if chunk["source_id"] == source)
        if mutation == "part":
            row["part"] = 2
        elif mutation == "fabricated_text":
            row["text"] = "Fabricated words absent from the complete extraction."
            row["word_count"] = len(row["text"].split())
        else:
            row["heading"] = "RTA s. 42" if source == "rta" else "Unselected heading"
        row["id"] = chunk_id(SOURCE_URLS[source], row["heading"], row["part"], row["text"])
    _chunks_file(synthetic_corpus / "chunks.jsonl", chunks)
    with pytest.raises(ValueError):
        load_corpus(synthetic_corpus)


@pytest.mark.parametrize(
    "source_id,heading",
    [("guideline", heading) for heading in sorted(GUIDELINE_HEADINGS)]
    + [("ltb_guide", heading) for heading in sorted(LTB_HEADINGS)],
)
def test_every_selected_guidance_section_is_required(
    synthetic_corpus: Path, source_id: str, heading: str
) -> None:
    chunks = [
        row
        for row in _chunk_rows(synthetic_corpus)
        if (row["source_id"], row["heading"]) != (source_id, heading)
    ]
    _chunks_file(synthetic_corpus / "chunks.jsonl", chunks)
    with pytest.raises(ValueError, match="every selected guidance section"):
        load_corpus(synthetic_corpus)


@pytest.mark.parametrize("reverse", [False, True])
def test_chunk_paragraphs_may_omit_annotations_but_keep_source_order(
    synthetic_corpus: Path, reverse: bool
) -> None:
    chunks = _chunk_rows(synthetic_corpus)
    row = next(chunk for chunk in chunks if chunk["heading"] == "RTA s. 117")
    first = row["text"]
    later = "A later synthetic operative paragraph."
    row["text"] = (later + "\n\n" + first) if reverse else (first + "\n\n" + later)
    row["word_count"] = len(row["text"].split())
    row["id"] = chunk_id(SOURCE_URLS["rta"], row["heading"], row["part"], row["text"])
    _chunks_file(synthetic_corpus / "chunks.jsonl", chunks)
    text_path = synthetic_corpus / "text" / "rta.txt"
    text_path.write_text(
        text_path.read_text().replace(
            first, first + "\n\nSynthetic future amendment annotation.\n\n" + later
        )
    )
    manifest_path = synthetic_corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    next(source for source in manifest["sources"] if source["source_id"] == "rta")[
        "text_sha256"
    ] = sha256(text_path.read_bytes()).hexdigest()
    _json_file(manifest_path, manifest)
    if reverse:
        with pytest.raises(ValueError, match="faithful extract"):
            load_corpus(synthetic_corpus)
    else:
        assert load_corpus(synthetic_corpus).chunk(row["id"]).text == row["text"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_rule",
        "duplicate_rule",
        "unknown_evidence",
        "wrong_constant",
        "wrong_unit",
        "no_guideline_statute",
        "no_guideline_page",
        "empty_evidence",
        "float",
        "bool",
    ],
)
def test_loader_rejects_invalid_rule_mapping(synthetic_corpus: Path, mutation: str) -> None:
    path = synthetic_corpus / "rules.json"
    rules = json.loads(path.read_text())
    if mutation == "missing_rule":
        rules.pop()
    elif mutation == "duplicate_rule":
        rules.append(rules[0])
    elif mutation == "unknown_evidence":
        rules[0]["evidence_ids"] = ["0" * 64]
    elif mutation == "wrong_constant":
        rules[0]["value"] = 89
    elif mutation == "wrong_unit":
        rules[0]["unit"] = "months"
    elif mutation in {"no_guideline_statute", "no_guideline_page"}:
        rule = next(row for row in rules if row["id"] == "guideline.2026")
        rule["evidence_ids"].pop(0 if mutation == "no_guideline_statute" else 1)
    elif mutation == "empty_evidence":
        rules[0]["evidence_ids"] = []
    elif mutation == "float":
        rules[0]["value"] = 90.0
    else:
        rules[0]["value"] = True
    _json_file(path, rules)
    with pytest.raises(ValueError):
        load_corpus(synthetic_corpus)


@pytest.mark.parametrize("rule_id", ["exemption.s6_1", "calendar.s89", "scope.ordinary"])
@pytest.mark.parametrize("field,value", [("value", 1), ("unit", "percent"), ("value", "   ")])
def test_semantic_rules_preserve_explicit_non_numeric_units(
    synthetic_corpus: Path, rule_id: str, field: str, value: object
) -> None:
    path = synthetic_corpus / "rules.json"
    rules = json.loads(path.read_text())
    next(rule for rule in rules if rule["id"] == rule_id)[field] = value
    _json_file(path, rules)
    with pytest.raises(ValueError):
        load_corpus(synthetic_corpus)


def test_committed_corpus_is_readable_without_source_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    corpus = load_corpus()
    assert len(corpus.sources) == 6
    assert corpus.snapshot_date == min(source.fetched_at_utc.date() for source in corpus.sources)
    assert all(chunk.word_count <= 500 for chunk in corpus.chunks)
    assert {rule.id for rule in corpus.rules} == set(get_args(RuleId))
    assert corpus.corpus_hash == corpus_hash()
