"""Closed local decode observations without response or exception payloads."""

from typing import Literal

from pydantic import TypeAdapter

from rent_navigator.models import ErrorCode
from rent_navigator.provider import ProviderFailure

JudgeDecodeReason = Literal[
    "layout_invalid",
    "model_mismatch",
    "stop_reason",
    "json_invalid",
    "keys_differ",
    "model_invalid",
    "coverage",
    "citation_applicability",
    "policy_ids",
    "packet_invalid",
]
_REASON: TypeAdapter[JudgeDecodeReason] = TypeAdapter(JudgeDecodeReason)


class LocalDecodeError(ValueError):
    def __init__(self, reason: JudgeDecodeReason) -> None:
        self.reason = _REASON.validate_python(reason, strict=True)
        super().__init__(self.reason)


class JudgeDecodeFailure(ProviderFailure):
    def __init__(
        self,
        reason: JudgeDecodeReason,
        *,
        code: Literal["provider_error", "invalid_generated_output"] | None = None,
    ) -> None:
        self.decode_rejection_reason = _REASON.validate_python(reason, strict=True)
        failure_code: ErrorCode = code or (
            "provider_error" if reason == "model_mismatch" else "invalid_generated_output"
        )
        super().__init__(failure_code)
