"""Synthetic calculator boundaries, separate from approved evaluation cases."""

import builtins
import importlib
import inspect
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import date
from decimal import Inexact, Rounded, getcontext, localcontext
from pathlib import Path
from typing import Literal, NoReturn
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import rent_navigator.corpus as corpus_module
import rent_navigator.rent as rent_module
from rent_navigator.corpus import Corpus, Rule, load_corpus
from rent_navigator.models import (
    NoticeFacts,
    RentFacts,
    RuleId,
    ScopeState,
    ToolResult,
    provider_tool_definitions,
)
from rent_navigator.notice import notice_deadline_check
from rent_navigator.rent import rent_increase_check

History = Literal["known", "none", "unknown"]
Guideline = Literal["controlled", "exempt_s6_1", "unknown"]
Form = Literal["N1", "N2", "other", "unknown"]
Method = Literal["hand", "mail", "unknown"]
SPACING_RULES = ["calendar.s89", "spacing.12_months"]
NOTICE_FIELDS = (
    "deemed_served_on",
    "notice_days",
    "earliest_notice_on",
    "latest_deemed_service_on",
    "latest_dispatch_on",
)
DERIVED_FIELDS = (*NOTICE_FIELDS, "earliest_spacing_on", "guideline_percent", "cap_cents_exact")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def facts(
    *,
    effective: str | None = "2026-09-01",
    served: str | None = "2025-01-01",
    method: Method = "hand",
    ordinary: ScopeState = "confirmed",
    period: ScopeState = "confirmed",
    current: int | None = 200000,
    proposed: int | None = 200001,
    tenancy: str | None = "2020-01-01",
    history: History = "known",
    last: str | None = "2025-09-01",
    guideline: Guideline = "controlled",
    form: Form = "N1",
) -> RentFacts:
    return RentFacts.model_validate_json(
        json.dumps(
            {
                "scope": {"ordinary": ordinary, "period_start": period},
                "effective_on": effective,
                "served_on": served,
                "service_method": method,
                "current_cents": current,
                "proposed_cents": proposed,
                "tenancy_start": tenancy,
                "last_increase": {"state": history, "date": last},
                "guideline_status": guideline,
                "form": form,
            }
        )
    )


def notice_facts(request: RentFacts) -> NoticeFacts:
    return NoticeFacts.model_validate_json(
        json.dumps(
            {
                key: value
                for key, value in request.model_dump(mode="json").items()
                if key in NoticeFacts.model_fields
            }
        )
    )


@pytest.mark.parametrize(
    ("history", "last", "tenancy", "threshold", "status"),
    [
        ("none", None, "2025-09-01", "2026-09-01", "pass"),
        ("unknown", None, "2025-09-01", None, "unknown"),
        ("known", "2025-09-01", "2020-01-01", "2026-09-01", "pass"),
        ("known", "2025-09-02", "2020-01-01", "2026-09-02", "fail"),
        ("known", "2025-09-01", None, "2026-09-01", "pass"),
        ("none", None, None, None, "unknown"),
        ("unknown", None, None, None, "unknown"),
        ("none", None, "2026-09-02", "2027-09-02", "fail"),
        ("known", "2026-09-02", "2026-09-01", "2027-09-02", "fail"),
    ],
)
def test_spacing_uses_confirmed_history_without_guessing_a_base(
    corpus: Corpus,
    history: History,
    last: str | None,
    tenancy: str | None,
    threshold: str | None,
    status: str,
) -> None:
    result = rent_increase_check(facts(history=history, last=last, tenancy=tenancy), corpus=corpus)
    assert result.model_dump(mode="json")["earliest_spacing_on"] == threshold
    check = result.checks[4]
    assert check.status == status
    assert (
        check.reason == {"pass": "satisfied", "fail": "violated", "unknown": "missing_fact"}[status]
    )
    assert check.rule_ids == SPACING_RULES


@pytest.mark.parametrize("history", ["known", "none"])
@pytest.mark.parametrize(
    ("effective", "status"),
    [("2026-08-31", "fail"), ("2026-09-01", "pass"), ("2026-09-02", "pass")],
)
def test_spacing_anniversary_day_boundaries(
    corpus: Corpus, history: History, effective: str, status: str
) -> None:
    result = rent_increase_check(
        facts(
            effective=effective,
            history=history,
            last="2025-09-01" if history == "known" else None,
            tenancy="2025-09-01",
        ),
        corpus=corpus,
    )
    assert result.earliest_spacing_on == date(2026, 9, 1)
    assert result.checks[4].status == status
    assert result.status == ("fails_checked_rules" if status == "fail" else "passes_checked_rules")


