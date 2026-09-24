"""Shared model admission, reservation, output limits and fixed token rates."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Literal

ACTOR_MODEL: Final = "claude-haiku-4-5-20251001"
JUDGE_MODEL: Final = "claude-sonnet-5"
RequestedModel = Literal["claude-haiku-4-5-20251001", "claude-sonnet-5"]


@dataclass(frozen=True)
class ModelPolicy:
    preflight_limit: int
    reserved_input_tokens: int
    max_output_tokens: int
    input_per_million: Decimal
    output_per_million: Decimal

    @property
    def reservation_usd(self) -> Decimal:
        return (
            self.reserved_input_tokens * self.input_per_million
            + self.max_output_tokens * self.output_per_million
        ) / 1_000_000


MODEL_POLICIES: Final[Mapping[RequestedModel, ModelPolicy]] = MappingProxyType(
    {
        ACTOR_MODEL: ModelPolicy(20000, 21000, 1200, Decimal("1"), Decimal("5")),
        JUDGE_MODEL: ModelPolicy(24000, 25000, 800, Decimal("2"), Decimal("10")),
    }
)


def policy_manifest() -> dict[str, dict[str, int | str]]:
    """Return JSON-compatible policy data for configuration and pricing identity."""
    return {
        model: {
            "preflight_limit": policy.preflight_limit,
            "reserved_input_tokens": policy.reserved_input_tokens,
            "max_output_tokens": policy.max_output_tokens,
            "input_per_million": str(policy.input_per_million),
            "output_per_million": str(policy.output_per_million),
        }
        for model, policy in MODEL_POLICIES.items()
    }
