"""Synthetic returned-model faults stop spending without losing the billed response."""

import asyncio
import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from anthropic.types import Message, MessageTokensCount
from test_eval_live import ScriptedGatePort, make_fixture, permit

from rent_navigator.eval.live_budget import FundedMessages, LiveBudget
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import PRICING_HASH, Usage, cost_for_usage


@pytest.mark.parametrize("mismatch_at", ["actor_selection", "actor_final", "judge"])
@pytest.mark.parametrize("returned_kind", ["other_model", "missing", "invalid"])
def test_model_mismatch_stops_before_any_following_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch_at: str, returned_kind: str
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
            returned = (
                (JUDGE_MODEL if kwargs["model"] == ACTOR_MODEL else ACTOR_MODEL)
                if returned_kind == "other_model"
                else None
                if returned_kind == "missing"
                else "invalid/model"
            )
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
    expected_cost = Decimal("0.0015") * (position - 1)
    assert Decimal(receipt["actual_usd"]) == expected_cost
    assert Decimal(receipt["unresolved_hold_usd"]) == (
        Decimal("0.024") if mismatch_at == "judge" else Decimal("0.019")
    )
    assert not receipt["complete"]
    reconciled = [event for event in receipt["events"] if event["event"] == "reconciled"]
    assert reconciled[-1]["reforecast"]
    assert not reconciled[-1]["cost"]["usage_complete"]
    assert reconciled[-1]["cost"]["actual_cost_usd"] is None
    assert reconciled[-1]["cost"]["input_tokens"] is None
    assert reconciled[-1]["cost"]["output_tokens"] is None
    raw_name = "judge-raw.jsonl" if mismatch_at == "judge" else "raw-provider.jsonl"
    raw = [json.loads(line) for line in (fixture.directory / raw_name).read_text().splitlines()]
    generated = [
        item for item in raw if item["operation"] == "generation" and item["event"] == "response"
    ]
    assert generated[-1]["value"].get("model") != models[-1]
    assert generated[-1]["value"]["usage"]["input_tokens"] == 1000
    assert generated[-1]["value"]["usage"]["output_tokens"] == 100
    metadata_name = "judge-metadata.jsonl" if mismatch_at == "judge" else "metadata.jsonl"
    metadata = [
        json.loads(line) for line in (fixture.directory / metadata_name).read_text().splitlines()
    ]
    detail = [item for item in metadata if item["provider_operation"] == "generation"][-1]
    assert detail["returned_model_id"] == (
        generated[-1]["value"]["model"] if returned_kind == "other_model" else None
    )
    assert not detail["usage_complete"]
    assert detail["actual_cost_usd"] is None
    row = json.loads((fixture.directory / "results.jsonl").read_text().splitlines()[0])
    if mismatch_at == "judge":
        assert row["serving_cost_usd"] == "0.003" and row["usage_complete"]
        assert row["judge_cost_usd"] is None and row["judge"] is None
    else:
        assert row["serving_cost_usd"] is None and not row["usage_complete"]
        assert row["judge_cost_usd"] is None and row["judge"] is None


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
    assert budget.receipt().actual_usd == Decimal(0)
    assert budget.receipt().unresolved_hold_usd == Decimal("0.019")
    assert budget.ledger.committed_usd == Decimal("0.019")
    assert budget.events[-1].cost is not None
    assert budget.events[-1].cost.actual_cost_usd is None
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


