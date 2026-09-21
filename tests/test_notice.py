"""Synthetic unit boundaries; these fixtures are not approved evaluation cases."""

import builtins
import importlib
import inspect
import json
import os
import socket
import sqlite3
import time
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NoReturn

import pytest

import rent_navigator.corpus as corpus_module
import rent_navigator.notice as notice_module
from rent_navigator.corpus import Corpus, Rule, load_corpus
from rent_navigator.models import (
    NoticeFacts,
    RuleId,
    ScopeState,
    ToolResult,
    provider_tool_definitions,
)
from rent_navigator.notice import notice_deadline_check

Method = Literal["hand", "mail", "unknown"]
NOTICE_RULES = ["calendar.s89", "notice.90_days"]
MAIL_RULES = ["calendar.s89", "notice.90_days", "notice.mail_5_days"]
DERIVED_FIELDS = (
    "deemed_served_on",
    "notice_days",
    "earliest_notice_on",
    "latest_deemed_service_on",
    "latest_dispatch_on",
)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def facts(
    *,
    effective: str | None = "2026-09-01",
    served: str | None = "2026-06-03",
    method: Method = "hand",
    ordinary: ScopeState = "confirmed",
    period: ScopeState = "confirmed",
) -> NoticeFacts:
    return NoticeFacts.model_validate_json(
        json.dumps(
            {
                "scope": {"ordinary": ordinary, "period_start": period},
                "effective_on": effective,
                "served_on": served,
                "service_method": method,
            }
        )
    )


def derived(result: ToolResult) -> tuple[object, ...]:
    wire = result.model_dump(mode="json")
    return tuple(wire[field] for field in DERIVED_FIELDS)


@pytest.mark.parametrize(
    ("method", "served", "deemed", "earliest", "days", "status", "reason"),
    [
        ("hand", "2026-06-04", "2026-06-04", "2026-09-02", 89, "fail", "violated"),
        ("hand", "2026-06-03", "2026-06-03", "2026-09-01", 90, "pass", "satisfied"),
        ("hand", "2026-06-02", "2026-06-02", "2026-08-31", 91, "pass", "satisfied"),
        ("mail", "2026-05-30", "2026-06-04", "2026-09-02", 89, "fail", "violated"),
        ("mail", "2026-05-29", "2026-06-03", "2026-09-01", 90, "pass", "satisfied"),
        ("mail", "2026-05-28", "2026-06-02", "2026-08-31", 91, "pass", "satisfied"),
    ],
)
def test_89_90_91_day_boundaries(
    corpus: Corpus,
    method: Method,
    served: str,
    deemed: str,
    earliest: str,
    days: int,
    status: str,
    reason: str,
) -> None:
    result = notice_deadline_check(facts(method=method, served=served), corpus=corpus)
    assert derived(result) == (
        deemed,
        days,
        earliest,
        "2026-06-03",
        "2026-05-29" if method == "mail" else "2026-06-03",
    )
    assert (result.checks[3].status, result.checks[3].reason) == (status, reason)
    assert result.status == ("passes_checked_rules" if status == "pass" else "fails_checked_rules")


@pytest.mark.parametrize(
    ("effective", "served", "method", "expected"),
    [
        (
            "2027-01-01",
            "2026-10-03",
            "hand",
            ("2026-10-03", 90, "2027-01-01", "2026-10-03", "2026-10-03"),
        ),
        (
            "2027-01-01",
            "2026-09-28",
            "mail",
            ("2026-10-03", 90, "2027-01-01", "2026-10-03", "2026-09-28"),
        ),
        (
            "2026-03-01",
            "2025-12-01",
            "hand",
            ("2025-12-01", 90, "2026-03-01", "2025-12-01", "2025-12-01"),
        ),
        (
            "2026-01-01",
            "2024-02-29",
            "hand",
            ("2024-02-29", 672, "2024-05-29", "2025-10-03", "2025-10-03"),
        ),
        (
            "2026-01-01",
            "2024-02-25",
            "mail",
            ("2024-03-01", 671, "2024-05-30", "2025-10-03", "2025-09-28"),
        ),
    ],
)
def test_calendar_crossings(
    corpus: Corpus,
    effective: str,
    served: str,
    method: Method,
    expected: tuple[object, ...],
) -> None:
    result = notice_deadline_check(
        facts(effective=effective, served=served, method=method), corpus=corpus
    )
    assert derived(result) == expected
    assert result.status == "passes_checked_rules"


