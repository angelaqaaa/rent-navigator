"""Durable reservations for one explicitly funded serial evaluation grant."""

import json
import os
from decimal import Decimal
from io import UnsupportedOperation
from typing import Any, Literal, Protocol, TextIO
from uuid import UUID

from anthropic.types import Message, MessageTokensCount

from rent_navigator.eval.permit import LivePermit
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, RequestedModel
from rent_navigator.models import CanonicalUUID, Sha256, StrictModel
from rent_navigator.provider import MessagesPort, ProviderFailure, Reservation, SpendLedger
from rent_navigator.trace import PRICING_HASH, CostSummary, NonNegativeInt, Usd, cost_for_usage


class BudgetEvent(StrictModel):
    event: Literal["generation_start", "reconciled"]
    batch_uuid: CanonicalUUID
    attempt_id: CanonicalUUID
    call_id: NonNegativeInt
    model: RequestedModel
    reserved_usd: Usd
    cost: CostSummary | None
    reforecast: bool


class BudgetReceipt(StrictModel):
    permit: LivePermit
    config_hash: Sha256
    pricing_hash: Sha256
    events: list[BudgetEvent]
    actual_usd: Usd
    unresolved_hold_usd: Usd
    actor_calls: NonNegativeInt
    judge_calls: NonNegativeInt
    future_live_batches_before: NonNegativeInt
    development_slots_before: NonNegativeInt
    complete: bool


def _write(stream: TextIO, value: str) -> None:
    stream.write(value + "\n")
    stream.flush()
    try:
        descriptor = stream.fileno()
    except (AttributeError, UnsupportedOperation):
        # In-memory test sinks have no file descriptor.
        return
    os.fsync(descriptor)


class LiveBudget:
    """Shared actor/judge allowance; count-only requests never reserve money."""

    def __init__(self, permit: LivePermit, stream: TextIO, *, config_hash: str) -> None:
        self.permit = permit
        self.stream = stream
        self.config_hash = config_hash
        self.ledger = SpendLedger(permit.reserved_usd)
        self.events: list[BudgetEvent] = []
        self.attempt_id: UUID | None = None
        self.counts: dict[RequestedModel, int] = {ACTOR_MODEL: 0, JUDGE_MODEL: 0}
        self._stopped = False
        _write(
            stream,
            json.dumps(
                {
                    "event": "grant",
                    "permit": permit.model_dump(mode="json"),
                    "config_hash": config_hash,
                    "pricing_hash": PRICING_HASH,
                }
            ),
        )

    def bind(self, attempt_id: UUID) -> None:
        self.attempt_id = attempt_id

    def require_reforecast(self) -> None:
        """Stop admission while allowing the current response to retain its usage."""
        self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped or self.ledger.stopped

    def reserve(self, model: RequestedModel) -> Reservation:
        limit = self.permit.max_actor_calls if model == ACTOR_MODEL else self.permit.max_judge_calls
        if self.stopped or self.attempt_id is None or self.counts[model] >= limit:
            self._stopped = True
            raise ProviderFailure("budget_exhausted")
        ticket = self.ledger.reserve(model)
        self.counts[model] += 1
        event = BudgetEvent(
            event="generation_start",
            batch_uuid=self.permit.batch_uuid,
            attempt_id=self.attempt_id,
            call_id=ticket.number,
            model=model,
            reserved_usd=ticket.amount,
            cost=None,
            reforecast=False,
        )
        self.events.append(event)
        _write(self.stream, event.model_dump_json())
        return ticket

    def reconcile(
        self, reservation: Reservation, cost: CostSummary, *, reforecast: bool = False
    ) -> None:
        reforecast = reforecast or self._stopped
        self.ledger.reconcile(reservation, cost, reforecast=reforecast)
        original = next(
            e
            for e in self.events
            if e.event == "generation_start" and e.call_id == reservation.number
        )
        event = BudgetEvent(
            event="reconciled",
            batch_uuid=self.permit.batch_uuid,
            attempt_id=original.attempt_id,
            call_id=reservation.number,
            model=reservation.model,
            reserved_usd=reservation.amount,
            cost=cost,
            reforecast=reforecast,
        )
        self.events.append(event)
        _write(self.stream, event.model_dump_json())

    def receipt(self) -> BudgetReceipt:
        return receipt_from_events(self.permit, self.config_hash, self.events)


class ReforecastBudget(Protocol):
    @property
    def stopped(self) -> bool: ...

    def require_reforecast(self) -> None: ...


class FundedMessages:
    """Preserve mismatched responses but stop every subsequent provider operation."""

    def __init__(self, port: MessagesPort, *, budget: ReforecastBudget) -> None:
        self._port, self._budget = port, budget

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        if self._budget.stopped:
            raise ProviderFailure("provider_error")
        return await self._port.count_tokens(**kwargs)

    async def create(self, **kwargs: Any) -> Message:
        if self._budget.stopped:
            raise ProviderFailure("provider_error")
        response = await self._port.create(**kwargs)
        if getattr(response, "model", None) != kwargs.get("model"):
            self._budget.require_reforecast()
        return response


def receipt_from_events(
    permit: LivePermit, config_hash: str, events: list[BudgetEvent]
) -> BudgetReceipt:
    starts: dict[int, BudgetEvent] = {}
    reconciled: dict[int, BudgetEvent] = {}
    counts = {ACTOR_MODEL: 0, JUDGE_MODEL: 0}
    complete = True
    for event in events:
        if event.batch_uuid != permit.batch_uuid:
            raise ValueError("Budget batch mismatch")
        if event.reserved_usd != cost_for_usage(event.model, None).reserved_cost_usd:
            raise ValueError("Reservation differs from the fixed model price")
        if event.event == "generation_start":
            if event.call_id != len(starts) + 1 or event.cost is not None or event.reforecast:
                raise ValueError("Invalid generation reservation order")
            starts[event.call_id] = event
            counts[event.model] += 1
        else:
            start = starts.get(event.call_id)
            if (
                start is None
                or event.call_id in reconciled
                or event.cost is None
                or (event.attempt_id, event.model, event.reserved_usd)
                != (start.attempt_id, start.model, start.reserved_usd)
            ):
                raise ValueError("Invalid generation reconciliation")
            reconciled[event.call_id] = event
            complete = complete and event.cost.usage_complete and not event.reforecast
    if counts[ACTOR_MODEL] > permit.max_actor_calls or counts[JUDGE_MODEL] > permit.max_judge_calls:
        raise ValueError("Generation count exceeds the permit")
    actual, held = Decimal(0), Decimal(0)
    for number, start in starts.items():
        result = reconciled.get(number)
        if result is None or result.cost is None or result.cost.actual_cost_usd is None:
            held += start.reserved_usd
            complete = False
        else:
            actual += result.cost.actual_cost_usd
            if result.cost.actual_cost_usd > start.reserved_usd:
                complete = False
    if actual + held > permit.reserved_usd:
        complete = False
    return BudgetReceipt(
        permit=permit,
        config_hash=config_hash,
        pricing_hash=PRICING_HASH,
        events=events,
        actual_usd=actual,
        unresolved_hold_usd=held,
        actor_calls=counts[ACTOR_MODEL],
        judge_calls=counts[JUDGE_MODEL],
        future_live_batches_before=permit.future_live_batches_remaining + 1,
        development_slots_before=permit.development_slots_remaining,
        complete=complete,
    )
