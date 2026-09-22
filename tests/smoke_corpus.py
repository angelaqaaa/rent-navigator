"""Run against an installed wheel outside its checkout, with no network or credentials."""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_args

import rent_navigator
from rent_navigator.corpus import SOURCE_URLS, load_corpus
from rent_navigator.guards import REDACTION_POLICY_HASH, redact_text
from rent_navigator.index import NOTICE_QUERY, RENT_QUERY, build_index, search
from rent_navigator.models import ExtractRequest, RuleId
from rent_navigator.security_cases import SecurityCaseId, load_security_cases, security_cases_hash


def _reject_network(event: str, arguments: tuple[object, ...]) -> None:
    if event.startswith("socket."):
        raise AssertionError("Offline corpus smoke attempted network access")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify installed offline corpus and index")
    parser.add_argument("--checkout-root", type=Path, required=True)
    arguments = parser.parse_args()
    checkout = arguments.checkout_root.resolve()
    assert not Path.cwd().resolve().is_relative_to(checkout), "Run from outside the checkout"
    assert rent_navigator.__file__ is not None
    package = Path(rent_navigator.__file__).resolve().parent
    assert "site-packages" in package.parts, "Install the wheel without an editable checkout"
    assert not package.is_relative_to(checkout / "src"), "Source tree import is not a wheel smoke"
    sys.addaudithook(_reject_network)
    corpus = load_corpus()
    assert len(corpus.sources) == 6
    assert {source.source_id for source in corpus.sources} == set(SOURCE_URLS)
    assert corpus.snapshot_date == min(source.fetched_at_utc.date() for source in corpus.sources)
    assert {rule.id for rule in corpus.rules} == set(get_args(RuleId))
    for rule in corpus.rules:
        assert corpus.rule(rule.id) == rule
        assert rule.evidence_ids
        for chunk_id in rule.evidence_ids:
            chunk = corpus.chunk(chunk_id)
            citation = corpus.citation(chunk_id)
            assert citation.id == chunk.id
            assert citation.url == SOURCE_URLS[chunk.source_id]
            assert citation.heading == chunk.heading
            assert citation.snapshot_date == corpus.snapshot_date
    cases = load_security_cases(corpus=corpus)
    assert tuple(case.id for case in cases) == get_args(SecurityCaseId)
    assert (
        security_cases_hash()
        == sha256(files("rent_navigator").joinpath("security_cases.jsonl").read_bytes()).hexdigest()
    )
    contact_case = cases[5].request
    assert isinstance(contact_case, ExtractRequest)
    redacted = redact_text(contact_case.letter)
    assert redacted == (
        "Contact [EMAIL] at [PHONE]; postal code [POSTAL]. The current rent is $1,234.50, "
        "the proposed rent is $1,259.80, effective 2027-04-01."
    )
    assert redact_text(redacted) == redacted
    with TemporaryDirectory(prefix="rent-navigator-smoke-") as directory:
        first, second = Path(directory) / "first.sqlite3", Path(directory) / "second.sqlite3"
        assert not first.is_relative_to(package)
        first_metadata, second_metadata = build_index(first), build_index(second)
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
                assert corpus.chunk(hit.chunk.id) == hit.chunk
                assert corpus.citation(hit.chunk.id).id == hit.chunk.id
        assert search(first, "") == search(second, "") == ()
    print(
        json.dumps(
            {
                "corpus_hash": corpus.corpus_hash,
                "snapshot_date": corpus.snapshot_date.isoformat(),
                "chunk_count": len(corpus.chunks),
                "source_count": len(corpus.sources),
                "chunks_by_source": dict(
                    sorted(Counter(c.source_id for c in corpus.chunks).items())
                ),
                "rule_count": len(corpus.rules),
                "sqlite_version": sqlite3.sqlite_version,
                "python_version": sys.version.split()[0],
                "logical_rebuilds_equal": True,
                "ordered_ids_and_scores_equal": True,
                "rules_and_citations_resolve": True,
                "outside_checkout": True,
                "installed_package": True,
                "network_access": False,
                "security_case_count": len(cases),
                "security_cases_hash": security_cases_hash(),
                "redaction_policy_hash": REDACTION_POLICY_HASH,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