@pytest.mark.parametrize(
    ("served", "method", "deemed", "days", "earliest"),
    [
        ("2026-07-03", "hand", "2026-07-03", 60, "2026-10-01"),
        ("2026-09-01", "hand", "2026-09-01", 0, "2026-11-30"),
        ("2026-09-01", "mail", "2026-09-06", -5, "2026-12-05"),
        ("2026-09-02", "hand", "2026-09-02", -1, "2026-12-01"),
        ("2026-09-02", "mail", "2026-09-07", -6, "2026-12-06"),
    ],
)
def test_demo_same_day_and_late_service_remain_adverse_facts(
    corpus: Corpus, served: str, method: Method, deemed: str, days: int, earliest: str
) -> None:
    result = notice_deadline_check(facts(served=served, method=method), corpus=corpus)
    assert derived(result) == (
        deemed,
        days,
        earliest,
        "2026-06-03",
        "2026-05-29" if method == "mail" else "2026-06-03",
    )
    assert result.status == "fails_checked_rules"
    assert result.checks[3].status == "fail"
    assert result.checks[3].reason == "violated"


@pytest.mark.parametrize(
    ("effective", "served", "method", "expected", "overall"),
    [
        (
            "2026-09-01",
            "2026-06-03",
            "hand",
            ("2026-06-03", 90, "2026-09-01", "2026-06-03", "2026-06-03"),
            "passes_checked_rules",
        ),
        (
            "2026-09-01",
            "2026-06-03",
            "mail",
            ("2026-06-08", 85, "2026-09-06", "2026-06-03", "2026-05-29"),
            "fails_checked_rules",
        ),
        (
            "2026-09-01",
            "2026-06-03",
            "unknown",
            (None, None, None, "2026-06-03", None),
            "cannot_determine",
        ),
        (
            "2026-09-01",
            None,
            "hand",
            (None, None, None, "2026-06-03", "2026-06-03"),
            "cannot_determine",
        ),
        (
            "2026-09-01",
            None,
            "mail",
            (None, None, None, "2026-06-03", "2026-05-29"),
            "cannot_determine",
        ),
        ("2026-09-01", None, "unknown", (None, None, None, "2026-06-03", None), "cannot_determine"),
        (
            None,
            "2026-06-03",
            "hand",
            ("2026-06-03", None, "2026-09-01", None, None),
            "cannot_determine",
        ),
        (
            None,
            "2026-06-03",
            "mail",
            ("2026-06-08", None, "2026-09-06", None, None),
            "cannot_determine",
        ),
        (None, "2026-06-03", "unknown", (None, None, None, None, None), "cannot_determine"),
        (None, None, "hand", (None, None, None, None, None), "cannot_determine"),
        (None, None, "mail", (None, None, None, None, None), "cannot_determine"),
        (None, None, "unknown", (None, None, None, None, None), "cannot_determine"),
    ],
)
def test_every_missing_date_and_method_combination_keeps_independent_fields(
    corpus: Corpus,
    effective: str | None,
    served: str | None,
    method: Method,
    expected: tuple[object, ...],
    overall: str,
) -> None:
    result = notice_deadline_check(
        facts(effective=effective, served=served, method=method), corpus=corpus
    )
    assert derived(result) == expected
    assert result.status == overall
    if effective is None or served is None or method == "unknown":
        assert (result.checks[3].status, result.checks[3].reason) == ("unknown", "missing_fact")
    assert result.checks[3].rule_ids == (MAIL_RULES if method == "mail" else NOTICE_RULES)


