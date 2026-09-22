"""Synthetic fixture wire boundaries and explicit package-resource loading."""

import json
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from rent_navigator import security_cases
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.models import ExtractRequest, NoticeRequest, QuestionRequest, RentRequest
from rent_navigator.security_cases import (
    SecurityAssertion,
    SecurityCase,
    SecurityCaseId,
    load_security_cases,
    security_cases_hash,
)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture
def rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in files("rent_navigator")
        .joinpath("security_cases.jsonl")
        .read_bytes()
        .splitlines()
    ]


def _replace_resource(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes) -> None:
    (tmp_path / "security_cases.jsonl").write_bytes(content)
    monkeypatch.setattr(security_cases, "files", lambda package: tmp_path)


def _encode_rows(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row) + "\n" for row in rows).encode()


def test_packaged_rows_are_exactly_ordered_strict_requests(corpus: Corpus) -> None:
    cases = load_security_cases(corpus=corpus)
    assert isinstance(cases, tuple)
    assert tuple(case.id for case in cases) == get_args(SecurityCaseId)
    assert [case.mode for case in cases] == [
        "question",
        "question",
        "question",
        "rent",
        "question",
        "extract",
        "rent",
        "question",
    ]
    assert all(isinstance(cases[index].request, QuestionRequest) for index in (0, 1, 2, 4, 7))
    assert isinstance(cases[3].request, RentRequest)
    assert isinstance(cases[5].request, ExtractRequest)
    assert isinstance(cases[6].request, RentRequest)
    assert len({case.request.attempt_id for case in cases}) == 8
    for case in cases:
        assert SecurityCase.model_validate_json(case.model_dump_json()) == case
        assert case.expected_deterministic_assertions == sorted(
            set(case.expected_deterministic_assertions)
        )
        assert case.canonical_evidence_ids == sorted(set(case.canonical_evidence_ids))
        for chunk_id in case.canonical_evidence_ids:
            assert corpus.chunk(chunk_id).id == chunk_id


def test_policy_evidence_is_the_frozen_rule_mapping(corpus: Corpus) -> None:
    cases = load_security_cases(corpus=corpus)
    notice = sorted(corpus.rule("notice.90_days").evidence_ids)
    for index in (0, 1, 2, 4, 7):
        assert cases[index].canonical_evidence_ids == notice
    assert cases[3].canonical_evidence_ids == sorted(
        set(corpus.rule("notice.90_days").evidence_ids)
        | set(corpus.rule("spacing.12_months").evidence_ids)
        | set(corpus.rule("guideline.2026").evidence_ids)
    )
    assert cases[6].canonical_evidence_ids == sorted(corpus.rule("scope.ordinary").evidence_ids)
    assert cases[5].canonical_evidence_ids == []
    assert cases[5].required_policy_claims == []
    assert {tag for case in cases for tag in case.expected_deterministic_assertions} == set(
        get_args(SecurityAssertion)
    )


def test_fixed_rent_inputs_preserve_preset_and_exclusion(corpus: Corpus) -> None:
    cases = load_security_cases(corpus=corpus)
    rent, excluded = cases[3].request, cases[6].request
    assert isinstance(rent, RentRequest)
    assert isinstance(excluded, RentRequest)
    expected = {
        "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
        "effective_on": "2026-09-01",
        "served_on": "2026-07-03",
        "service_method": "hand",
        "current_cents": 200000,
        "proposed_cents": 204800,
        "tenancy_start": "2024-09-01",
        "last_increase": {"state": "known", "date": "2025-09-01"},
        "guideline_status": "controlled",
        "form": "N1",
    }
    assert rent.facts.model_dump(mode="json") == expected
    expected["scope"] = {"ordinary": "excluded", "period_start": "confirmed"}
    assert excluded.facts.model_dump(mode="json") == expected
    assert rent.confirmed is excluded.confirmed is True


def test_schema_has_only_required_contract_fields() -> None:
    expected = {
        "id",
        "mode",
        "request",
        "injected_retrieved_text",
        "expected_deterministic_assertions",
        "required_policy_claims",
        "canonical_evidence_ids",
    }
    schema = SecurityCase.model_json_schema()
    assert set(schema["properties"]) == expected
    assert set(schema["required"]) == expected
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("field", list(SecurityCase.model_fields))
def test_every_wire_field_is_required(rows: list[dict[str, Any]], field: str) -> None:
    row = rows[0]
    del row[field]
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "S00"),
        ("id", "S09"),
        ("id", 1),
        ("mode", "qa"),
        ("injected_retrieved_text", ""),
        ("injected_retrieved_text", "x" * 4001),
        ("injected_retrieved_text", False),
        ("injected_retrieved_text", 123),
        ("expected_deterministic_assertions", []),
        ("expected_deterministic_assertions", ["free_form_assertion"]),
        (
            "expected_deterministic_assertions",
            ["server_disclaimer_preserved", "citation_provenance_enforced"],
        ),
        ("expected_deterministic_assertions", ["citation_provenance_enforced"] * 2),
        ("expected_deterministic_assertions", "citation_provenance_enforced"),
        ("required_policy_claims", []),
        ("required_policy_claims", None),
        ("required_policy_claims", [{"id": "", "text": "Policy"}]),
        ("required_policy_claims", [{"id": "claim", "text": ""}]),
        ("required_policy_claims", [{"id": 1, "text": "Policy"}]),
        ("required_policy_claims", [{"id": "claim", "text": "Policy", "extra": True}]),
        (
            "required_policy_claims",
            [{"id": "claim", "text": "One"}, {"id": "claim", "text": "Two"}],
        ),
        ("canonical_evidence_ids", []),
        ("canonical_evidence_ids", None),
        ("canonical_evidence_ids", ["not_a_hash"]),
        ("canonical_evidence_ids", ["a" * 64, "0" * 64]),
        ("canonical_evidence_ids", ["a" * 64] * 2),
        ("canonical_evidence_ids", [123]),
    ],
)
def test_invalid_wire_fields_fail(rows: list[dict[str, Any]], field: str, value: object) -> None:
    row = rows[0]
    row[field] = value
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("mode", ["extract", "notice", "rent"])
def test_mismatched_request_mode_fails(rows: list[dict[str, Any]], mode: str) -> None:
    row = rows[0]
    row["mode"] = mode
    with pytest.raises(ValidationError, match="mode must match"):
        SecurityCase.model_validate_json(json.dumps(row))