@pytest.mark.parametrize("history", ["known", "none"])
@pytest.mark.parametrize(
    ("base", "effective", "threshold", "status"),
    [
        ("2024-02-29", "2026-09-01", "2025-02-28", "pass"),
        ("2023-03-01", "2026-09-01", "2024-03-01", "pass"),
        ("2025-12-31", "2026-12-31", "2026-12-31", "pass"),
        ("0001-01-01", "2026-09-01", "0002-01-01", "pass"),
        ("9998-12-31", "2026-09-01", "9999-12-31", "fail"),
        ("9999-01-01", "2026-09-01", None, "fail"),
        ("9999-12-31", "2027-09-01", None, "fail"),
        ("2024-02-29", None, "2025-02-28", "unknown"),
        ("2025-09-01", None, "2026-09-01", "unknown"),
        ("9999-12-31", None, None, "unknown"),
    ],
)
def test_spacing_calendar_and_full_gregorian_boundaries(
    corpus: Corpus,
    history: History,
    base: str,
    effective: str | None,
    threshold: str | None,
    status: str,
) -> None:
    result = rent_increase_check(
        facts(
            effective=effective,
            history=history,
            last=base if history == "known" else None,
            tenancy=base,
        ),
        corpus=corpus,
    )
    assert result.model_dump(mode="json")["earliest_spacing_on"] == threshold
    assert result.checks[4].status == status
    assert (
        result.checks[4].reason
        == {"pass": "satisfied", "fail": "violated", "unknown": "missing_fact"}[status]
    )


def test_known_history_before_known_tenancy_remains_invalid_wire_input() -> None:
    with pytest.raises(ValidationError, match="known last increase precedes known tenancy start"):
        facts(tenancy="2025-09-02", last="2025-09-01")


@pytest.mark.parametrize(
    ("effective", "current", "proposed", "percent", "cap", "status", "reason"),
    [
        ("2026-09-01", 200000, 204199, "2.1", "204200", "pass", "satisfied"),
        ("2026-09-01", 200000, 204200, "2.1", "204200", "pass", "satisfied"),
        ("2026-09-01", 200000, 204201, "2.1", "204200", "fail", "violated"),
        ("2027-09-01", 200000, 203799, "1.9", "203800", "pass", "satisfied"),
        ("2027-09-01", 200000, 203800, "1.9", "203800", "pass", "satisfied"),
        ("2027-09-01", 200000, 203801, "1.9", "203800", "fail", "violated"),
        ("2026-09-01", 100001, 102101, "2.1", "102101.021", "pass", "satisfied"),
        ("2026-09-01", 100001, 102102, "2.1", "102101.021", "unknown", "rounding_uncertain"),
        ("2026-09-01", 100001, 102103, "2.1", "102101.021", "fail", "violated"),
        ("2027-09-01", 100001, 101901, "1.9", "101901.019", "pass", "satisfied"),
        ("2027-09-01", 100001, 101902, "1.9", "101901.019", "unknown", "rounding_uncertain"),
        ("2027-09-01", 100001, 101903, "1.9", "101901.019", "fail", "violated"),
        ("2026-09-01", 10, 10, "2.1", "10.21", "pass", "satisfied"),
        ("2026-09-01", 100, 100, "2.1", "102.1", "pass", "satisfied"),
        ("2026-09-01", 1, 1, "2.1", "1.021", "pass", "satisfied"),
        ("2027-09-01", 1, 2, "1.9", "1.019", "unknown", "rounding_uncertain"),
        ("2026-09-01", 200000, 1, "2.1", "204200", "pass", "satisfied"),
    ],
)
def test_exact_money_boundaries_and_canonical_serialization(
    corpus: Corpus,
    effective: str,
    current: int,
    proposed: int,
    percent: str,
    cap: str,
    status: str,
    reason: str,
) -> None:
    result = rent_increase_check(
        facts(effective=effective, current=current, proposed=proposed), corpus=corpus
    )
    assert (result.guideline_percent, result.cap_cents_exact) == (percent, cap)
    assert (result.checks[5].status, result.checks[5].reason) == (status, reason)
    assert result.checks[5].rule_ids == ["guideline." + effective[:4]]
    assert (
        result.status
        == {
            "pass": "passes_checked_rules",
            "fail": "fails_checked_rules",
            "unknown": "cannot_determine",
        }[status]
    )
    assert ToolResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("effective", "current", "proposed", "cap", "status"),
    [
        (
            "2026-09-01",
            100000000000000000000000000001,
            102100000000000000000000000001,
            "102100000000000000000000000001.021",
            "pass",
        ),
        (
            "2026-09-01",
            100000000000000000000000000001,
            102100000000000000000000000002,
            "102100000000000000000000000001.021",
            "unknown",
        ),
        (
            "2026-09-01",
            100000000000000000000000000001,
            102100000000000000000000000003,
            "102100000000000000000000000001.021",
            "fail",
        ),
        (
            "2027-09-01",
            100000000000000000000000000001,
            101900000000000000000000000001,
            "101900000000000000000000000001.019",
            "pass",
        ),
        (
            "2027-09-01",
            100000000000000000000000000001,
            101900000000000000000000000002,
            "101900000000000000000000000001.019",
            "unknown",
        ),
        (
            "2027-09-01",
            100000000000000000000000000001,
            101900000000000000000000000003,
            "101900000000000000000000000001.019",
            "fail",
        ),
        (
            "2026-09-01",
            1000000000000000000000000000000,
            1021000000000000000000000000000,
            "1021000000000000000000000000000",
            "pass",
        ),
    ],
)
def test_unbounded_integer_money_is_independent_of_decimal_precision(
    corpus: Corpus, effective: str, current: int, proposed: int, cap: str, status: str
) -> None:
    original_context = repr(getcontext())
    request = facts(effective=effective, current=current, proposed=proposed)
    for precision in (2, 28):
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            before = repr(context)
            result = rent_increase_check(request, corpus=corpus)
            assert repr(context) == before
            assert result.cap_cents_exact == cap
            assert result.checks[5].status == status
            assert ToolResult.model_validate_json(result.model_dump_json()) == result
    assert repr(getcontext()) == original_context