@pytest.mark.parametrize("ordinary", ["confirmed", "unknown", "excluded"])
@pytest.mark.parametrize("period", ["confirmed", "unknown", "excluded"])
@pytest.mark.parametrize(
    ("effective", "year_status", "year_reason"),
    [
        ("2026-09-01", "pass", "satisfied"),
        ("2027-09-01", "pass", "satisfied"),
        (None, "unknown", "missing_fact"),
        ("2025-09-01", "not_applicable", "out_of_year_range"),
        ("2028-09-01", "not_applicable", "out_of_year_range"),
    ],
)
@pytest.mark.parametrize("served", ["2026-06-03", "2028-12-31", None])
def test_gate_crossproduct_preserves_actual_checks_and_status_precedence(
    corpus: Corpus,
    ordinary: ScopeState,
    period: ScopeState,
    effective: str | None,
    year_status: str,
    year_reason: str,
    served: str | None,
) -> None:
    result = notice_deadline_check(
        facts(ordinary=ordinary, period=period, effective=effective, served=served),
        corpus=corpus,
    )
    scope_expectations = {
        "confirmed": ("pass", "satisfied"),
        "unknown": ("unknown", "missing_fact"),
        "excluded": ("not_applicable", "excluded_scope"),
    }
    for check, state in ((result.checks[0], ordinary), (result.checks[2], period)):
        assert (check.status, check.reason) == scope_expectations[state]
        assert check.rule_ids == ["scope.ordinary"]
    assert (result.checks[1].status, result.checks[1].reason) == (year_status, year_reason)
    assert result.checks[1].rule_ids == []
    if "excluded" in (ordinary, period) or year_status == "not_applicable":
        assert result.status == "unsupported"
        assert (result.checks[3].status, result.checks[3].reason) == (
            "not_applicable",
            "excluded_scope",
        )
        assert result.checks[3].rule_ids == ["scope.ordinary"]
        assert result.rule_ids == ["scope.ordinary"]
        assert derived(result) == (None, None, None, None, None)
    else:
        expected_notice = (
            ("unknown", "missing_fact")
            if effective is None or served is None
            else ("fail", "violated")
            if served == "2028-12-31"
            else ("pass", "satisfied")
        )
        assert (result.checks[3].status, result.checks[3].reason) == expected_notice
        if "unknown" in (ordinary, period) or expected_notice[0] == "unknown":
            assert result.status == "cannot_determine"
        elif expected_notice[0] == "fail":
            assert result.status == "fails_checked_rules"
        else:
            assert result.status == "passes_checked_rules"
        assert result.checks[3].rule_ids == NOTICE_RULES


@pytest.mark.parametrize("uncertain", ["ordinary", "period"])
def test_unconfirmed_scope_retains_definite_notice_failure_and_dates(
    corpus: Corpus, uncertain: str
) -> None:
    request = facts(
        served="2026-07-03",
        ordinary="unknown" if uncertain == "ordinary" else "confirmed",
        period="unknown" if uncertain == "period" else "confirmed",
    )
    result = notice_deadline_check(request, corpus=corpus)
    assert result.status == "cannot_determine"
    assert result.checks[3].status == "fail"
    assert derived(result) == ("2026-07-03", 60, "2026-10-01", "2026-06-03", "2026-06-03")


