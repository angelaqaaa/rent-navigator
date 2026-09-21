"""Deterministic extraction from the six retained official response bodies."""

import json
import re
from dataclasses import dataclass
from io import BytesIO

from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag
from pypdf import PdfReader

from rent_navigator.corpus import (
    GUIDELINE_HEADINGS,
    LTB_HEADINGS,
    RTA_SECTIONS,
    SourceId,
    normalize_text,
)


@dataclass(frozen=True)
class ExtractedSource:
    title: str
    consolidation_period: str | None
    text: str
    sections: tuple[tuple[str, str], ...]


def _classes(node: Tag) -> set[str]:
    value = node.get("class")
    if isinstance(value, str):
        return set(value.split())
    return set(value or ())


def _line(text: str) -> str:
    return normalize_text(re.sub(r"\s+", " ", text))


def _inline(node: Tag) -> str:
    return _line(node.get_text("", strip=False))


def _blocks(node: Tag) -> list[tuple[str, str]]:
    """Keep blocks once, including nested lists, table rows and unwrapped text."""
    blocks: list[tuple[str, str]] = []
    pending: list[str] = []

    def flush() -> None:
        text = _line("".join(pending))
        if text:
            blocks.append(("text", text))
        pending.clear()

    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            pending.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in {"script", "style", "nav", "form"}:
            continue
        if child.name == "br":
            flush()
        elif child.name in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            flush()
            text = _inline(child)
            if text:
                blocks.append((child.name, text))
        elif child.name == "table":
            flush()
            for row in child.find_all("tr"):
                cells = row.find_all(["th", "td"], recursive=False)
                text = " | ".join(_inline(cell) for cell in cells)
                if text:
                    blocks.append(("row", text))
        elif child.name in {"ul", "ol"}:
            flush()
            number = int(str(child.get("start", "1")))
            for item in child.find_all("li", recursive=False):
                item_blocks = _blocks(item)
                if child.name == "ol" and item_blocks:
                    number = int(str(item.get("value", str(number))))
                    kind, first = item_blocks[0]
                    item_blocks[0] = (kind, f"{number}. {first}")
                    number += 1
                blocks.extend(item_blocks)
        elif child.name in {"div", "section", "article", "main", "header", "footer"}:
            flush()
            blocks.extend(_blocks(child))
        else:
            pending.append(child.get_text("", strip=False))
    flush()
    return blocks


def _render(blocks: list[tuple[str, str]]) -> str:
    return normalize_text("\n\n".join(text for _, text in blocks))