@pytest.mark.parametrize("guideline", ["controlled", "exempt_s6_1", "unknown"])
@pytest.mark.parametrize("current", [None, 200000])
@pytest.mark.parametrize("proposed", [None, 200001])
@pytest.mark.parametrize("effective", [None, "2026-09-01"])
def test_partial_guideline_fields_and_explicit_exemption(
    corpus: Corpus,
    guideline: Guideline,
    current: int | None,
    proposed: int | None,
    effective: str | None,
) -> None:
    result = rent_increase_check(
        facts(guideline=guideline, current=current, proposed=proposed, effective=effective),
        corpus=corpus,
    )
    check = result.checks[5]
    if guideline == "controlled" and effective is not None:
        assert result.guideline_percent == "2.1"
        assert result.cap_cents_exact == ("204200" if current is not None else None)
        assert check.rule_ids == ["guideline.2026"]
    else:
        assert result.guideline_percent is None
        assert result.cap_cents_exact is None
        assert check.rule_ids == ([] if guideline == "controlled" else ["exemption.s6_1"])
    if guideline == "exempt_s6_1":
        expected = ("not_applicable", "exempt")
    elif guideline == "controlled" and None not in (current, proposed, effective):
        expected = ("pass", "satisfied")
    else:
        expected = ("unknown", "missing_fact")
    assert (check.status, check.reason) == expected


@pytest.mark.parametrize(
    ("guideline", "form", "status", "reason", "rules"),
    [
        ("controlled", "N1", "pass", "satisfied", ["form.N1"]),
        ("controlled", "N2", "fail", "violated", ["form.N1"]),
        ("controlled", "other", "fail", "violated", ["form.N1"]),
        ("controlled", "unknown", "unknown", "missing_fact", ["form.N1"]),
        ("exempt_s6_1", "N1", "fail", "violated", ["exemption.s6_1", "form.N2"]),
        ("exempt_s6_1", "N2", "pass", "satisfied", ["exemption.s6_1", "form.N2"]),
        ("exempt_s6_1", "other", "fail", "violated", ["exemption.s6_1", "form.N2"]),
        ("exempt_s6_1", "unknown", "unknown", "missing_fact", ["exemption.s6_1", "form.N2"]),
        ("unknown", "N1", "unknown", "missing_fact", ["form.N1", "form.N2"]),
        ("unknown", "N2", "unknown", "missing_fact", ["form.N1", "form.N2"]),
        ("unknown", "other", "unknown", "missing_fact", ["form.N1", "form.N2"]),
        ("unknown", "unknown", "unknown", "missing_fact", ["form.N1", "form.N2"]),
    ],
)
def test_form_matrix_cannot_infer_exemption(
    corpus: Corpus, guideline: Guideline, form: Form, status: str, reason: str, rules: list[str]
) -> None:
    result = rent_increase_check(facts(guideline=guideline, form=form), corpus=corpus)
    assert (result.checks[6].status, result.checks[6].reason, result.checks[6].rule_ids) == (
        status,
        reason,
        rules,
    )
    assert (
        result.checks[5].status
        == {"controlled": "pass", "exempt_s6_1": "not_applicable", "unknown": "unknown"}[guideline]
    )


