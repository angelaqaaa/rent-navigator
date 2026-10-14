"""Faithful token evidence and strict reconstruction for recorded provider calls."""

import math
from collections.abc import Collection, Mapping
from typing import Any

from anthropic.types import Message, MessageTokensCount
from anthropic.types import Usage as SDKUsage

from rent_navigator.model_policy import MODEL_POLICIES, RequestedModel
from rent_navigator.provider import ProviderFailure, _usage
from rent_navigator.trace import CostSummary, cost_for_usage


def _json_value(value: Any) -> Any:
    """Copy only native JSON values, without coercion or arbitrary serialization."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json_value(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("Provider token evidence contains an unsupported value")


def capture_provider_response(
    response: Message | MessageTokensCount, *, message_fields: Collection[str]
) -> dict[str, Any]:
    """Preserve native usage/count types before the enclosing SDK JSON serializer."""
    try:
        if isinstance(response, Message):
            raw = response.model_dump(
                mode="json",
                warnings=False,
                exclude_none=True,
                include=set(message_fields) - {"usage"},
            )
            if "usage" in message_fields and "usage" in response.model_fields_set:
                native = response.usage
                if native is None:
                    raw["usage"] = None
                elif isinstance(native, SDKUsage):
                    raw["usage"] = _json_value(
                        native.model_dump(mode="python", warnings=False, exclude_unset=True)
                    )
                else:
                    raise ValueError("Provider usage is not an SDK usage object")
            return dict(_json_value(raw))
        if isinstance(response, MessageTokensCount):
            return (
                {"input_tokens": _json_value(response.input_tokens)}
                if "input_tokens" in response.model_fields_set
                else {}
            )
        raise ValueError("Unexpected provider response type")
    except Exception:
        raise ProviderFailure("provider_error") from None


def usage_cost_from_raw(value: Mapping[str, Any], requested_model: RequestedModel) -> CostSummary:
    """Apply runtime usage rules to preserved raw types, including failed calls."""
    raw_usage = value.get("usage")
    if raw_usage is not None and type(raw_usage) is not dict:
        raise ValueError("Recorded usage must be an object, null, or absent")
    _json_value(raw_usage)
    if value.get("model") != requested_model:
        return cost_for_usage(requested_model, None)
    # Construction reproduces the observation; only _usage decides valid billing.
    empty: dict[str, Any] = {}
    usage = (
        SDKUsage.model_construct(**empty).model_copy(update=dict(raw_usage))
        if isinstance(raw_usage, dict)
        else None
    )
    fields: dict[str, Any] = {"usage": usage}
    response = Message.model_construct(**fields)
    return cost_for_usage(requested_model, _usage(response))


def requires_input_reforecast(model: RequestedModel, cost: CostSummary) -> bool:
    """A proved input overage requires stopping even below the dollar reservation."""
    return (
        cost.input_tokens is not None
        and cost.input_tokens > MODEL_POLICIES[model].reserved_input_tokens
    )