@pytest.mark.parametrize(
    ("effective", "served", "method", "expected", "overall"),
    [
        (
            "2026-09-01",
            "0001-01-01",
            "hand",
            ("0001-01-01", 739859, "0001-04-01", "2026-06-03", "2026-06-03"),
            "passes_checked_rules",
        ),
        (
            "2026-09-01",
            "0001-01-01",
            "mail",
            ("0001-01-06", 739854, "0001-04-06", "2026-06-03", "2026-05-29"),
            "passes_checked_rules",
        ),
        (
            "2026-09-01",
            "9999-12-31",
            "hand",
            ("9999-12-31", -2912199, None, "2026-06-03", "2026-06-03"),
            "fails_checked_rules",
        ),
        (
            "2026-09-01",
            "9999-12-31",
            "mail",
            (None, -2912204, None, "2026-06-03", "2026-05-29"),
            "fails_checked_rules",
        ),
        (
            "2026-09-01",
            "9999-12-31",
            "unknown",
            (None, None, None, "2026-06-03", None),
            "cannot_determine",
        ),
        (None, "9999-12-31", "hand", ("9999-12-31", None, None, None, None), "cannot_determine"),
        (None, "9999-12-31", "mail", (None, None, None, None, None), "cannot_determine"),
        (None, "9999-12-26", "mail", ("9999-12-31", None, None, None, None), "cannot_determine"),
        (None, "9999-12-27", "mail", (None, None, None, None, None), "cannot_determine"),
        (
            None,
            "9999-10-02",
            "hand",
            ("9999-10-02", None, "9999-12-31", None, None),
            "cannot_determine",
        ),
        (None, "9999-10-03", "hand", ("9999-10-03", None, None, None, None), "cannot_determine"),
        (
            None,
            "9999-09-27",
            "mail",
            ("9999-10-02", None, "9999-12-31", None, None),
            "cannot_determine",
        ),
        (None, "9999-09-28", "mail", ("9999-10-03", None, None, None, None), "cannot_determine"),
        ("0001-01-01", "9999-12-31", "mail", (None, None, None, None, None), "unsupported"),
        ("9999-12-31", "0001-01-01", "hand", (None, None, None, None, None), "unsupported"),
    ],
)
def test_full_gregorian_range_preserves_representable_fields_and_signed_intervals(
    corpus: Corpus,
    effective: str | None,
    served: str,
    method: Method,
    expected: tuple[object, ...],
    overall: str,
) -> None:
    result = notice_deadline_check(
        facts(effective=effective, served=served, method=method), corpus=corpus
    )
    assert derived(result) == expected
    assert result.status == overall
    if overall == "fails_checked_rules":
        assert (result.checks[3].status, result.checks[3].reason) == ("fail", "violated")


def test_complete_mail_result_wire_contract_and_rule_provenance(corpus: Corpus) -> None:
    result = notice_deadline_check(facts(served="2026-05-29", method="mail"), corpus=corpus)
    assert json.loads(result.model_dump_json()) == {
        "tool": "notice_deadline_check",
        "status": "passes_checked_rules",
        "checks": [
            {
                "id": "scope",
                "status": "pass",
                "reason": "satisfied",
                "rule_ids": ["scope.ordinary"],
            },
            {"id": "supported_year", "status": "pass", "reason": "satisfied", "rule_ids": []},
            {
                "id": "period_start",
                "status": "pass",
                "reason": "satisfied",
                "rule_ids": ["scope.ordinary"],
            },
            {"id": "notice", "status": "pass", "reason": "satisfied", "rule_ids": MAIL_RULES},
        ],
        "deemed_served_on": "2026-06-03",
        "notice_days": 90,
        "earliest_notice_on": "2026-09-01",
        "latest_deemed_service_on": "2026-06-03",
        "latest_dispatch_on": "2026-05-29",
        "earliest_spacing_on": None,
        "guideline_percent": None,
        "cap_cents_exact": None,
        "rule_ids": ["calendar.s89", "notice.90_days", "notice.mail_5_days", "scope.ordinary"],
    }
    assert ToolResult.model_validate_json(result.model_dump_json()) == result
    for rule_id in result.rule_ids:
        assert corpus.rule(rule_id).evidence_ids


