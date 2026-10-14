"""Private single-worker admission and UTC-day reservation accounting."""

import asyncio
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Lock

from rent_navigator.model_policy import ACTOR_MODEL, RequestedModel
from rent_navigator.provider import ProviderFailure, Reservation
from rent_navigator.trace import CostSummary, cost_for_usage

BODY_LIMIT_BYTES = 16384
POSTS_PER_HOUR = 60
IN_FLIGHT_LIMIT = 2
DEMO_DAILY_LIMIT = Decimal("0.30")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Admission clock must be timezone-aware")
    return value.astimezone(UTC)


class DailyBudget:
    """Refund the reservation's original day, even after the UTC boundary."""

    def __init__(
        self,
        limit: Decimal = DEMO_DAILY_LIMIT,
        *,
        clock: Callable[[], datetime] = utc_now,
        charges: MutableMapping[date, dict[int, Decimal]] | None = None,
    ) -> None:
        if not limit.is_finite() or not 0 <= limit <= DEMO_DAILY_LIMIT:
            raise ValueError("Demo daily limit must be within zero and 0.30 USD")
        self.limit = limit
        self._clock = clock
        self._charges = {} if charges is None else charges
        self._pending: dict[int, tuple[date, Reservation]] = {}
        self._number = max((number for day in self._charges.values() for number in day), default=0)
        self._stopped = False
        self._lock = Lock()

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def committed_usd(self) -> Decimal:
        with self._lock:
            return sum(self._charges.get(_utc(self._clock).date(), {}).values(), Decimal(0))

    def require_available(self) -> None:
        """Avoid an unnecessary preflight when even one reservation cannot fit."""
        amount = cost_for_usage(ACTOR_MODEL, None).reserved_cost_usd
        with self._lock:
            if self._stopped:
                raise ProviderFailure("provider_error")
            spent = sum(self._charges.get(_utc(self._clock).date(), {}).values(), Decimal(0))
            if spent + amount > self.limit:
                raise ProviderFailure("budget_exhausted")

    def reserve(self, model: RequestedModel) -> Reservation:
        amount = cost_for_usage(model, None).reserved_cost_usd
        with self._lock:
            if self._stopped:
                raise ProviderFailure("provider_error")
            day = _utc(self._clock).date()
            charges = self._charges.setdefault(day, {})
            if sum(charges.values(), Decimal(0)) + amount > self.limit:
                raise ProviderFailure("budget_exhausted")
            self._number += 1
            ticket = Reservation(self._number, model, amount)
            charges[ticket.number] = amount
            self._pending[ticket.number] = (day, ticket)
            return ticket

    def reconcile(
        self, reservation: Reservation, cost: CostSummary, *, reforecast: bool = False
    ) -> None:
        with self._lock:
            pending = self._pending.get(reservation.number)
            if pending is None or pending[1] != reservation:
                raise ProviderFailure("provider_error")
            day, _ = self._pending.pop(reservation.number)
            charge = (
                cost.actual_cost_usd if cost.actual_cost_usd is not None else reservation.amount
            )
            self._charges[day][reservation.number] = charge
            if not cost.usage_complete or reforecast or charge > reservation.amount:
                self._stopped = True


class Admission:
    def __init__(
        self,
        *,
        public_origin: str | None,
        budget: DailyBudget,
        clock: Callable[[], datetime] = utc_now,
        hourly_counts: MutableMapping[datetime, int] | None = None,
    ) -> None:
        self.public_origin = public_origin
        self.budget = budget
        self._clock = clock
        self._counts = {} if hourly_counts is None else hourly_counts
        self._lock = Lock()
        self._semaphore = asyncio.Semaphore(IN_FLIGHT_LIMIT)

    @asynccontextmanager
    async def enter(self, origin: str | None) -> AsyncIterator[None]:
        with self._lock:
            hour = _utc(self._clock).replace(minute=0, second=0, microsecond=0)
            count = self._counts.get(hour, 0)
            if count >= POSTS_PER_HOUR:
                raise ProviderFailure("rate_limited")
            self._counts[hour] = count + 1
        if origin is not None and origin != self.public_origin:
            raise ProviderFailure("invalid_request")
        if self._semaphore.locked():
            raise ProviderFailure("busy")
        await self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()
