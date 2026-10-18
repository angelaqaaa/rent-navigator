"""Synthetic usage metadata through live adapters, retained costs and judge projection."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from anthropic.types import RedactedThinkingBlock, ThinkingBlock
from anthropic.types import Usage as SDKUsage
from test_eval_judge import harness, message
from test_provider import RecordingMessages, assert_accounting, perform, reply

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.evidence import verify_raw_links
from rent_navigator.eval.judge import build_smoke_input
from rent_navigator.eval.provider_evidence import capture_provider_response, usage_cost_from_raw
from rent_navigator.eval.recording import RawProviderRecord
from rent_navigator.eval.runner import JudgeAccounting, JudgeFailure, JudgeInput
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, MODEL_POLICIES, RequestedModel
from rent_navigator.provider import SpendLedger

SYNTHETIC_USAGE: dict[str, Any] = {
    "input_tokens": 3409,
    "output_tokens": 242,
    "service_tier": "standard",
    "inference_geo": "not_available",
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
SYNTHETIC_SECRET = "synthetic-speed-reasoning-and-signature"


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
@pytest.mark.parametrize(
    "extra,accepted",
    [
        ({}, True),
        ({"speed": None}, True),
        ({"speed": "standard"}, True),
        ({"speed": "fast"}, False),
        ({"speed": "STANDARD"}, False),
        ({"speed": ""}, False),
        ({"speed": False}, False),
        ({"speed": 0}, False),
        ({"speed": 1}, False),
        ({"speed": 0.0}, False),
        ({"speed": []}, False),
        ({"speed": {}}, False),
        ({"speed": {"mode": "standard"}}, False),
        ({"speed": "standard", "unrecognized_billed_tokens": 1}, False),
        ({"speed": "standard", "unrecognized_billed_tokens": "standard"}, False),
        ({"speed": "standard", "service_tier": "priority"}, False),
        ({"speed": "standard", "cache_creation_input_tokens": 1}, False),
        ({"speed": "standard", "unrecognized_billed_tokens": 0}, True),
        ({"speed": "standard", "unrecognized_billed_tokens": None}, True),
        ({"speed": "standard", "cache_read_input_tokens": 0}, True),
    ],
)
def test_speed_live_and_retained_billing_agree_without_request_changes(
    model: RequestedModel, extra: dict[str, Any], accepted: bool
) -> None:
    usage = SDKUsage.model_validate({**SYNTHETIC_USAGE, **extra})
    assert "speed" not in SDKUsage.model_fields
    if "speed" in extra:
        assert usage.model_extra is not None and usage.model_extra["speed"] == extra["speed"]
    response = reply(model=model, usage=usage)
    port, ledger = RecordingMessages(response), SpendLedger(Decimal("1"))
    outcome = asyncio.run(perform(port, ledger, model=model))
    actual = ("0.004619" if model == ACTOR_MODEL else "0.009238") if accepted else None
    assert_accounting(
        outcome,
        actual=actual,
        reserved=str(MODEL_POLICIES[model].reservation_usd),
        complete=accepted,
    )
    assert (outcome.failure is None) is accepted
    assert ledger.stopped is ledger.reforecast_required is (not accepted)
    assert len(port.counts) == len(port.creates) == 1
    assert all("speed" not in payload for payload in (*port.counts, *port.creates))
    assert "speed" not in outcome.log

    raw = capture_provider_response(response, message_fields={"model", "usage"})
    if "speed" in extra:
        assert raw["usage"]["speed"] == extra["speed"]
    else:
        assert "speed" not in raw["usage"]
    cost = usage_cost_from_raw(raw, model)
    assert cost.actual_cost_usd == (Decimal(actual) if actual is not None else None)
    assert cost.usage_complete is accepted
    assert cost.reserved_cost_usd == MODEL_POLICIES[model].reservation_usd
    if model == ACTOR_MODEL:
        run_id = UUID("10000000-0000-4000-8000-000000000073")
        terminal = outcome.records[-1]
        assert terminal.attempt_id is not None
        records = [
            RawProviderRecord.model_validate_json(
                json.dumps(
                    {
                        "run_id": str(run_id),
                        "attempt_id": str(terminal.attempt_id),
                        "trace_id": str(terminal.trace_id),
                        "phase": "analysis",
                        "operation": operation,
                        "operation_index": index,
                        "event": event,
                        "context_provenance": {
                            "retrieved_evidence_ids": [],
                            "foundation_evidence_ids": [],
                            "initial_context_evidence_ids": [],
                        },
                        "value": value,
                    }
                )
            )
            for operation, index, event, value in (
                ("count_tokens", 1, "request", port.counts[0]),
                ("count_tokens", 1, "response", {"input_tokens": 1000}),
                ("generation", 2, "request", port.creates[0]),
                ("generation", 2, "response", raw),
            )
        ]
        verify_raw_links(records, outcome.records, run_id)
    if not accepted:
        retry = asyncio.run(perform(port, ledger, model=model, trace_number=2))
        assert retry.failure is not None and len(port.creates) == 1


@pytest.fixture(scope="module")
def judge_value() -> JudgeInput:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    return build_smoke_input(case, corpus)


@pytest.mark.parametrize(
    "extra,accepted",
    [
        ({"speed": None}, True),
        ({"speed": "standard"}, True),
        ({"speed": "fast"}, False),
        ({"speed": False}, False),
        ({"speed": "standard", "future_charge": 1}, False),
    ],
)
def test_speed_judge_projection_accounting_and_full_replay(
    judge_value: JudgeInput, extra: dict[str, Any], accepted: bool
) -> None:
    test = harness(judge_value)
    original = message(judge_value)
    usage = SDKUsage.model_validate(
        {**SYNTHETIC_USAGE, **extra, "output_tokens_details": {"thinking_tokens": 40}}
    )
    test.port.response = original.model_copy(
        update={
            "usage": usage,
            "content": [
                ThinkingBlock(
                    type="thinking", thinking=SYNTHETIC_SECRET, signature=SYNTHETIC_SECRET
                ),
                RedactedThinkingBlock(type="redacted_thinking", data=SYNTHETIC_SECRET),
                *original.content,
            ],
        }
    )
    accounting: JudgeAccounting
    if accepted:
        evaluated = test.run()
        accounting = evaluated
        test.verify(accounting, evaluated.judgment)
    else:
        with pytest.raises(JudgeFailure) as caught:
            test.run()
        assert caught.value.code == "provider_error"
        accounting = caught.value.accounting
        test.verify(accounting)
    assert accounting.cost.actual_cost_usd == (Decimal("0.009238") if accepted else None)
    assert accounting.cost.usage_complete is accepted
    assert accounting.cost.reserved_cost_usd == Decimal("0.21384")
    assert test.budget.committed_usd == (Decimal("0.009238") if accepted else Decimal("0.21384"))
    response = test.records()[-1].value
    assert response["usage"]["speed"] == extra["speed"]
    assert response["usage"]["output_tokens"] == 242
    assert response["judge_projection"]["thinking_tokens"] == 40
    assert SYNTHETIC_SECRET not in test.raw.getvalue() + test.metadata.getvalue()
    assert all("speed" not in payload for payload in (*test.port.counts, *test.port.creates))