def _selected_html(
    blocks: list[tuple[str, str]], headings: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    sections: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, (kind, text) in enumerate(blocks):
        if not kind.startswith("h") or text not in headings:
            continue
        if text in seen:
            raise ValueError("duplicate selected source heading")
        seen.add(text)
        end = next(
            (i for i in range(index + 1, len(blocks)) if blocks[i][0].startswith("h")),
            len(blocks),
        )
        if end == index + 1:
            raise ValueError("selected source section has no body")
        sections.append((text, _render(blocks[index:end])))
    if seen != headings:
        raise ValueError("source is missing required selected headings")
    return tuple(sections)


def _extract_html(source_id: SourceId, raw: bytes) -> ExtractedSource:
    soup = BeautifulSoup(raw.decode("utf-8"), "html.parser")
    title = soup.find("h1")
    expected = (
        "Residential rent increases"
        if source_id == "guideline"
        else "Brochure: A Guide to the Residential Tenancies Act"
    )
    if title is None or _inline(title) != expected:
        raise ValueError("official source title is missing or unexpected")
    body = soup.find("article" if source_id == "guideline" else "main")
    if body is None:
        raise ValueError("official article body is missing")
    for navigation in body.select(".toc__wrapper"):
        navigation.decompose()
    blocks = _blocks(body)
    if source_id == "ltb_guide":
        blocks.insert(0, ("h1", expected))
    headings = GUIDELINE_HEADINGS if source_id == "guideline" else LTB_HEADINGS
    return ExtractedSource(expected, None, _render(blocks), _selected_html(blocks, headings))


def _extract_statute(source_id: SourceId, raw: bytes) -> ExtractedSource:
    data = json.loads(raw)
    expected_title = (
        "Residential Tenancies Act, 2006, S.O. 2006, c. 17"
        if source_id == "rta"
        else "Legislation Act, 2006, S.O. 2006, c. 21, Sched. F"
    )
    expected_alias = "statute/06r17" if source_id == "rta" else "statute/06l21"
    if (
        not isinstance(data, dict)
        or data.get("title") != expected_title
        or data.get("alias") != expected_alias
        or data.get("state") != "current"
        or not isinstance(data.get("description"), str)
        or not str(data["description"]).startswith("Consolidation Period:")
        or not isinstance(data.get("content"), str)
    ):
        raise ValueError("official current statute response metadata is invalid")
    soup = BeautifulSoup(data["content"], "html.parser")
    body = soup.select_one("div.WordSection1")
    if body is None:
        raise ValueError("official statute body is missing")
    paragraphs = body.find_all("p", recursive=False)
    boundaries: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for index, paragraph in enumerate(paragraphs):
        if "section" not in _classes(paragraph):
            continue
        number_tag = paragraph.find("b")
        number = _inline(number_tag) if number_tag else ""
        if not re.fullmatch(r"\d+(?:\.\d+)*(?:-\d+)?", number) or number in seen:
            raise ValueError("statute section numbers must be valid and unique")
        seen.add(number)
        start = index
        while start > 0 and _classes(paragraphs[start - 1]) & {
            "headnote",
            "heading1",
            "partnum",
        }:
            start -= 1
        boundaries.append((number, start, index))
    selected = RTA_SECTIONS if source_id == "rta" else frozenset({"89"})
    sections: list[tuple[str, str]] = []
    for position, (number, start, index) in enumerate(boundaries):
        if number not in selected:
            continue
        end = boundaries[position + 1][1] if position + 1 < len(boundaries) else len(paragraphs)
        contents: list[str] = []
        for paragraph in paragraphs[start:end]:
            classes = _classes(paragraph)
            if any(
                name in {"Pnote", "footnoteLeft", "partnum", "heading1"} or name.startswith("Y")
                for name in classes
            ):
                continue
            text = _inline(paragraph)
            if text:
                contents.append(text)
        headings = [
            _inline(paragraph)
            for paragraph in paragraphs[start:index]
            if "headnote" in _classes(paragraph)
        ]
        if not headings or len(contents) <= len(headings):
            raise ValueError("selected statutory section has no heading or body")
        heading = (
            f"RTA s. {number} — {' / '.join(headings)}"
            if source_id == "rta"
            else "Legislation Act s. 89"
        )
        sections.append((heading, normalize_text("\n\n".join(contents))))
    if len(sections) != len(selected):
        raise ValueError("statute is missing a required selected section")
    full_blocks = [("title", expected_title), ("period", _line(data["description"]))]
    full_blocks.extend(_blocks(body))
    return ExtractedSource(
        expected_title, _line(data["description"]), _render(full_blocks), tuple(sections)
    )


def _extract_pdf(source_id: SourceId, raw: bytes) -> ExtractedSource:
    if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-1024:]:
        raise ValueError("notice instructions require a complete PDF response")
    reader = PdfReader(BytesIO(raw), strict=True)
    expected_pages = 5 if source_id == "n1" else 4
    if reader.is_encrypted or len(reader.pages) != expected_pages:
        raise ValueError("notice instructions have unexpected pages or encryption")
    pages = [normalize_text(page.extract_text()) for page in reader.pages]
    if any(not page for page in pages):
        raise ValueError("notice instructions contain an empty extracted page")
    form = source_id.upper()
    title = "Notice of Rent Increase"
    if source_id == "n2":
        title += " (Unit Partially Exempt)"
    cover = " ".join(pages[0].split())
    if f"Form {form}" not in cover or "Notice of Rent Increase" not in cover:
        raise ValueError("notice instructions have an unexpected title or form")
    if source_id == "n2" and "Partially Exempt" not in cover:
        raise ValueError("N2 instructions must identify the partial exemption")
    text = normalize_text("\n\n".join(pages))
    matches = list(re.finditer(r"(?m)^SECTION\n([A-F]) ([^\n]+)$", text))
    expected_letters = list("ABCDEF" if source_id == "n1" else "ABCDE")
    if [match[1] for match in matches] != expected_letters:
        raise ValueError("notice instructions are missing required sections")
    sections = [(f"{form} instructions — Cover and contents", text[: matches[0].start()].strip())]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            (
                f"{form} instructions — Section {match[1]}: {match[2]}",
                text[match.start() : end].strip(),
            )
        )
    return ExtractedSource(title, None, text, tuple(sections))


def extract_source(source_id: SourceId, raw: bytes) -> ExtractedSource:
    """Extract retained bytes only; never retrieve or refresh a source."""
    if source_id in {"rta", "legislation_act"}:
        return _extract_statute(source_id, raw)
    if source_id in {"guideline", "ltb_guide"}:
        return _extract_html(source_id, raw)
    if source_id in {"n1", "n2"}:
        return _extract_pdf(source_id, raw)
    raise ValueError("unsupported source identifier")
