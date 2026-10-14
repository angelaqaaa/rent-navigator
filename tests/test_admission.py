"""Synthetic UTC accounting, rate windows and reject-without-queue admission."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from rent_navigator.admission import Admission, DailyBudget
from rent_navigator.model_policy import ACTOR_MODEL
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import Usage, cost_for_usage


class Clock:
    value = datetime(2026, 10, 13, 23, 59, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def test_reservation_refund_stays_on_original_day_after_midnight() -> None:
    clock = Clock()
    charges: dict[date, dict[int, Decimal]] = {}
    budget = DailyBudget(clock=clock, charges=charges)
    old = budget.reserve(ACTOR_MODEL)
    old_day = clock.value.date()
    clock.value += timedelta(minutes=2)
    new = budget.reserve(ACTOR_MODEL)
    assert old.number != new.number
    assert budget.committed_usd == Decimal("0.027")
    budget.reconcile(old, cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100)))
    assert budget.committed_usd == Decimal("0.027")
    assert charges[old_day][old.number] == Decimal("0.0015")
    budget.reconcile(new, cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100)))
    assert budget.committed_usd == Decimal("0.0015")
    with pytest.raises(ProviderFailure):
        budget.reconcile(old, cost_for_usage(ACTOR_MODEL, None))


def test_missing_usage_keeps_reservation_and_stops_further_calls() -> None:
    budget = DailyBudget()
    ticket = budget.reserve(ACTOR_MODEL)
    budget.reconcile(ticket, cost_for_usage(ACTOR_MODEL, None))
    assert budget.committed_usd == Decimal("0.027") and budget.stopped
    with pytest.raises(ProviderFailure):
        budget.reserve(ACTOR_MODEL)


def test_daily_reservations_cannot_overdraw_or_cross_into_next_day() -> None:
    clock = Clock()
    budget = DailyBudget(clock=clock)
    for _ in range(11):
        budget.reserve(ACTOR_MODEL)
    assert budget.committed_usd == Decimal("0.297")
    with pytest.raises(ProviderFailure) as error:
        budget.reserve(ACTOR_MODEL)
    assert error.value.code == "budget_exhausted"
    clock.value += timedelta(days=1)
    assert budget.committed_usd == 0
    assert budget.reserve(ACTOR_MODEL).amount == Decimal("0.027")


@pytest.mark.parametrize("limit", ["-0.1", "0.31", "NaN", "Infinity"])
def test_demo_daily_limit_rejects_invalid_or_larger_caps(limit: str) -> None:
    with pytest.raises(ValueError):
        DailyBudget(Decimal(limit))


def test_origin_absence_does_not_bypass_global_rate_and_hour_reset() -> None:
    clock = Clock()
    admission = Admission(public_origin="https://example.test", budget=DailyBudget(), clock=clock)

    async def run() -> None:
        for _ in range(60):
            async with admission.enter(None):
                pass
        with pytest.raises(ProviderFailure) as caught:
            async with admission.enter(None):
                pass
        assert caught.value.code == "rate_limited"
        clock.value += timedelta(minutes=2)
        async with admission.enter(None):
            pass

    asyncio.run(run())


def test_two_inflight_requests_reject_third_and_release_after_failure() -> None:
    admission = Admission(public_origin=None, budget=DailyBudget())

    async def run() -> None:
        async with admission.enter(None), admission.enter(None):
            with pytest.raises(ProviderFailure) as error:
                async with admission.enter(None):
                    pass
            assert error.value.code == "busy"
        with pytest.raises(ValueError):
            async with admission.enter(None):
                raise ValueError("synthetic failure")
        async with admission.enter(None), admission.enter(None):
            pass

    asyncio.run(run())
