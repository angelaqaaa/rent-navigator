"""Offline extraction checks using official snapshots and independent synthetic markup."""

import json
from dataclasses import FrozenInstanceError
from importlib.resources import files
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from pypdf import PdfWriter

from rent_navigator.corpus import (
    GUIDELINE_HEADINGS,
    LTB_HEADINGS,
    RAW_EXTENSIONS,
    Chunk,
    SourceId,
    chunk_section,
    normalize_text,
)
from rent_navigator.corpus._extract import ExtractedSource, _blocks, extract_source

SOURCES: tuple[SourceId, ...] = ("rta", "legislation_act", "guideline", "ltb_guide", "n1", "n2")


def original(source_id: SourceId) -> bytes:
    return (
        files("rent_navigator.corpus")
        .joinpath(f"raw/{source_id}.{RAW_EXTENSIONS[source_id]}")
        .read_bytes()
    )


@pytest.mark.parametrize("source_id", SOURCES)
def test_committed_original_extraction_is_deterministic_and_normalized(source_id: SourceId) -> None:
    extracted = extract_source(source_id, original(source_id))
    assert extracted == extract_source(source_id, original(source_id))
    assert extracted.text == normalize_text(extracted.text)
    assert len(extracted.sections) == len(dict(extracted.sections))
    full_text = " ".join(extracted.text.split())
    for heading, text in extracted.sections:
        assert heading == normalize_text(heading)
        assert text == normalize_text(text)
        position = 0
        for paragraph in text.split("\n\n"):
            passage = " ".join(paragraph.split())
            start = full_text.find(passage, position)
            assert start >= 0
            position = start + len(passage)
        assert all(chunk.word_count <= 500 for chunk in chunk_section(source_id, heading, text))


@pytest.mark.parametrize("source_id", SOURCES)
def test_committed_text_and_chunks_reproduce_from_original_bytes(source_id: SourceId) -> None:
    corpus = files("rent_navigator.corpus")
    extracted = extract_source(source_id, original(source_id))
    assert corpus.joinpath(f"text/{source_id}.txt").read_bytes() == (extracted.text + "\n").encode()
    committed = [
        Chunk.model_validate_json(line)
        for line in corpus.joinpath("chunks.jsonl").read_text().splitlines()
        if json.loads(line)["source_id"] == source_id
    ]
    rebuilt = [
        chunk
        for heading, section in extracted.sections
        for chunk in chunk_section(source_id, heading, section)
    ]
    assert committed == rebuilt


def test_html_block_extraction_preserves_lists_tables_unicode_and_no_duplicates() -> None:
    synthetic = BeautifulSoup(
        "<div><h2>Cafe\u0301</h2><p>First <em>paragraph</em>.</p>"
        '<ol start="3"><li>Outer<ul><li>Inner</li></ul></li>'
        '<li value="8"><p>Last item</p></li></ol>'
        "<table><tr><th>Year</th><th>Rate</th></tr>"
        "<tr><td><p>2027</p></td><td>1.9%</td></tr></table>"
        "<!-- hidden old material --><script>untrusted script</script>"
        "<div>End<br>Contact</div></div>",
        "html.parser",
    )
    assert _blocks(synthetic) == [
        ("h2", "Café"),
        ("p", "First paragraph."),
        ("text", "3. Outer"),
        ("text", "Inner"),
        ("p", "8. Last item"),
        ("row", "Year | Rate"),
        ("row", "2027 | 1.9%"),
        ("text", "End"),
        ("text", "Contact"),
    ]


def synthetic_statute() -> bytes:
    return json.dumps(
        {
            "title": "Legislation Act, 2006, S.O. 2006, c. 21, Sched. F",
            "alias": "statute/06l21",
            "state": "current",
            "description": (
                "Consolidation Period: From a synthetic date to a synthetic currency date."
            ),
            "content": '<div class="WordSection1"><p class="headnote">Calendar</p>'
            '<p class="section"><b>89 </b>Current first paragraph.</p>'
            '<p class="Pnote">Note: future amendment.</p>'
            '<p class="Ysubsection">Future replacement.</p>'
            '<p class="headnote">Days</p><p class="subsection">(2) Current second paragraph.</p>'
            '<p class="footnoteLeft">Amendment history.</p>'
            '<p class="headnote">Next section</p>'
            '<p class="section"><b>90 </b>Not selected.</p></div>',
        }
    ).encode()


def test_statutory_selection_keeps_current_text_but_full_text_retains_annotations() -> None:
    extracted = extract_source("legislation_act", synthetic_statute())
    assert extracted.sections == (
        (
            "Legislation Act s. 89",
            "Calendar\n\n89 Current first paragraph.\n\nDays\n\n(2) Current second paragraph.",
        ),
    )
    assert "future amendment" in extracted.text
    assert "Future replacement" in extracted.text
    assert "Amendment history" in extracted.text
    assert "Not selected" in extracted.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "historical"),
        ("alias", "statute/wrong"),
        ("description", "not a consolidation period"),
    ],
)
def test_statute_rejects_wrong_current_source_metadata(field: str, value: str) -> None:
    data = json.loads(synthetic_statute())
    data[field] = value
    with pytest.raises(ValueError):
        extract_source("legislation_act", json.dumps(data).encode())


def test_statute_rejects_missing_selected_section() -> None:
    with pytest.raises(ValueError, match="missing"):
        extract_source("legislation_act", synthetic_statute().replace(b">89 ", b">88 "))


@pytest.mark.parametrize("source_id", ["guideline", "ltb_guide"])
def test_html_selection_exact_allowlist_and_missing_heading_rejected(source_id: SourceId) -> None:
    extracted = extract_source(source_id, original(source_id))
    expected = GUIDELINE_HEADINGS if source_id == "guideline" else LTB_HEADINGS
    assert set(dict(extracted.sections)) == expected
    heading = "Rules for rent increase" if source_id == "guideline" else "About the "
    with pytest.raises(ValueError, match="missing"):
        extract_source(
            source_id, original(source_id).replace(heading.encode(), b"Removed heading ")
        )


@pytest.mark.parametrize("source_id", SOURCES)
def test_error_page_is_rejected_as_a_snapshot(source_id: SourceId) -> None:
    with pytest.raises(ValueError):
        extract_source(source_id, b"<html><title>Error</title><body>Access denied</body></html>")


@pytest.mark.parametrize("source_id", ["n1", "n2"])
def test_pdf_retains_cover_and_all_instruction_sections(source_id: SourceId) -> None:
    extracted = extract_source(source_id, original(source_id))
    assert extracted.sections[0][0].endswith("Cover and contents")
    assert "Instructions" in extracted.sections[0][1]
    assert len(extracted.sections) == (7 if source_id == "n1" else 6)
    with pytest.raises(ValueError, match="complete PDF"):
        extract_source(source_id, original(source_id)[:-1024])


def test_pdf_with_missing_pages_is_rejected() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="unexpected pages"):
        extract_source("n1", output.getvalue())


def test_extracted_result_is_immutable() -> None:
    extracted = ExtractedSource("synthetic", None, "synthetic", ())
    with pytest.raises(FrozenInstanceError):
        extracted.title = "modified"  # type: ignore[misc]