def test_actor_raw_verifier_rejects_known_cost_for_model_anomaly() -> None:
    from rent_navigator.eval.evidence import verify_raw_links
    from rent_navigator.eval.recording import RawProviderRecord
    from rent_navigator.trace import MetadataSink, TraceContext, TraceRecord, TraceRecorder

    batch, attempt, trace_id = uuid4(), uuid4(), uuid4()
    stream = StringIO()
    context = TraceContext(
        attempt_id=attempt,
        trace_id=trace_id,
        phase="analysis",
        source_commit="3" * 40,
        config_hash="5" * 64,
        corpus_hash="6" * 64,
        pricing_hash=PRICING_HASH,
    )
    with TraceRecorder(context, MetadataSink(stream)) as trace:
        with trace.provider_call(ACTOR_MODEL) as call:
            call.usage = Usage(input_tokens=1000, output_tokens=100)
            call.returned_model_id = JUDGE_MODEL
            call.response_code = "provider_error"
    records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
    values: list[dict[str, Any]] = [
        {"model": ACTOR_MODEL},
        {
            "id": "msg_synthetic_mismatch",
            "type": "message",
            "role": "assistant",
            "model": JUDGE_MODEL,
            "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        },
    ]
    events: tuple[Literal["request", "response"], ...] = ("request", "response")
    raw = [
        RawProviderRecord(
            run_id=batch,
            attempt_id=attempt,
            trace_id=trace_id,
            phase="analysis",
            operation="generation",
            operation_index=1,
            event=event,
            value=value,
        )
        for event, value in zip(events, values, strict=True)
    ]
    with pytest.raises(ValueError, match="model anomaly"):
        verify_raw_links(raw, records, batch)


@pytest.mark.parametrize("returned_model", [JUDGE_MODEL, None, "invalid/model"])
def test_rehashed_known_cost_model_anomaly_rejected_even_in_failed_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returned_model: str | None
) -> None:
    from test_eval_live import rehash, verify

    original = ScriptedGatePort.create

    async def changed(self: ScriptedGatePort, **kwargs: Any) -> Message:
        response = await original(self, **kwargs)
        return response.model_copy(update={"model": returned_model})

    monkeypatch.setattr(ScriptedGatePort, "create", changed)
    fixture = make_fixture(tmp_path)
    # A properly held, incomplete attempt remains auditable without passing.
    audited = verify(fixture, require_pass=False)
    assert not audited.evaluation_complete and not audited.passed
    directory = fixture.directory
    priced = cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100))
    priced_json = priced.model_dump(mode="json")
    metadata_path = directory / "metadata.jsonl"
    metadata = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    for record in metadata:
        if record["record_kind"] == "endpoint" or record["provider_operation"] == "generation":
            record.update(priced_json)
    metadata_path.write_text("".join(json.dumps(record) + "\n" for record in metadata))
    row_path = directory / "results.jsonl"
    row = json.loads(row_path.read_text())
    row.update(input_tokens=1000, output_tokens=100, usage_complete=True, serving_cost_usd="0.0015")
    row_path.write_text(json.dumps(row) + "\n")
    budget_path = directory / "budget.jsonl"
    events = [json.loads(line) for line in budget_path.read_text().splitlines()]
    events[-1]["cost"] = priced_json
    budget_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["events"][-1]["cost"] = priced_json
    receipt["actual_usd"] = "0.0015"
    receipt["unresolved_hold_usd"] = "0"
    receipt_path.write_text(json.dumps(receipt))
    rehash(directory)
    with pytest.raises(ValueError, match="model anomaly"):
        verify(fixture, require_pass=False)


def test_model_anomaly_keeps_prior_verified_cost_and_current_full_hold() -> None:
    budget = LiveBudget(permit(), StringIO(), config_hash="5" * 64)
    budget.bind(uuid4())
    prior = budget.reserve(ACTOR_MODEL)
    known = cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100))
    budget.reconcile(prior, known)
    current = budget.reserve(JUDGE_MODEL)
    budget.require_reforecast()
    budget.reconcile(
        current, cost_for_usage(JUDGE_MODEL, Usage(input_tokens=1000, output_tokens=100))
    )
    receipt = budget.receipt()
    assert receipt.actual_usd == Decimal("0.0015")
    assert receipt.unresolved_hold_usd == Decimal("0.024")
    assert budget.ledger.committed_usd == Decimal("0.0255")
    assert not receipt.complete


def test_unrelated_stop_does_not_erase_verified_exact_model_cost() -> None:
    budget = LiveBudget(permit(), StringIO(), config_hash="5" * 64)
    budget.bind(uuid4())
    current = budget.reserve(ACTOR_MODEL)
    # The shared stop state is not itself evidence of a returned-model anomaly.
    budget.counts[ACTOR_MODEL] = budget.permit.max_actor_calls
    with pytest.raises(ProviderFailure):
        budget.reserve(ACTOR_MODEL)
    budget.reconcile(
        current, cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100))
    )
    receipt = budget.receipt()
    assert receipt.actual_usd == Decimal("0.0015")
    assert receipt.unresolved_hold_usd == 0
    assert budget.events[-1].cost is not None and budget.events[-1].cost.usage_complete


def test_failed_audit_cannot_drop_a_valid_returned_model_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_eval_live import rehash, verify

    original = ScriptedGatePort.create

    async def changed(self: ScriptedGatePort, **kwargs: Any) -> Message:
        response = await original(self, **kwargs)
        return response.model_copy(update={"model": JUDGE_MODEL})

    monkeypatch.setattr(ScriptedGatePort, "create", changed)
    fixture = make_fixture(tmp_path)
    verify(fixture, require_pass=False)
    metadata_path = fixture.directory / "metadata.jsonl"
    records = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    generated = next(record for record in records if record["provider_operation"] == "generation")
    assert generated["returned_model_id"] == JUDGE_MODEL
    generated["returned_model_id"] = None
    metadata_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    rehash(fixture.directory)
    with pytest.raises(ValueError, match="returned identity"):
        verify(fixture, require_pass=False)


def test_exact_model_normal_cost_is_still_settled() -> None:
    budget = LiveBudget(permit(), StringIO(), config_hash="5" * 64)
    budget.bind(uuid4())
    current = budget.reserve(ACTOR_MODEL)
    budget.reconcile(
        current, cost_for_usage(ACTOR_MODEL, Usage(input_tokens=1000, output_tokens=100))
    )
    receipt = budget.receipt()
    assert receipt.actual_usd == Decimal("0.0015")
    assert receipt.unresolved_hold_usd == 0
    assert receipt.complete
    assert not budget.events[-1].reforecast
