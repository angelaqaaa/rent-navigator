"""Immutable official snapshots, deterministic chunks and offline integrity checks."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self, get_args
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator

from rent_navigator.models import Citation, ISODate, RuleId, Sha256, StrictModel

SourceId = Literal["rta", "legislation_act", "guideline", "ltb_guide", "n1", "n2"]
SOURCE_URLS: Final = MappingProxyType(
    {
        "rta": "https://www.ontario.ca/laws/statute/06r17",
        "legislation_act": "https://www.ontario.ca/laws/statute/06l21",
        "guideline": "https://www.ontario.ca/page/residential-rent-increases",
        "ltb_guide": (
            "https://tribunalsontario.ca/documents/ltb/Brochures/"
            "Guide%20to%20RTA%20%28English%29.html"
        ),
        "n1": (
            "https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase"
            "%20%26%20Instructions/N1%20instructions_final_Nov30_2015.pdf"
        ),
        "n2": (
            "https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase"
            "%20%26%20Instructions/N2%20instructions_final_Nov30_2015.pdf"
        ),
    }
)
RTA_SECTIONS: Final = frozenset(
    {
        "5",
        "6",
        "6.1",
        "7",
        "36.1",
        "116",
        "117",
        "119",
        "120",
        "121",
        "122",
        "123",
        "126",
        "127",
        "135.1",
        "136",
        "191",
    }
)
RAW_EXTENSIONS: Final = MappingProxyType(
    {
        "rta": "json",
        "legislation_act": "json",
        "guideline": "html",
        "ltb_guide": "html",
        "n1": "pdf",
        "n2": "pdf",
    }
)
GUIDELINE_HEADINGS: Final = frozenset(
    {
        "Rules for rent increase",
        "Rent increase guideline",
        "Exceptions",
        "How we calculate the guideline",
        "A sample calculation of a rent increase",
        "Previous rent increase guidelines",
        "Resolving issues about rent control",
        "New buildings and additions",
        "New units in existing houses",
    }
)
LTB_HEADINGS: Final = frozenset(
    {
        "About the LTB",
        "Increasing a tenant's rent",
        "Seasonal rent increases",
        "The rent increase guideline",
    }
)
DATA_PATHS: Final = tuple(
    sorted(
        ["manifest.json", "chunks.jsonl", "rules.json"]
        + [f"raw/{source}.{RAW_EXTENSIONS[source]}" for source in SOURCE_URLS]
        + [f"text/{source}.txt" for source in SOURCE_URLS]
    )
)
_Nonempty = Annotated[str, Field(min_length=1, pattern=r"\S")]


def normalize_text(text: str) -> str:
    """Normalize characters and whitespace without changing paragraph content."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n")
    text = re.sub(r"[\r\v\f\x85\u2028\u2029]", "\n", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return "\n".join(line.strip(" ") for line in text.split("\n")).strip(" \n")


def chunk_id(canonical_url: str, heading: str, part: int, text: str) -> str:
    """Hash the exact canonical UTF-8 identity, without an appended newline."""
    return sha256("\n".join((canonical_url, heading, str(part), text)).encode("utf-8")).hexdigest()


class _FrozenModel(StrictModel):
    model_config = ConfigDict(frozen=True, revalidate_instances="always")


class Source(_FrozenModel):
    source_id: SourceId
    canonical_url: str
    retrieved_url: str
    title: _Nonempty
    fetched_at_utc: datetime
    consolidation_period: _Nonempty | None
    raw_sha256: Sha256
    text_sha256: Sha256

    @model_validator(mode="after")
    def canonical_metadata(self) -> Self:
        if self.canonical_url != SOURCE_URLS[self.source_id]:
            raise ValueError("source URL must equal its canonical URL")
        retrieved = urlsplit(self.retrieved_url)
        hostname = retrieved.hostname or ""
        official = any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in ("ontario.ca", "tribunalsontario.ca")
        )
        if (
            retrieved.scheme != "https"
            or not official
            or retrieved.username is not None
            or retrieved.password is not None
            or retrieved.port not in {None, 443}
            or retrieved.fragment
        ):
            raise ValueError("retrieved URL must be an official HTTPS source")
        if self.fetched_at_utc.utcoffset() != timedelta(0):
            raise ValueError("retrieval timestamp must be timezone-aware UTC")
        legislative = self.source_id in {"rta", "legislation_act"}
        if legislative != (self.consolidation_period is not None):
            raise ValueError("only legislative sources require a consolidation period")
        return self


