"""Offline native token evidence fidelity and strict reconstruction boundaries."""

import json
from decimal import Decimal
from typing import Any

import pytest
from anthropic.types import Message, MessageTokensCount
from anthropic.types import Usage as SDKUsage

from rent_navigator.eval.provider_evidence import (
    capture_provider_response,
    usage_cost_from_raw,
)
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, RequestedModel
from rent_navigator.provider import ProviderFailure, _usage
from rent_navigator.trace import cost_for_usage

FIELDS = {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"}


def response(**changes: Any) -> Message:
    fields: dict[str, Any] = {
        "id": "msg_synthetic_evidence",
        "type": "message",
        "role": "assistant",
        "model": ACTOR_MODEL,
        "content": [],
        "stop_reason": "end_turn",
    }
    return Message.model_construct(**fields).model_copy(update=changes)


def native_usage(**values: Any) -> SDKUsage:
    return SDKUsage.model_construct(**values)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("token", [True, "100", 100.0, None, -1, 100])
def test_native_token_json_roundtrip_reconstructs_original_strict_cost(
    field: str, token: Any
) -> None:
    values = {"input_tokens": 1000, "output_tokens": 100, field: token}
    original = response(usage=native_usage(**values))
    captured = capture_provider_response(original, message_fields=FIELDS)
    raw = json.loads(json.dumps(captured, allow_nan=False))
    assert raw["usage"] == values
    assert type(raw["usage"][field]) is type(token)
    assert usage_cost_from_raw(raw, ACTOR_MODEL) == cost_for_usage(ACTOR_MODEL, _usage(original))


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_absent_token_is_not_filled_with_null_or_zero(field: str) -> None:
    values = {"input_tokens": 1000, "output_tokens": 100}
    del values[field]
    original = response(usage=native_usage(**values))
    raw = capture_provider_response(original, message_fields=FIELDS)
    assert raw["usage"] == values and field not in raw["usage"]
    cost = usage_cost_from_raw(raw, ACTOR_MODEL)
    assert cost == cost_for_usage(ACTOR_MODEL, _usage(original))
    assert cost.actual_cost_usd is None and not cost.usage_complete


@pytest.mark.parametrize("present", [False, True])
def test_absent_and_null_usage_remain_distinct_unknown_observations(present: bool) -> None:
    original = response(**({"usage": None} if present else {}))
    raw = capture_provider_response(original, message_fields=FIELDS)
    assert ("usage" in raw) is present
    assert raw.get("usage") is None
    assert usage_cost_from_raw(raw, ACTOR_MODEL) == cost_for_usage(ACTOR_MODEL, None)


@pytest.mark.parametrize("token", [True, "100", 100.0, None, 0, 100])
def test_native_count_preserves_exact_scalar_type(token: Any) -> None:
    raw = capture_provider_response(
        MessageTokensCount.model_construct(input_tokens=token), message_fields=FIELDS
    )
    assert raw == {"input_tokens": token} and type(raw["input_tokens"]) is type(token)


def test_absent_count_remains_absent() -> None:
    fields: dict[str, Any] = {}
    assert (
        capture_provider_response(
            MessageTokensCount.model_construct(**fields), message_fields=FIELDS
        )
        == {}
    )


class Unsupported:
    def __repr__(self) -> str:
        raise AssertionError("Unsupported evidence must never be represented")


@pytest.mark.parametrize(
    "native",
    [
        "synthetic-private-token-value",
        {"input_tokens": 1000, "output_tokens": 100},
        native_usage(input_tokens=Unsupported(), output_tokens=100),
        native_usage(input_tokens=b"synthetic-private-token-value", output_tokens=100),
        native_usage(input_tokens={1, 2}, output_tokens=100),
        native_usage(input_tokens=float("nan"), output_tokens=100),
        native_usage(input_tokens=float("inf"), output_tokens=100),
    ],
)
def test_unsupported_native_evidence_fails_safely_without_repr(native: Any) -> None:
    with pytest.raises(ProviderFailure) as caught:
        capture_provider_response(response(usage=native), message_fields=FIELDS)
    assert caught.value.code == "provider_error"
    assert "synthetic-private" not in str(caught.value)


@pytest.mark.parametrize("token", [Unsupported(), b"private", {1}, float("nan")])
def test_unsupported_count_fails_safely(token: Any) -> None:
    with pytest.raises(ProviderFailure) as caught:
        capture_provider_response(
            MessageTokensCount.model_construct(input_tokens=token), message_fields=FIELDS
        )
    assert caught.value.code == "provider_error"


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
def test_valid_accounting_uses_unchanged_rates(model: RequestedModel) -> None:
    cost = usage_cost_from_raw(
        {"model": model, "usage": {"input_tokens": 1000, "output_tokens": 100}}, model
    )
    assert cost.actual_cost_usd == Decimal(".0015" if model == ACTOR_MODEL else ".003")
    assert cost.usage_complete


@pytest.mark.parametrize("model", [ACTOR_MODEL, JUDGE_MODEL])
def test_model_anomaly_discards_otherwise_known_pricing(model: RequestedModel) -> None:
    raw = {"model": "unexpected-model", "usage": {"input_tokens": 1000, "output_tokens": 100}}
    assert usage_cost_from_raw(raw, model) == cost_for_usage(model, None)


def test_unknown_usage_field_cannot_be_consumed_as_sdk_constructor_metadata() -> None:
    native = native_usage(input_tokens=1000, output_tokens=100).model_copy(
        update={"_fields_set": ["unapproved_usage_category"]}
    )
    original = response(usage=native)
    raw = capture_provider_response(original, message_fields=FIELDS)
    assert raw["usage"]["_fields_set"] == ["unapproved_usage_category"]
    assert _usage(original) is None
    assert usage_cost_from_raw(raw, ACTOR_MODEL) == cost_for_usage(ACTOR_MODEL, None)


@pytest.mark.parametrize("usage", [False, "100", 100, [], {"input_tokens": float("inf")}])
def test_impossible_serialized_usage_is_rejected(usage: Any) -> None:
    with pytest.raises(ValueError):
        usage_cost_from_raw({"model": ACTOR_MODEL, "usage": usage}, ACTOR_MODEL)