@pytest.mark.parametrize(
    ("served", "last", "form", "statuses", "overall"),
    [
        (
            "2025-01-01",
            "2025-09-01",
            "N2",
            ["pass", "pass", "not_applicable", "pass"],
            "passes_checked_rules",
        ),
        (
            "2026-07-03",
            "2025-09-01",
            "N2",
            ["fail", "pass", "not_applicable", "pass"],
            "fails_checked_rules",
        ),
        (
            "2025-01-01",
            "2025-09-02",
            "N2",
            ["pass", "fail", "not_applicable", "pass"],
            "fails_checked_rules",
        ),
        (
            "2025-01-01",
            "2025-09-01",
            "N1",
            ["pass", "pass", "not_applicable", "fail"],
            "fails_checked_rules",
        ),
    ],
)
def test_exemption_only_removes_guideline_check(
    corpus: Corpus, served: str, last: str, form: Form, statuses: list[str], overall: str
) -> None:
    result = rent_increase_check(
        facts(
            guideline="exempt_s6_1",
            current=None,
            proposed=None,
            served=served,
            last=last,
            form=form,
        ),
        corpus=corpus,
    )
    assert [check.status for check in result.checks[3:]] == statuses
    assert result.status == overall
    assert (result.guideline_percent, result.cap_cents_exact) == (None, None)


@pytest.mark.parametrize("ordinary", ["confirmed", "unknown", "excluded"])
@pytest.mark.parametrize("period", ["confirmed", "unknown", "excluded"])
@pytest.mark.parametrize(
    "effective", [None, "2026-09-01", "2027-09-01", "2025-09-01", "2028-09-01"]
)
def test_scope_year_and_period_priority_retains_each_check(
    corpus: Corpus, ordinary: ScopeState, period: ScopeState, effective: str | None
) -> None:
    request = facts(
        ordinary=ordinary, period=period, effective=effective, proposed=300000, form="other"
    )
    result = rent_increase_check(request, corpus=corpus)
    notice = notice_deadline_check(notice_facts(request), corpus=corpus)
    assert result.checks[:4] == notice.checks
    if "excluded" in (ordinary, period) or effective in ("2025-09-01", "2028-09-01"):
        assert result.status == "unsupported"
        assert all(result.model_dump(mode="json")[field] is None for field in DERIVED_FIELDS)
        assert [(check.status, check.reason, check.rule_ids) for check in result.checks[3:]] == [
            ("not_applicable", "excluded_scope", ["scope.ordinary"])
        ] * 4
        assert result.rule_ids == ["scope.ordinary"]
    else:
        assert result.status == (
            "cannot_determine" if "unknown" in (ordinary, period) else "fails_checked_rules"
        )
        assert result.checks[6].status == "fail"
        assert (
            result.cap_cents_exact
            == {None: None, "2026-09-01": "204200", "2027-09-01": "203800"}[effective]
        )
        assert result.earliest_spacing_on == date(2026, 9, 1)
    assert result.rule_ids == sorted(
        {identifier for check in result.checks for identifier in check.rule_ids}
    )
    for check in result.checks:
        assert check.rule_ids == sorted(set(check.rule_ids))
    for identifier in result.rule_ids:
        assert corpus.rule(identifier).evidence_ids


