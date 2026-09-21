"""Synthetic algorithm fixtures and package smoke; no retrieval quality measurements."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from rent_navigator.corpus import Chunk, load_corpus
from rent_navigator.index import (
    NOTICE_QUERY,
    RENT_QUERY,
    build_index,
    inspect_index,
    main,
    query_expression,
    search,
)

SYNTHETIC_HASH = "e" * 64


@dataclass(frozen=True)
class SyntheticCorpus:
    chunks: tuple[Chunk, ...]
    corpus_hash: str = SYNTHETIC_HASH


def synthetic_chunk(text: str, *, heading: str = "Synthetic passage", part: int = 1) -> Chunk:
    chunk_id = sha256(
        "\n".join(("https://www.ontario.ca/laws/statute/06r17", heading, str(part), text)).encode()
    ).hexdigest()
    return Chunk(
        id=chunk_id,
        source_id="rta",
        heading=heading,
        part=part,
        text=text,
        word_count=len(text.split()),
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("", ""),
        (' \n\t — _ + * " %', ""),
        ("Rent rent RENT", '"rent"'),
        ("MAIL notice mail rent NOTICE", '"mail" OR "notice" OR "rent"'),
        (
            "préavis Straße STRASSE 中文１２ Ⅻ ²",
            '"préavis" OR "strasse" OR "中文１２" OR "ⅻ" OR "²"',
        ),
        ("N1_N2 2026 2.1", '"n1" OR "n2" OR "2026" OR "2" OR "1"'),
        ("the AND OR NOT NEAR", '"the" OR "and" OR "or" OR "not" OR "near"'),
        ('heading:rent* -mail ("days")', '"heading" OR "rent" OR "mail" OR "days"'),
    ],
)
def test_query_tokens_follow_frozen_normalization(question: str, expected: str) -> None:
    assert query_expression(question) == expected


def test_fact_query_constants_are_exact() -> None:
    assert NOTICE_QUERY == "Ontario residential rent increase notice 90 days service mail"
    assert RENT_QUERY == (
        "Ontario residential rent increase notice 90 days service mail "
        "guideline 12 months exemption N1 N2"
    )


def test_search_sorts_by_bm25_ascending_then_id_and_limits_five(tmp_path: Path) -> None:
    chunks = tuple(
        synthetic_chunk(
            " ".join(["beacon"] * frequency + ["filler"] * (10 - frequency)), part=frequency
        )
        for frequency in range(1, 9)
    )
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus(chunks))
    hits = search(database, "beacon")
    assert len(hits) == len({hit.chunk.id for hit in hits}) == 5
    assert [hit.chunk.part for hit in hits] == [8, 7, 6, 5, 4]
    assert [hit.score for hit in hits] == sorted(hit.score for hit in hits)
    assert all(hit.score < 0 for hit in hits)


def test_equal_scores_tie_break_by_id_and_default_heading_text_weights(tmp_path: Path) -> None:
    heading_match = synthetic_chunk("copper", heading="beacon")
    text_match = synthetic_chunk("beacon", heading="copper")
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus((text_match, heading_match)))
    hits = search(database, "beacon")
    assert len(hits) == 2
    assert hits[0].score == hits[1].score
    assert [hit.chunk.id for hit in hits] == sorted([heading_match.id, text_match.id])


def test_duplicate_terms_and_matching_multiple_tokens_do_not_duplicate_hits(tmp_path: Path) -> None:
    chunk = synthetic_chunk("mail notice beacon")
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus((chunk,)))
    repeated = search(database, "MAIL mail notice Notice")
    unique = search(database, "mail notice")
    assert repeated == unique
    assert [hit.chunk for hit in repeated] == [chunk]


@pytest.mark.parametrize("question", ["", "—!()_ \n\t", "unmatchedword"])
def test_empty_or_nonmatching_query_has_zero_hits(tmp_path: Path, question: str) -> None:
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus((synthetic_chunk("beacon"),)))
    assert search(database, question) == ()


def test_unicode_and_special_characters_are_data_not_fts_syntax(tmp_path: Path) -> None:
    unicode_chunk = synthetic_chunk("préavis Straße 中文１２")
    expression_chunk = synthetic_chunk("OR")
    unrelated = synthetic_chunk("unrelated")
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus((unicode_chunk, expression_chunk, unrelated)))
    assert [hit.chunk for hit in search(database, '"préavis" (中文１２)')] == [unicode_chunk]
    assert [hit.chunk for hit in search(database, "OR")] == [expression_chunk]
    assert search(database, '"; DROP TABLE chunks; --') == ()
    assert inspect_index(database).chunk_count == 3


def test_queries_are_read_only_and_no_index_is_created_on_missing_file(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="Unavailable or invalid"):
        search(database, "beacon")
    assert not database.exists()
    build_index(database, SyntheticCorpus((synthetic_chunk("beacon"),)))
    before = database.read_bytes()
    database.chmod(0o444)
    assert len(search(database, "beacon")) == 1
    assert database.read_bytes() == before


def test_metadata_records_identity_and_sqlite_version(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "index.sqlite3"
    result = build_index(database, SyntheticCorpus((synthetic_chunk("beacon"),)))
    assert result == inspect_index(database)
    assert result.corpus_hash == SYNTHETIC_HASH
    assert result.sqlite_version == sqlite3.sqlite_version
    assert result.chunk_count == 1
    assert len(search(database, "beacon", expected_corpus_hash=SYNTHETIC_HASH)) == 1
    with pytest.raises(ValueError, match="does not match"):
        search(database, "beacon", expected_corpus_hash="a" * 64)


def test_empty_synthetic_corpus_can_be_rebuilt(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    assert build_index(database, SyntheticCorpus(())).chunk_count == 0
    assert search(database, "beacon") == ()


def test_duplicate_chunk_id_rejected_without_replacing_valid_index(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    chunk = synthetic_chunk("beacon")
    build_index(database, SyntheticCorpus((chunk,)))
    original = database.read_bytes()
    with pytest.raises(ValueError, match="must be unique"):
        build_index(database, SyntheticCorpus((chunk, chunk)))
    assert database.read_bytes() == original
    assert len(search(database, "beacon")) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM metadata",
        "UPDATE metadata SET schema_version = 99",
        "UPDATE metadata SET chunk_count = 2",
        "DELETE FROM chunks",
        "DELETE FROM chunk_search",
        "INSERT INTO chunk_search SELECT id, heading, text FROM chunk_search",
        "UPDATE chunk_search SET text = 'wrong contents'",
        "DROP TABLE chunk_search",
    ],
)
def test_incomplete_or_wrong_database_rejected(tmp_path: Path, mutation: str) -> None:
    database = tmp_path / "index.sqlite3"
    build_index(database, SyntheticCorpus((synthetic_chunk("beacon"),)))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(mutation)
    with pytest.raises(ValueError):
        inspect_index(database)
    with pytest.raises(ValueError):
        search(database, "beacon")


def test_two_clean_offline_rebuilds_have_identical_logical_data_results_and_scores(
    tmp_path: Path,
) -> None:
    chunks = (
        synthetic_chunk("beacon oak", part=1),
        synthetic_chunk("beacon elm", part=2),
        synthetic_chunk("birch maple", part=3),
    )
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    metadata_one = build_index(first, SyntheticCorpus(chunks))
    metadata_two = build_index(second, SyntheticCorpus(tuple(reversed(chunks))))
    assert metadata_one == metadata_two
    assert metadata_one.sqlite_version == sqlite3.sqlite_version
    with closing(sqlite3.connect(first)) as one, closing(sqlite3.connect(second)) as two:
        for table in ("metadata", "chunks", "chunk_search"):
            assert (
                one.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                == two.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            )
    for query in ("beacon", "oak birch", "BEACON beacon elm", "", "notpresent"):
        assert search(first, query) == search(second, query)


def test_query_cli_uses_existing_offline_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "index.sqlite3"
    chunk = synthetic_chunk("beacon")
    build_index(database, SyntheticCorpus((chunk,)))
    main(["query", str(database), "beacon"])
    result = json.loads(capsys.readouterr().out)
    assert result["metadata"]["corpus_hash"] == SYNTHETIC_HASH
    assert result["hits"][0]["chunk"] == chunk.model_dump(mode="json")
    assert isinstance(result["hits"][0]["score"], float)


def test_rebuild_cli_loads_corpus_and_writes_requested_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = SyntheticCorpus((synthetic_chunk("beacon"),))
    monkeypatch.setattr("rent_navigator.index.load_corpus", lambda: corpus)
    monkeypatch.chdir(tmp_path)
    main(["rebuild", str(tmp_path / "derived" / "index.sqlite3")])
    metadata = json.loads(capsys.readouterr().out)
    assert metadata == {
        "corpus_hash": SYNTHETIC_HASH,
        "sqlite_version": sqlite3.sqlite_version,
        "chunk_count": 1,
        "schema_version": 1,
    }
    assert len(search(tmp_path / "derived" / "index.sqlite3", "beacon")) == 1


def test_packaged_corpus_rebuilds_offline_with_identical_rows_and_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    corpus = load_corpus()
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    first_metadata = build_index(first)
    second_metadata = build_index(second)
    assert first_metadata == second_metadata
    assert first_metadata.corpus_hash == corpus.corpus_hash
    assert first_metadata.chunk_count == len(corpus.chunks)
    assert first_metadata.sqlite_version == sqlite3.sqlite_version
    with closing(sqlite3.connect(first)) as one, closing(sqlite3.connect(second)) as two:
        for table in ("metadata", "chunks", "chunk_search"):
            assert (
                one.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                == two.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            )
    for question in ("tenant", NOTICE_QUERY, RENT_QUERY):
        hits = search(first, question, expected_corpus_hash=corpus.corpus_hash)
        assert hits == search(second, question, expected_corpus_hash=corpus.corpus_hash)
        assert 1 <= len(hits) <= 5
        assert len(hits) == len({hit.chunk.id for hit in hits})
        assert [(hit.score, hit.chunk.id) for hit in hits] == sorted(
            (hit.score, hit.chunk.id) for hit in hits
        )
        for hit in hits:
            assert hit.chunk == corpus.chunk(hit.chunk.id)
            citation = corpus.citation(hit.chunk.id)
            assert citation.id == hit.chunk.id
            assert citation.heading == hit.chunk.heading
            assert citation.snapshot_date == corpus.snapshot_date