def test_notice_mode_accepts_existing_notice_request(rows: list[dict[str, Any]]) -> None:
    row = rows[3]
    row["mode"] = row["request"]["mode"] = "notice"
    row["request"]["facts"] = {
        key: value
        for key, value in row["request"]["facts"].items()
        if key in {"scope", "effective_on", "served_on", "service_method"}
    }
    assert isinstance(SecurityCase.model_validate_json(json.dumps(row)).request, NoticeRequest)


@pytest.mark.parametrize("nested", [False, True])
def test_extra_fields_rejected(rows: list[dict[str, Any]], nested: bool) -> None:
    row = rows[0]
    target = row["request"] if nested else row
    target["letter"] = "Mixed request variants are not permitted."
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("cents", [True, "200000", 0, -1])
def test_existing_fact_wire_validation_remains_strict(
    rows: list[dict[str, Any]], cents: object
) -> None:
    row = rows[3]
    row["request"]["facts"]["current_cents"] = cents
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("field", ["current_cents", "served_on"])
def test_nullable_facts_require_explicit_null(rows: list[dict[str, Any]], field: str) -> None:
    row = rows[3]
    row["request"]["facts"][field] = None
    SecurityCase.model_validate_json(json.dumps(row))
    del row["request"]["facts"][field]
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_policy_claims", [{"id": "claim", "text": "Policy"}]),
        ("canonical_evidence_ids", ["a" * 64]),
        ("injected_retrieved_text", "Injected content"),
    ],
)
def test_s06_is_only_extraction_with_empty_claims_and_evidence(
    rows: list[dict[str, Any]], field: str, value: object
) -> None:
    row = rows[5]
    row[field] = value
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


def test_s06_cannot_be_reassigned_to_question(rows: list[dict[str, Any]]) -> None:
    row = rows[5]
    row["mode"] = "question"
    row["request"] = rows[0]["request"]
    with pytest.raises(ValidationError):
        SecurityCase.model_validate_json(json.dumps(row))


def test_extraction_cannot_receive_sidecar(rows: list[dict[str, Any]]) -> None:
    row = rows[0]
    row["mode"] = "extract"
    row["request"] = rows[5]["request"]
    row["injected_retrieved_text"] = "Injection"
    with pytest.raises(ValidationError, match="extraction cannot receive"):
        SecurityCase.model_validate_json(json.dumps(row))


@pytest.mark.parametrize("size", [1, 4000])
def test_nonempty_sidecar_boundaries(rows: list[dict[str, Any]], size: int) -> None:
    row = rows[0]
    row["injected_retrieved_text"] = "x" * size
    assert SecurityCase.model_validate_json(json.dumps(row)).injected_retrieved_text == "x" * size


@pytest.mark.parametrize("variation", ["missing", "duplicate", "order", "extra", "empty"])
def test_loader_requires_eight_unique_ordered_ids(
    rows: list[dict[str, Any]],
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variation: str,
) -> None:
    if variation == "missing":
        rows.pop()
    elif variation == "duplicate":
        rows[1] = deepcopy(rows[0])
    elif variation == "order":
        rows[0], rows[1] = rows[1], rows[0]
    elif variation == "extra":
        rows.append(deepcopy(rows[0]))
    else:
        rows = []
    _replace_resource(monkeypatch, tmp_path, _encode_rows(rows))
    with pytest.raises(ValueError, match="exactly S01 through S08 in order"):
        load_security_cases(corpus=corpus)


@pytest.mark.parametrize("malformed", [b"{", b"null\n", b"[]\n", b"\n"])
def test_loader_rejects_malformed_rows(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed: bytes,
) -> None:
    _replace_resource(monkeypatch, tmp_path, malformed)
    with pytest.raises(ValidationError):
        load_security_cases(corpus=corpus)


def test_loader_rejects_noncanonical_evidence(
    rows: list[dict[str, Any]],
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows[0]["canonical_evidence_ids"] = ["0" * 64]
    _replace_resource(monkeypatch, tmp_path, _encode_rows(rows))
    with pytest.raises(ValueError, match="evidence does not resolve"):
        load_security_cases(corpus=corpus)


def test_hash_is_of_exact_resource_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = files("rent_navigator").joinpath("security_cases.jsonl").read_bytes()
    assert security_cases_hash() == sha256(original).hexdigest()
    _replace_resource(monkeypatch, tmp_path, original + b"\n")
    assert security_cases_hash() == sha256(original + b"\n").hexdigest()
    assert security_cases_hash() != sha256(original).hexdigest()


def test_loading_is_independent_of_working_directory(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert len(load_security_cases(corpus=corpus)) == 8


def test_import_defers_resource_io_and_does_not_load_runtime_modules() -> None:
    script = """
import sys
from unittest.mock import patch
from rent_navigator.corpus import Corpus
with patch(
    'importlib.resources.files', side_effect=AssertionError('unexpected resource read')
) as access:
    import rent_navigator.security_cases
    access.assert_not_called()
for name in ('provider', 'cli', 'agent', 'extract', 'smoke_extraction'):
    assert 'rent_navigator.' + name not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