@pytest.mark.parametrize("method", ["hand", "mail", "unknown"])
def test_result_is_repeatable_and_does_not_mutate_facts_or_corpus(
    corpus: Corpus, method: Method
) -> None:
    request = facts(method=method)
    original_request = request.model_dump_json()
    original_rules = tuple(rule.model_dump_json() for rule in corpus.rules)
    original_chunks = tuple(chunk.model_dump_json() for chunk in corpus.chunks)
    first = notice_deadline_check(request, corpus=corpus)
    second = notice_deadline_check(request, corpus=corpus)
    assert first.model_dump_json() == second.model_dump_json()
    assert request.model_dump_json() == original_request
    assert tuple(rule.model_dump_json() for rule in corpus.rules) == original_rules
    assert tuple(chunk.model_dump_json() for chunk in corpus.chunks) == original_chunks
    assert all(
        value is None
        for value in (first.earliest_spacing_on, first.guideline_percent, first.cap_cents_exact)
    )


def forbidden_dependency(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("notice calculation touched an external dependency")


def test_call_and_module_import_need_no_implicit_load_io_clock_or_environment(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = facts(method="mail", served="2026-05-29")
    with monkeypatch.context() as blocked:
        for owner, name in (
            (builtins, "open"),
            (Path, "open"),
            (corpus_module, "load_corpus"),
            (socket, "socket"),
            (sqlite3, "connect"),
            (urllib.request, "urlopen"),
            (time, "time"),
            (time, "monotonic"),
            (time, "perf_counter"),
            (os, "getenv"),
        ):
            blocked.setattr(owner, name, forbidden_dependency)
        blocked.setattr(type(os.environ), "__getitem__", forbidden_dependency)
        importlib.reload(notice_module)
        blocked.setattr(
            notice_module,
            "date",
            SimpleNamespace(
                min=date.min,
                max=date.max,
                fromordinal=date.fromordinal,
                today=forbidden_dependency,
            ),
        )
        result = notice_module.notice_deadline_check(request, corpus=corpus)
    assert result.status == "passes_checked_rules"


def test_rule_reads_use_only_injected_corpus(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[RuleId] = []
    original: Callable[[Corpus, RuleId], Rule] = Corpus.rule

    def record_rule(self: Corpus, rule_id: RuleId) -> Rule:
        assert self is corpus
        reads.append(rule_id)
        return original(self, rule_id)

    monkeypatch.setattr(Corpus, "rule", record_rule)
    result = notice_deadline_check(facts(method="mail"), corpus=corpus)
    assert {"notice.90_days", "notice.mail_5_days"} <= set(reads)
    assert set(reads) <= set(result.rule_ids)


@pytest.mark.parametrize(
    ("ordinary", "period", "effective"),
    [
        ("excluded", "unknown", "2026-09-01"),
        ("unknown", "excluded", "2026-09-01"),
        ("unknown", "unknown", "0001-01-01"),
        ("confirmed", "confirmed", "9999-12-31"),
    ],
)
def test_known_exclusion_never_requests_numeric_rules(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    ordinary: ScopeState,
    period: ScopeState,
    effective: str,
) -> None:
    original: Callable[[Corpus, RuleId], Rule] = Corpus.rule
    reads: list[RuleId] = []

    def scope_rule_only(self: Corpus, rule_id: RuleId) -> Rule:
        assert rule_id == "scope.ordinary"
        reads.append(rule_id)
        return original(self, rule_id)

    monkeypatch.setattr(Corpus, "rule", scope_rule_only)
    result = notice_deadline_check(
        facts(
            ordinary=ordinary,
            period=period,
            effective=effective,
            served="9999-12-31",
            method="mail",
        ),
        corpus=corpus,
    )
    assert reads
    assert result.status == "unsupported"
    assert derived(result) == (None, None, None, None, None)


def test_internal_corpus_is_required_keyword_only_and_absent_from_provider_schema() -> None:
    signature = inspect.signature(notice_deadline_check)
    assert list(signature.parameters) == ["facts", "corpus"]
    assert signature.parameters["corpus"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["corpus"].default is inspect.Parameter.empty
    definition = provider_tool_definitions()[0]
    assert definition["name"] == "notice_deadline_check"
    assert definition["input_schema"] == NoticeFacts.model_json_schema()
    assert "corpus" not in json.dumps(definition["input_schema"])
