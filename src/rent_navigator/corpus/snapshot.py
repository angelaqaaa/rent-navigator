"""Reproduce derived snapshot files from the six committed official responses."""

import argparse
import json
import re
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from rent_navigator.corpus import (
    RAW_EXTENSIONS,
    Chunk,
    Rule,
    SourceId,
    _Manifest,
    chunk_section,
    load_corpus,
)
from rent_navigator.corpus._extract import extract_source
from rent_navigator.models import RuleId


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_rules(chunks: Sequence[Chunk]) -> tuple[Rule, ...]:
    """Map fixed rule semantics to passages; never infer rules from search rankings."""

    def evidence(source: SourceId, heading: str, phrase: str | None = None) -> list[str]:
        matches = [
            chunk.id
            for chunk in chunks
            if chunk.source_id == source
            and re.search(heading, chunk.heading)
            and (phrase is None or phrase in chunk.text)
        ]
        if not matches:
            raise ValueError(f"Required rule passage is missing: {source} {heading}")
        return matches

    def statute(section: str, phrase: str | None = None) -> list[str]:
        return evidence("rta", rf"^RTA s\. {re.escape(section)}(?: —|$)", phrase)

    def rule(
        identifier: RuleId, value: int | str, unit: str, description: str, ids: list[str]
    ) -> Rule:
        return Rule(
            id=identifier,
            value=value,
            unit=unit,
            description=description,
            evidence_ids=tuple(sorted(set(ids))),
        )

    return (
        rule(
            "notice.90_days",
            90,
            "calendar_days",
            "Ordinary notice of rent increase requires at least 90 days.",
            statute("116"),
        ),
        rule(
            "notice.mail_5_days",
            5,
            "calendar_days",
            "A notice sent by mail is deemed given on the fifth day after mailing.",
            statute("191", "fifth day"),
        ),
        rule(
            "spacing.12_months",
            12,
            "calendar_months",
            "Ordinary increases require 12 months since the last increase or first rental.",
            statute("119"),
        ),
        rule(
            "guideline.2026",
            "2.1",
            "percent",
            "The 2026 guideline for a confirmed controlled tenancy is 2.1 percent.",
            statute("120", "guideline")
            + evidence("guideline", "^Previous rent increase guidelines$", "2026"),
        ),
        rule(
            "guideline.2027",
            "1.9",
            "percent",
            "The 2027 guideline for a confirmed controlled tenancy is 1.9 percent.",
            statute("120", "guideline") + evidence("guideline", "^Rent increase guideline$", "1.9"),
        ),
        rule(
            "exemption.s6_1",
            "confirmed_s6_1_exemption",
            "semantic",
            "An explicitly confirmed section 6.1 exemption excludes the guideline check; "
            "the project does not decide exemption evidence.",
            statute("6.1"),
        ),
        rule(
            "calendar.s89",
            "exclude_start_include_end_calendar_months",
            "semantic",
            "Count days excluding the first day and including the last; count calendar "
            "months by corresponding date, or the last day when no corresponding date exists.",
            evidence("legislation_act", r"^Legislation Act s\. 89(?: —|$)"),
        ),
        rule(
            "form.N1",
            "N1",
            "form",
            "Use N1 for the confirmed ordinary controlled scenario; form completeness "
            "is outside the project checks.",
            evidence("n1", r"Section [AB]:"),
        ),
        rule(
            "form.N2",
            "N2",
            "form",
            "Use N2 for a confirmed section 6.1 exemption in the ordinary scenario; "
            "the form itself does not establish exemption.",
            evidence("n2", r"Section A:") + statute("6.1", "120"),
        ),
        rule(
            "scope.ordinary",
            "confirmed_ordinary_private_residential",
            "semantic",
            "Project scope requires confirmed ordinary private residential facts and "
            "rental-period start. Excluded statutory regimes and special increases are "
            "retained as context, not implemented as additional calculators.",
            statute("5")
            + statute("6")
            + statute("7")
            + statute("36.1", "Sections 110, 116, 119 and 120")
            + statute("117")
            + statute("120", "121")
            + statute("135.1")
            + statute("136"),
        ),
    )


def derived_files(root: Path) -> dict[str, bytes]:
    """Derive text, chunks and rules offline, checking the captured manifest metadata."""
    manifest = _Manifest.model_validate_json((root / "manifest.json").read_bytes())
    output: dict[str, bytes] = {}
    chunks: list[Chunk] = []
    for source in manifest.sources:
        raw = (root / "raw" / f"{source.source_id}.{RAW_EXTENSIONS[source.source_id]}").read_bytes()
        if sha256(raw).hexdigest() != source.raw_sha256:
            raise ValueError("Original response no longer matches the captured manifest")
        extracted = extract_source(source.source_id, raw)
        if (extracted.title, extracted.consolidation_period) != (
            source.title,
            source.consolidation_period,
        ):
            raise ValueError("Source identity or consolidation does not match the manifest")
        text = (extracted.text + "\n").encode("utf-8")
        if sha256(text).hexdigest() != source.text_sha256:
            raise ValueError("Reproduced text does not match the captured manifest")
        output[f"text/{source.source_id}.txt"] = text
        for heading, section in extracted.sections:
            chunks.extend(chunk_section(source.source_id, heading, section))
    output["chunks.jsonl"] = "".join(chunk.model_dump_json() + "\n" for chunk in chunks).encode(
        "utf-8"
    )
    output["rules.json"] = _json_bytes(
        [rule.model_dump(mode="json") for rule in build_rules(chunks)]
    )
    return output


def verify_snapshot(root: Path) -> None:
    """Reject any committed derivative that cannot be reproduced from the originals."""
    for path, expected in derived_files(root).items():
        if (root / path).read_bytes() != expected:
            raise ValueError(f"Snapshot derivative is not reproducible: {path}")
    load_corpus(root)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Verify snapshot extraction entirely offline")
    parser.add_argument("snapshot", type=Path, help="Directory containing the captured manifest")
    arguments = parser.parse_args(argv)
    verify_snapshot(arguments.snapshot)
    print(
        json.dumps({"corpus_hash": load_corpus(arguments.snapshot).corpus_hash, "verified": True})
    )


if __name__ == "__main__":
    main()