class Chunk(_FrozenModel):
    id: Sha256
    source_id: SourceId
    heading: _Nonempty
    part: Annotated[int, Field(gt=0)]
    text: _Nonempty
    word_count: Annotated[int, Field(ge=1, le=500)]

    @model_validator(mode="after")
    def canonical_chunk(self) -> Self:
        if normalize_text(self.text) != self.text:
            raise ValueError("chunk text must already be normalized")
        if normalize_text(self.heading) != self.heading or "\n" in self.heading:
            raise ValueError("chunk heading must be normalized on one line")
        if self.word_count != len(self.text.split()):
            raise ValueError("chunk word count does not match text")
        if self.id != chunk_id(SOURCE_URLS[self.source_id], self.heading, self.part, self.text):
            raise ValueError("chunk ID does not match canonical content")
        return self


def chunk_section(source_id: SourceId, heading: str, text: str) -> list[Chunk]:
    """Pack whole paragraphs within a section; split only oversized paragraphs."""
    heading, text = normalize_text(heading), normalize_text(text)
    parts: list[str] = []
    paragraphs: list[str] = []
    count = 0
    for paragraph in re.split(r"\n[ \t]*\n+", text):
        words = paragraph.split()
        if not words:
            continue
        if len(words) > 500:
            if paragraphs:
                parts.append("\n\n".join(paragraphs))
                paragraphs, count = [], 0
            parts.extend(
                " ".join(words[start : start + 500]) for start in range(0, len(words), 500)
            )
        else:
            if count + len(words) > 500:
                parts.append("\n\n".join(paragraphs))
                paragraphs, count = [], 0
            paragraphs.append(paragraph)
            count += len(words)
    if paragraphs:
        parts.append("\n\n".join(paragraphs))
    return [
        Chunk(
            id=chunk_id(SOURCE_URLS[source_id], heading, part, content),
            source_id=source_id,
            heading=heading,
            part=part,
            text=content,
            word_count=len(content.split()),
        )
        for part, content in enumerate(parts, start=1)
    ]


class Rule(_FrozenModel):
    id: RuleId
    value: int | _Nonempty
    unit: _Nonempty
    description: _Nonempty
    evidence_ids: Annotated[tuple[Sha256, ...], Field(min_length=1)]

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("rule evidence IDs must be unique")
        return value


class _Manifest(_FrozenModel):
    snapshot_date: ISODate
    sources: tuple[Source, ...]

    @model_validator(mode="after")
    def complete_sources(self) -> Self:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != 6 or set(source_ids) != set(SOURCE_URLS):
            raise ValueError("manifest must contain each of the six sources exactly once")
        if self.snapshot_date != min(source.fetched_at_utc.date() for source in self.sources):
            raise ValueError("snapshot date must be the earliest UTC retrieval date")
        return self


@dataclass(frozen=True)
class Corpus:
    sources: tuple[Source, ...]
    chunks: tuple[Chunk, ...]
    rules: tuple[Rule, ...]
    snapshot_date: date
    corpus_hash: str

    def chunk(self, chunk_id: str) -> Chunk:
        for chunk in self.chunks:
            if chunk.id == chunk_id:
                return chunk
        raise KeyError(chunk_id)

    def rule(self, rule_id: RuleId) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)

    def citation(self, chunk_id: str) -> Citation:
        chunk = self.chunk(chunk_id)
        return Citation(
            id=chunk.id,
            url=SOURCE_URLS[chunk.source_id],
            heading=chunk.heading,
            snapshot_date=self.snapshot_date,
        )


def corpus_hash(root: Traversable | Path | None = None) -> str:
    """Hash the fifteen formal data files; ignore derived indexes and code."""
    resource = root if root is not None else files(__package__)
    entries = (
        f"{path}\n{sha256(resource.joinpath(path).read_bytes()).hexdigest()}\n"
        for path in DATA_PATHS
    )
    return sha256("".join(entries).encode("utf-8")).hexdigest()


def _section(chunk: Chunk) -> str | None:
    if chunk.source_id == "rta":
        match = re.match(r"^RTA s\. ([0-9]+(?:\.[0-9]+)?)(?=$|[ :—–-])", chunk.heading)
        if match is None or match[1] not in RTA_SECTIONS:
            raise ValueError("chunk is outside the selected RTA sections")
        return match[1]
    if chunk.source_id == "legislation_act":
        if re.match(r"^Legislation Act s\. 89(?=$|[ :—–-])", chunk.heading) is None:
            raise ValueError("chunk is outside Legislation Act section 89")
        return "89"
    return None