@pytest.mark.parametrize(
    ("case_facts", "statuses", "overall"),
    [
        (
            facts(served=None, proposed=204201),
            ["unknown", "pass", "fail", "pass"],
            "fails_checked_rules",
        ),
        (
            facts(served=None, proposed=204201, ordinary="unknown"),
            ["unknown", "pass", "fail", "pass"],
            "cannot_determine",
        ),
        (
            facts(served=None, proposed=204201, period="unknown"),
            ["unknown", "pass", "fail", "pass"],
            "cannot_determine",
        ),
        (
            facts(effective=None, form="N2"),
            ["unknown", "unknown", "unknown", "fail"],
            "fails_checked_rules",
        ),
        (
            facts(served="2026-07-03", proposed=204800),
            ["fail", "pass", "fail", "pass"],
            "fails_checked_rules",
        ),
        (
            facts(
                served=None,
                history="unknown",
                last=None,
                current=None,
                guideline="unknown",
                form="other",
            ),
            ["unknown", "unknown", "unknown", "unknown"],
            "cannot_determine",
        ),
    ],
)
def test_aggregate_rechecks_all_seven_conditions(
    corpus: Corpus, case_facts: RentFacts, statuses: list[str], overall: str
) -> None:
    result = rent_increase_check(case_facts, corpus=corpus)
    assert [check.status for check in result.checks[3:]] == statuses
    assert result.status == overall
    if case_facts.served_on == date(2026, 7, 3):
        assert result.notice_days == 60
        assert result.cap_cents_exact == "204200"
        assert result.earliest_spacing_on == date(2026, 9, 1)


def test_complete_result_wire_contract_and_rule_union(corpus: Corpus) -> None:
    result = rent_increase_check(facts(served="2026-05-29", method="mail"), corpus=corpus)
    assert json.loads(result.model_dump_json()) == {
        "tool": "rent_increase_check",
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
            {
                "id": "notice",
                "status": "pass",
                "reason": "satisfied",
                "rule_ids": ["calendar.s89", "notice.90_days", "notice.mail_5_days"],
            },
            {"id": "spacing", "status": "pass", "reason": "satisfied", "rule_ids": SPACING_RULES},
            {
                "id": "guideline",
                "status": "pass",
                "reason": "satisfied",
                "rule_ids": ["guideline.2026"],
            },
            {"id": "form", "status": "pass", "reason": "satisfied", "rule_ids": ["form.N1"]},
        ],
        "deemed_served_on": "2026-06-03",
        "notice_days": 90,
        "earliest_notice_on": "2026-09-01",
        "latest_deemed_service_on": "2026-06-03",
        "latest_dispatch_on": "2026-05-29",
        "earliest_spacing_on": "2026-09-01",
        "guideline_percent": "2.1",
        "cap_cents_exact": "204200",
        "rule_ids": [
            "calendar.s89",
            "form.N1",
            "guideline.2026",
            "notice.90_days",
            "notice.mail_5_days",
            "scope.ordinary",
            "spacing.12_months",
        ],
    }
    assert ToolResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("method", ["hand", "mail", "unknown"])
@pytest.mark.parametrize("effective", [None, "2026-09-01"])
@pytest.mark.parametrize("served", [None, "2026-07-03", "9999-12-31"])
def test_notice_fields_are_exactly_the_public_notice_result(
    corpus: Corpus, method: Method, effective: str | None, served: str | None
) -> None:
    request = facts(method=method, effective=effective, served=served)
    original_request = request.model_dump_json()
    original_corpus = repr(corpus)
    notice = notice_deadline_check(notice_facts(request), corpus=corpus)
    result = rent_increase_check(request, corpus=corpus)
    assert result.checks[:4] == notice.checks
    assert {field: result.model_dump(mode="json")[field] for field in NOTICE_FIELDS} == {
        field: notice.model_dump(mode="json")[field] for field in NOTICE_FIELDS
    }
    assert result.model_dump_json() == rent_increase_check(request, corpus=corpus).model_dump_json()
    assert request.model_dump_json() == original_request
    assert repr(corpus) == original_corpus


