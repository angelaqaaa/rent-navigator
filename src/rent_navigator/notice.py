"""Pure notice-only thresholds over confirmed facts and already loaded rules."""

from datetime import date
from typing import Literal

from rent_navigator.corpus import Corpus
from rent_navigator.models import (
    CheckResult,
    NoticeFacts,
    RuleId,
    ScopeState,
    ToolResult,
    ToolStatus,
)


def _scope_check(identifier: Literal["scope", "period_start"], state: ScopeState) -> CheckResult:
    if state == "confirmed":
        return CheckResult(
            id=identifier, status="pass", reason="satisfied", rule_ids=["scope.ordinary"]
        )
    if state == "excluded":
        return CheckResult(
            id=identifier,
            status="not_applicable",
            reason="excluded_scope",
            rule_ids=["scope.ordinary"],
        )
    return CheckResult(
        id=identifier, status="unknown", reason="missing_fact", rule_ids=["scope.ordinary"]
    )


def _supported_year(effective_on: date | None) -> CheckResult:
    # This is the project's supported range, not a statutory restriction on notice.
    if effective_on is None:
        return CheckResult(
            id="supported_year", status="unknown", reason="missing_fact", rule_ids=[]
        )
    if effective_on.year in (2026, 2027):
        return CheckResult(id="supported_year", status="pass", reason="satisfied", rule_ids=[])
    return CheckResult(
        id="supported_year", status="not_applicable", reason="out_of_year_range", rule_ids=[]
    )


def _calendar_days(corpus: Corpus, identifier: RuleId) -> int:
    rule = corpus.rule(identifier)
    if type(rule.value) is not int or rule.unit != "calendar_days":
        raise ValueError("Notice arithmetic requires an integer calendar-day rule")
    return rule.value


def _representable_date(ordinal: int | None) -> date | None:
    if ordinal is None or not date.min.toordinal() <= ordinal <= date.max.toordinal():
        return None
    return date.fromordinal(ordinal)


def notice_deadline_check(facts: NoticeFacts, *, corpus: Corpus) -> ToolResult:
    """Check ordinary notice timing without I/O, a clock, or mutating inputs.

    The caller supplies a validated, loaded corpus. A passing result covers only
    these checks. Unrepresentable derived dates are null; exact intervals remain.
    """
    checks = [
        _scope_check("scope", facts.scope.ordinary),
        _supported_year(facts.effective_on),
        _scope_check("period_start", facts.scope.period_start),
    ]
    deemed: int | None = None
    earliest: int | None = None
    latest_deemed: int | None = None
    latest_dispatch: int | None = None
    notice_days: int | None = None
    status: ToolStatus

    if any(check.status == "not_applicable" for check in checks):
        status = "unsupported"
        checks.append(
            CheckResult(
                id="notice",
                status="not_applicable",
                reason="excluded_scope",
                rule_ids=["scope.ordinary"],
            )
        )
    else:
        required_days = _calendar_days(corpus, "notice.90_days")
        notice_rules: list[RuleId] = ["calendar.s89", "notice.90_days"]
        offset: int | None = None
        if facts.service_method == "hand":
            offset = 0
        elif facts.service_method == "mail":
            offset = _calendar_days(corpus, "notice.mail_5_days")
            notice_rules.append("notice.mail_5_days")

        # Keep ordinal arithmetic independent of whether a date can be serialized.
        if facts.served_on is not None and offset is not None:
            deemed = facts.served_on.toordinal() + offset
            earliest = deemed + required_days
        if facts.effective_on is not None:
            effective = facts.effective_on.toordinal()
            latest_deemed = effective - required_days
            if offset is not None:
                latest_dispatch = latest_deemed - offset
            if deemed is not None:
                notice_days = effective - deemed

        if notice_days is None:
            notice = CheckResult(
                id="notice", status="unknown", reason="missing_fact", rule_ids=notice_rules
            )
        elif notice_days >= required_days:
            notice = CheckResult(
                id="notice", status="pass", reason="satisfied", rule_ids=notice_rules
            )
        else:
            notice = CheckResult(
                id="notice", status="fail", reason="violated", rule_ids=notice_rules
            )
        checks.append(notice)

        if facts.scope.ordinary == "unknown" or facts.scope.period_start == "unknown":
            status = "cannot_determine"
        elif notice.status == "fail":
            status = "fails_checked_rules"
        elif any(check.status == "unknown" for check in checks):
            status = "cannot_determine"
        else:
            status = "passes_checked_rules"

    rule_ids = sorted({identifier for check in checks for identifier in check.rule_ids})
    for identifier in rule_ids:
        corpus.rule(identifier)
    return ToolResult(
        tool="notice_deadline_check",
        status=status,
        checks=checks,
        deemed_served_on=_representable_date(deemed),
        notice_days=notice_days,
        earliest_notice_on=_representable_date(earliest),
        latest_deemed_service_on=_representable_date(latest_deemed),
        latest_dispatch_on=_representable_date(latest_dispatch),
        earliest_spacing_on=None,
        guideline_percent=None,
        cap_cents_exact=None,
        rule_ids=rule_ids,
    )