def _validate_chunks(chunks: tuple[Chunk, ...]) -> None:
    if not chunks or len({chunk.id for chunk in chunks}) != len(chunks):
        raise ValueError("corpus requires nonempty unique chunks")
    if {chunk.source_id for chunk in chunks} != set(SOURCE_URLS):
        raise ValueError("chunks must cover all six sources")
    parts: dict[tuple[str, str], list[int]] = {}
    sections: set[str] = set()
    for chunk in chunks:
        section = _section(chunk)
        if chunk.source_id == "guideline" and chunk.heading not in GUIDELINE_HEADINGS:
            raise ValueError("chunk is outside selected guideline sections")
        if chunk.source_id == "ltb_guide" and chunk.heading not in LTB_HEADINGS:
            raise ValueError("chunk is outside selected LTB guide sections")
        if chunk.source_id == "rta" and section is not None:
            sections.add(section)
        parts.setdefault((chunk.source_id, chunk.heading), []).append(chunk.part)
    if sections != RTA_SECTIONS:
        raise ValueError("chunks must cover every selected RTA section")
    for source_id, required_headings in (
        ("guideline", GUIDELINE_HEADINGS),
        ("ltb_guide", LTB_HEADINGS),
    ):
        actual_headings = {chunk.heading for chunk in chunks if chunk.source_id == source_id}
        if actual_headings != required_headings:
            raise ValueError("chunks must cover every selected guidance section")
    if any(sorted(numbers) != list(range(1, len(numbers) + 1)) for numbers in parts.values()):
        raise ValueError("section part numbers must be consecutive starting with one")


def _validate_rules(rules: tuple[Rule, ...], chunks: tuple[Chunk, ...]) -> None:
    if len(rules) != len(get_args(RuleId)) or {rule.id for rule in rules} != set(get_args(RuleId)):
        raise ValueError("rules must contain each RuleId exactly once")
    by_id = {chunk.id: chunk for chunk in chunks}
    constants: dict[RuleId, tuple[int | str, str]] = {
        "notice.90_days": (90, "calendar_days"),
        "notice.mail_5_days": (5, "calendar_days"),
        "spacing.12_months": (12, "calendar_months"),
        "guideline.2026": ("2.1", "percent"),
        "guideline.2027": ("1.9", "percent"),
        "form.N1": ("N1", "form"),
        "form.N2": ("N2", "form"),
    }
    required: dict[RuleId, set[tuple[str, str | None]]] = {
        "notice.90_days": {("rta", "116")},
        "notice.mail_5_days": {("rta", "191")},
        "spacing.12_months": {("rta", "119")},
        "guideline.2026": {("rta", "120"), ("guideline", None)},
        "guideline.2027": {("rta", "120"), ("guideline", None)},
        "exemption.s6_1": {("rta", "6.1")},
        "calendar.s89": {("legislation_act", "89")},
        "form.N1": {("n1", None)},
        "form.N2": {("n2", None)},
        "scope.ordinary": {("rta", "5"), ("rta", "6"), ("rta", "7")},
    }
    for rule in rules:
        if rule.id in constants and (rule.value, rule.unit) != constants[rule.id]:
            raise ValueError("rule constant or unit differs from the approved contract")
        if rule.id not in constants and (
            not isinstance(rule.value, str) or rule.unit != "semantic"
        ):
            raise ValueError("semantic rules require a string value and semantic unit")
        if any(chunk_id not in by_id for chunk_id in rule.evidence_ids):
            raise ValueError("rule evidence ID does not resolve")
        evidence = {by_id[chunk_id] for chunk_id in rule.evidence_ids}
        locations = {(chunk.source_id, _section(chunk)) for chunk in evidence}
        if not required[rule.id] <= locations:
            raise ValueError("rule evidence lacks its required source passages")


def load_corpus(root: Path | None = None) -> Corpus:
    """Validate and read committed package data, entirely offline and independent of cwd."""
    resource = root if root is not None else files(__package__)
    manifest = _Manifest.model_validate_json(resource.joinpath("manifest.json").read_bytes())
    source_texts: dict[str, str] = {}
    for source in manifest.sources:
        extension = RAW_EXTENSIONS[source.source_id]
        raw = resource.joinpath(f"raw/{source.source_id}.{extension}").read_bytes()
        text = resource.joinpath(f"text/{source.source_id}.txt").read_bytes()
        if sha256(raw).hexdigest() != source.raw_sha256:
            raise ValueError("raw snapshot hash mismatch")
        if sha256(text).hexdigest() != source.text_sha256:
            raise ValueError("extracted text hash mismatch")
        decoded = text.decode("utf-8")
        if not decoded.strip() or normalize_text(decoded) != decoded.rstrip("\n"):
            raise ValueError("extracted text must be nonempty and normalized")
        source_texts[source.source_id] = " ".join(decoded.split())
    chunks = tuple(
        Chunk.model_validate_json(line)
        for line in resource.joinpath("chunks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    _validate_chunks(chunks)
    for chunk in chunks:
        position = 0
        source_text = source_texts[chunk.source_id]
        for paragraph in re.split(r"\n\n+", chunk.text):
            passage = " ".join(paragraph.split())
            start = source_text.find(passage, position)
            if start < 0:
                raise ValueError("chunk text is not a faithful extract of its source")
            position = start + len(passage)
    rules = TypeAdapter(tuple[Rule, ...]).validate_json(
        resource.joinpath("rules.json").read_bytes()
    )
    _validate_rules(rules, chunks)
    return Corpus(manifest.sources, chunks, rules, manifest.snapshot_date, corpus_hash(resource))