def test_public_notice_function_is_called_once_and_its_result_is_not_modified(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = facts()
    original = notice_deadline_check(notice_facts(request), corpus=corpus)
    original_json = original.model_dump_json()
    calls: list[NoticeFacts] = []

    def record_notice(facts: NoticeFacts, *, corpus: Corpus) -> ToolResult:
        assert corpus is loaded
        calls.append(facts)
        return original

    loaded = corpus
    monkeypatch.setattr(rent_module, "notice_deadline_check", record_notice)
    result = rent_module.rent_increase_check(request, corpus=corpus)
    assert len(calls) == 1
    assert notice_facts(request).model_dump() == {
        key: value
        for key, value in calls[0].model_dump().items()
        if key in NoticeFacts.model_fields
    }
    assert original.model_dump_json() == original_json
    assert result is not original
    assert result.checks is not original.checks
    assert len(result.checks) == 7


def forbidden_dependency(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("rent calculation touched an external dependency")


def test_import_and_call_need_no_implicit_load_io_clock_or_environment(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = facts(current=100001, proposed=102102, last="2024-02-29")
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
        importlib.reload(rent_module)
        date_access = Mock(wraps=date, min=date.min, max=date.max)
        date_access.today.side_effect = forbidden_dependency
        blocked.setattr(rent_module, "date", date_access)
        result = rent_module.rent_increase_check(request, corpus=corpus)
    assert result.status == "cannot_determine"
    assert result.cap_cents_exact == "102101.021"
    assert result.earliest_spacing_on == date(2025, 2, 28)


def test_rule_reads_use_the_injected_corpus(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[RuleId] = []
    original: Callable[[Corpus, RuleId], Rule] = Corpus.rule

    def record_rule(self: Corpus, rule_id: RuleId) -> Rule:
        assert self is corpus
        reads.append(rule_id)
        return original(self, rule_id)

    monkeypatch.setattr(Corpus, "rule", record_rule)
    result = rent_increase_check(facts(method="mail"), corpus=corpus)
    assert {"notice.90_days", "notice.mail_5_days", "spacing.12_months", "guideline.2026"} <= set(
        reads
    )
    assert set(reads) == set(result.rule_ids)


@pytest.mark.parametrize(
    ("ordinary", "period", "effective"),
    [
        ("excluded", "unknown", "2026-09-01"),
        ("unknown", "excluded", None),
        ("unknown", "unknown", "0001-01-01"),
        ("confirmed", "confirmed", "9999-12-31"),
    ],
)
def test_known_exclusion_never_reads_numeric_rules(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    ordinary: ScopeState,
    period: ScopeState,
    effective: str | None,
) -> None:
    original: Callable[[Corpus, RuleId], Rule] = Corpus.rule

    def scope_only(self: Corpus, rule_id: RuleId) -> Rule:
        assert rule_id == "scope.ordinary"
        return original(self, rule_id)

    monkeypatch.setattr(Corpus, "rule", scope_only)
    result = rent_increase_check(
        facts(
            ordinary=ordinary,
            period=period,
            effective=effective,
            served="9999-12-31",
            method="mail",
            last="9999-12-31",
            current=100000000000000000000000000001,
        ),
        corpus=corpus,
    )
    assert result.status == "unsupported"
    assert all(result.model_dump(mode="json")[field] is None for field in DERIVED_FIELDS)


def test_internal_corpus_is_required_keyword_only_and_not_a_wire_argument() -> None:
    signature = inspect.signature(rent_increase_check)
    assert list(signature.parameters) == ["facts", "corpus"]
    assert signature.parameters["corpus"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["corpus"].default is inspect.Parameter.empty
    definition = provider_tool_definitions()[1]
    assert definition["name"] == "rent_increase_check"
    assert definition["input_schema"] == RentFacts.model_json_schema()
    assert "corpus" not in json.dumps(definition["input_schema"])


def test_money_beyond_runtime_decimal_digit_conversion_limit(corpus: Corpus) -> None:
    digit_limit = sys.get_int_max_str_digits()
    values = facts(proposed=1).model_dump()
    values["current_cents"] = 10**4300
    request = RentFacts.model_validate(values)
    result = rent_increase_check(request, corpus=corpus)
    assert result.cap_cents_exact == "1021" + "0" * 4297
    assert result.checks[5].status == "pass"
    assert ToolResult.model_validate_json(result.model_dump_json()) == result
    assert sys.get_int_max_str_digits() == digit_limit


@pytest.mark.parametrize(
    ("current", "cap"),
    [
        (100000000, "102100000"),
        (1000000000, "1021000000"),
        (10000000000, "10210000000"),
        (10000000001, "10210000001.021"),
    ],
)
def test_decimal_group_boundaries_preserve_internal_and_trailing_zeros(
    corpus: Corpus, current: int, cap: str
) -> None:
    result = rent_increase_check(facts(current=current, proposed=1), corpus=corpus)
    assert result.cap_cents_exact == cap
    assert result.checks[5].status == "pass"
