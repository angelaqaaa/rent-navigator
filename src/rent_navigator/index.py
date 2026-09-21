"""Offline, deterministic FTS5 retrieval over the committed corpus."""

import argparse
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Protocol

from pydantic import TypeAdapter

from rent_navigator.corpus import Chunk, load_corpus
from rent_navigator.models import Sha256

NOTICE_QUERY: Final = "Ontario residential rent increase notice 90 days service mail"
RENT_QUERY: Final = NOTICE_QUERY + " guideline 12 months exemption N1 N2"
_SCHEMA_VERSION: Final = 1
_MAX_HITS: Final = 5
_HASH = TypeAdapter(Sha256)


class IndexCorpus(Protocol):
    """Read-only corpus boundary, also usable with isolated synthetic fixtures."""

    @property
    def chunks(self) -> tuple[Chunk, ...]: ...

    @property
    def corpus_hash(self) -> str: ...


@dataclass(frozen=True)
class IndexMetadata:
    corpus_hash: str
    sqlite_version: str
    chunk_count: int
    schema_version: int = _SCHEMA_VERSION


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


def query_expression(question: str) -> str:
    """Quote casefolded Unicode alphanumeric tokens without rewriting the question."""
    tokens = dict.fromkeys(token.casefold() for token in re.findall(r"[^\W_]+", question))
    return " OR ".join(f'"{token}"' for token in tokens)


def build_index(destination: Path, corpus: IndexCorpus | None = None) -> IndexMetadata:
    """Atomically rebuild a derived database at an explicitly writable destination."""
    source = load_corpus() if corpus is None else corpus
    corpus_hash = _HASH.validate_python(source.corpus_hash)
    chunks = tuple(sorted(source.chunks, key=lambda chunk: chunk.id))
    if len({chunk.id for chunk in chunks}) != len(chunks):
        raise ValueError("Corpus chunk IDs must be unique")
    metadata = IndexMetadata(corpus_hash, sqlite3.sqlite_version, len(chunks))
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    corpus_hash TEXT NOT NULL,
                    sqlite_version TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE chunks (
                    id TEXT PRIMARY KEY NOT NULL,
                    source_id TEXT NOT NULL,
                    heading TEXT NOT NULL,
                    part INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    word_count INTEGER NOT NULL
                );
                CREATE VIRTUAL TABLE chunk_search USING fts5(
                    id UNINDEXED, heading, text, tokenize='unicode61'
                );
                """
            )
            connection.executemany(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        chunk.id,
                        chunk.source_id,
                        chunk.heading,
                        chunk.part,
                        chunk.text,
                        chunk.word_count,
                    )
                    for chunk in chunks
                ),
            )
            connection.executemany(
                "INSERT INTO chunk_search (id, heading, text) VALUES (?, ?, ?)",
                ((chunk.id, chunk.heading, chunk.text) for chunk in chunks),
            )
            connection.execute(
                "INSERT INTO metadata VALUES (1, ?, ?, ?, ?)",
                (
                    metadata.corpus_hash,
                    metadata.sqlite_version,
                    metadata.chunk_count,
                    metadata.schema_version,
                ),
            )
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return metadata


def _read_metadata(connection: sqlite3.Connection) -> IndexMetadata:
    row = connection.execute("SELECT * FROM metadata WHERE singleton = 1").fetchone()
    if row is None or row["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("Unsupported or incomplete corpus index metadata")
    corpus_hash = _HASH.validate_python(row["corpus_hash"])
    if not isinstance(row["sqlite_version"], str) or not row["sqlite_version"]:
        raise ValueError("Corpus index must record its SQLite version")
    chunk_count = row["chunk_count"]
    if not isinstance(chunk_count, int) or chunk_count < 0:
        raise ValueError("Invalid corpus index chunk count")
    actual_count = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
    search_count = connection.execute("SELECT count(*) FROM chunk_search").fetchone()[0]
    unique_count = connection.execute("SELECT count(DISTINCT id) FROM chunk_search").fetchone()[0]
    matching_count = connection.execute(
        """SELECT count(*) FROM chunk_search AS f JOIN chunks AS c
           ON f.id = c.id AND f.heading = c.heading AND f.text = c.text"""
    ).fetchone()[0]
    if any(
        count != chunk_count for count in (actual_count, search_count, unique_count, matching_count)
    ):
        raise ValueError("Incomplete or inconsistent corpus index contents")
    return IndexMetadata(corpus_hash, row["sqlite_version"], chunk_count)


def inspect_index(index_path: Path) -> IndexMetadata:
    """Read metadata without creating, modifying, or refreshing the database."""
    try:
        with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return _read_metadata(db)
    except sqlite3.DatabaseError as error:
        raise ValueError("Unavailable or invalid corpus index") from error


def search(
    index_path: Path, question: str, *, expected_corpus_hash: str | None = None
) -> tuple[SearchHit, ...]:
    """Return at most five distinct chunks by default-weight BM25, then chunk ID."""
    expression = query_expression(question)
    try:
        with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            metadata = _read_metadata(db)
            if expected_corpus_hash is not None and metadata.corpus_hash != expected_corpus_hash:
                raise ValueError("Corpus index does not match the expected corpus hash")
            if not expression:
                return ()
            rows = db.execute(
                """SELECT c.*, bm25(chunk_search) AS score
                   FROM chunk_search JOIN chunks AS c ON c.id = chunk_search.id
                   WHERE chunk_search MATCH ?
                   ORDER BY score ASC, c.id ASC LIMIT ?""",
                (expression, _MAX_HITS),
            ).fetchall()
            hits = []
            for row in rows:
                fields = dict(row)
                score = fields.pop("score")
                hits.append(SearchHit(Chunk.model_validate(fields), float(score)))
            return tuple(hits)
    except sqlite3.DatabaseError as error:
        raise ValueError("Unavailable or invalid corpus index") from error


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Rebuild or query the offline corpus index")
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser("rebuild", help="Rebuild from committed package data")
    rebuild.add_argument("database", type=Path, help="Writable derived SQLite destination")
    query = commands.add_parser("query", help="Query an existing offline database")
    query.add_argument("database", type=Path)
    query.add_argument("question")
    arguments = parser.parse_args(argv)
    if arguments.command == "rebuild":
        print(json.dumps(asdict(build_index(arguments.database)), sort_keys=True))
    else:
        metadata = inspect_index(arguments.database)
        hits = search(arguments.database, arguments.question)
        print(
            json.dumps(
                {
                    "metadata": asdict(metadata),
                    "hits": [
                        {"chunk": hit.chunk.model_dump(mode="json"), "score": hit.score}
                        for hit in hits
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
