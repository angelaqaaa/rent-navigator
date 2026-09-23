"""Synthetic returned-model faults stop spending without losing the billed response."""

import asyncio
import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from test_eval_live import ScriptedGatePort, make_fixture, permit

from rent_navigator.eval.live_budget import FundedMessages, LiveBudget
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import Usage, cost_for_usage


@pytest.mark.parametrize("mismatch_at", ["actor_selection", "actor_final", "judge"])
def test_model_mismatch_stops_before_any_following_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch_at: str
) -> None:
    original = ScriptedGatePort.create
    original_count = ScriptedGatePort.count_tokens
    models: list[str] = []
    counts: list[str] = []
    position = {"actor_selection": 1, "actor_final": 2, "judge": 3}[mismatch_at]

    async def changed(self: ScriptedGatePort, **kwargs: Any) -> Message:
        response = await original(self, **kwargs)
        models.append(kwargs["model"])
        if self.creates == position:
            returned = JUDGE_MODEL if kwargs["model"] == ACTOR_MODEL else ACTOR_MODEL
            return response.model_copy(update={"model": returned})
        return response

    async def counted(self: ScriptedGatePort, **kwargs: Any) -> MessageTokensCount:
        counts.append(kwargs["model"])
        return await original_count(self, **kwargs)

    monkeypatch.setattr(ScriptedGatePort, "create", changed)
    monkeypatch.setattr(ScriptedGatePort, "count_tokens", counted)
    fixture = make_fixture(tmp_path)
    assert len(models) == position
    assert len(counts) == position
    assert len(fixture.manifest.started_attempts) == 1
    assert not fixture.manifest.passed and not fixture.manifest.evaluation_complete
    receipt = json.loads((fixture.directory / "receipt.json").read_text())
    expected_cost = Decimal("0.0015") * min(position, 2)
    if mismatch_at == "judge":
        expected_cost += Decimal("0.003")
    assert Decimal(receipt["actual_usd"]) == expected_cost
    assert receipt["unresolved_hold_usd"] == "0"
    assert not receipt["complete"]
    reconciled = [event for event in receipt["events"] if event["event"] == "reconciled"]
    assert reconciled[-1]["reforecast"]
    assert reconciled[-1]["cost"]["usage_complete"]
    raw_name = "judge-raw.jsonl" if mismatch_at == "judge" else "raw-provider.jsonl"
    raw = [json.loads(line) for line in (fixture.directory / raw_name).read_text().splitlines()]
    generated = [
        item for item in raw if item["operation"] == "generation" and item["event"] == "response"
    ]
    assert generated[-1]["value"]["model"] != models[-1]
    assert generated[-1]["value"]["usage"]["input_tokens"] == 1000
    assert generated[-1]["value"]["usage"]["output_tokens"] == 100
    metadata_name = "judge-metadata.jsonl" if mismatch_at == "judge" else "metadata.jsonl"
    metadata = [
        json.loads(line) for line in (fixture.directory / metadata_name).read_text().splitlines()
    ]
    detail = [item for item in metadata if item["provider_operation"] == "generation"][-1]
    assert detail["returned_model_id"] == generated[-1]["value"]["model"]
    assert detail["usage_complete"]


def test_guard_returns_original_response_then_rejects_counts_and_generations() -> None:
    response = Message.model_validate(
        {
            "id": "msg_synthetic_wrong_model",
            "type": "message",
            "role": "assistant",
            "model": JUDGE_MODEL,
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )

    class Port:
        async def create(self, **kwargs: Any) -> Message:
            return response

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            raise AssertionError("Stopped guard must not call the port")

    stream = StringIO()
    budget = LiveBudget(permit(), stream, config_hash="5" * 64)
    budget.bind(uuid4())
    reservation = budget.reserve(ACTOR_MODEL)
    guard = FundedMessages(Port(), budget=budget)
    assert asyncio.run(guard.create(model=ACTOR_MODEL)) is response
    assert budget.stopped
    budget.reconcile(
        reservation, cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100))
    )
    assert budget.receipt().actual_usd == Decimal("0.0015")
    assert not budget.receipt().complete
    assert budget.events[-1].reforecast
    with pytest.raises(ProviderFailure):
        asyncio.run(guard.count_tokens(model=ACTOR_MODEL))
    with pytest.raises(ProviderFailure):
        asyncio.run(guard.create(model=ACTOR_MODEL))
    with pytest.raises(ProviderFailure):
        budget.reserve(JUDGE_MODEL)


def test_receipt_retains_before_counts_and_permit_after_counts() -> None:
    grant = permit()
    budget = LiveBudget(grant, StringIO(), config_hash="5" * 64)
    receipt = budget.receipt()
    assert receipt.future_live_batches_before == 4
    assert receipt.permit.future_live_batches_remaining == 3
    assert receipt.development_slots_before == 14
    assert receipt.permit.development_slots_remaining == 14
    assert receipt.permit.funded_slot == grant.funded_slot
    assert receipt.permit.prior_ledger_sha256 == grant.prior_ledger_sha256
