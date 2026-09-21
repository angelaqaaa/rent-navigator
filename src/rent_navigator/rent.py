"""Pure ordinary rent checks over confirmed facts and already loaded rules."""

from calendar import monthrange
from datetime import date
from typing import Literal, cast

from rent_navigator.corpus import Corpus
from rent_navigator.models import (
    CheckResult,
    NoticeFacts,
    RentFacts,
    RuleId,
    ToolResult,
    ToolStatus,
)
from rent_navigator.notice import notice_deadline_check

_Percentage = Literal["2.1", "1.9"]


def _spacing(facts: RentFacts, corpus: Corpus) -> tuple[CheckResult, date | None]:
    check = CheckResult(
        id="spacing",
        status="unknown",
        reason="missing_fact",
        rule_ids=["calendar.s89", "spacing.12_months"],
    )
    base = None
    if facts.last_increase.state == "known":
        base = facts.last_increase.date
    elif facts.last_increase.state == "none":
        base = facts.tenancy_start
    if base is None:
        return check, None

    rule = corpus.rule("spacing.12_months")
    if type(rule.value) is not int or rule.unit != "calendar_months":
        raise ValueError("Spacing requires an integer calendar-month rule")
    year, zero_month = divmod(base.year * 12 + base.month - 1 + rule.value, 12)
    month = zero_month + 1
    day = min(base.day, monthrange(year, month)[1])
    anniversary = (year, month, day)
    # Compare even when the anniversary cannot be represented by a wire date.
    threshold = date(*anniversary) if date.min.year <= year <= date.max.year else None
    if facts.effective_on is not None:
        effective = facts.effective_on
        satisfied = (effective.year, effective.month, effective.day) >= anniversary
        check.status = "pass" if satisfied else "fail"
        check.reason = "satisfied" if satisfied else "violated"
    return check, threshold


def _integer_text(value: int) -> str:
    """Format positive integers without changing the runtime's digit safety limit."""
    groups: list[str] = []
    while value >= 1_000_000_000:
        value, group = divmod(value, 1_000_000_000)
        groups.append(f"{group:09d}")
    return str(value) + "".join(reversed(groups))


def _exact_cap(current: int, percentage: str) -> tuple[int, int, str]:
    """Return floor, remainder and canonical text using only scaled integers."""
    whole, _, fraction = percentage.partition(".")
    scale = 10 ** len(fraction)
    coefficient = int(whole) * scale + int(fraction or "0")
    numerator = current * (100 * scale + coefficient)
    denominator = 100 * scale
    floor, remainder = divmod(numerator, denominator)
    text = _integer_text(floor)
    if remainder:
        digits = str(remainder).zfill(len(fraction) + 2).rstrip("0")
        text += "." + digits
    return floor, remainder, text


def _guideline(
    facts: RentFacts, corpus: Corpus
) -> tuple[CheckResult, _Percentage | None, str | None]:
    check = CheckResult(
        id="guideline",
        status="unknown",
        reason="missing_fact",
        rule_ids=["exemption.s6_1"],
    )
    if facts.guideline_status == "exempt_s6_1":
        check.status = "not_applicable"
        check.reason = "exempt"
        return check, None, None
    if facts.guideline_status == "unknown":
        return check, None, None
    check.rule_ids = []
    if facts.effective_on is None:
        return check, None, None

    # The notice gate has already excluded unsupported years; Corpus is validated.
    identifier = cast(RuleId, f"guideline.{facts.effective_on.year}")
    rule = corpus.rule(identifier)
    if not isinstance(rule.value, str) or rule.unit != "percent":
        raise ValueError("Guideline requires an exact percentage string")
    percentage = cast(_Percentage, rule.value)
    check.rule_ids = [identifier]
    if facts.current_cents is None:
        return check, percentage, None

    floor, remainder, cap = _exact_cap(facts.current_cents, percentage)
    if facts.proposed_cents is None:
        return check, percentage, cap
    if facts.proposed_cents <= floor:
        check.status, check.reason = "pass", "satisfied"
    elif remainder and facts.proposed_cents == floor + 1:
        check.reason = "rounding_uncertain"
    else:
        check.status, check.reason = "fail", "violated"
    return check, percentage, cap


def _form(facts: RentFacts, corpus: Corpus) -> CheckResult:
    check = CheckResult(
        id="form", status="unknown", reason="missing_fact", rule_ids=["form.N1", "form.N2"]
    )
    if facts.guideline_status == "unknown":
        return check
    identifier: RuleId = "form.N1" if facts.guideline_status == "controlled" else "form.N2"
    check.rule_ids = (
        [identifier] if facts.guideline_status == "controlled" else ["exemption.s6_1", identifier]
    )
    if facts.form != "unknown":
        satisfied = facts.form == corpus.rule(identifier).value
        check.status = "pass" if satisfied else "fail"
        check.reason = "satisfied" if satisfied else "violated"
    return check


def rent_increase_check(facts: RentFacts, *, corpus: Corpus) -> ToolResult:
    """Combine notice, spacing, exact guideline and form checks without I/O.

    The caller supplies a validated, loaded corpus. An exemption applies only to
    the guideline check, and a passing result covers only the checked conditions.
    """
    notice = notice_deadline_check(
        NoticeFacts(
            scope=facts.scope,
            effective_on=facts.effective_on,
            served_on=facts.served_on,
            service_method=facts.service_method,
        ),
        corpus=corpus,
    )
    checks = [check.model_copy(deep=True) for check in notice.checks]
    earliest_spacing = None
    percentage = None
    cap = None
    status: ToolStatus
    if any(check.status == "not_applicable" for check in checks[:3]):
        status = "unsupported"
        for identifier in ("spacing", "guideline", "form"):
            checks.append(
                CheckResult(
                    id=identifier,
                    status="not_applicable",
                    reason="excluded_scope",
                    rule_ids=["scope.ordinary"],
                )
            )
    else:
        spacing, earliest_spacing = _spacing(facts, corpus)
        guideline, percentage, cap = _guideline(facts, corpus)
        checks.extend((spacing, guideline, _form(facts, corpus)))
        if facts.scope.ordinary == "unknown" or facts.scope.period_start == "unknown":
            status = "cannot_determine"
        elif any(check.status == "fail" for check in checks):
            status = "fails_checked_rules"
        elif any(check.status == "unknown" for check in checks):
            status = "cannot_determine"
        else:
            status = "passes_checked_rules"

    rule_ids = sorted({identifier for check in checks for identifier in check.rule_ids})
    for identifier in rule_ids:
        corpus.rule(identifier)
    return ToolResult(
        tool="rent_increase_check",
        status=status,
        checks=checks,
        deemed_served_on=notice.deemed_served_on,
        notice_days=notice.notice_days,
        earliest_notice_on=notice.earliest_notice_on,
        latest_deemed_service_on=notice.latest_deemed_service_on,
        latest_dispatch_on=notice.latest_dispatch_on,
        earliest_spacing_on=earliest_spacing,
        guideline_percent=percentage,
        cap_cents_exact=cap,
        rule_ids=rule_ids,
    )
