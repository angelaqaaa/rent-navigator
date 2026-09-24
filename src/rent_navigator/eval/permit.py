"""Single-run funded grants; validation performs no network or provider work."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from rent_navigator.eval.models import EvaluationModel, _utc_timestamp
from rent_navigator.model_policy import ACTOR_MODEL, JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.models import CanonicalUUID, Sha256, SourceCommit
from rent_navigator.trace import Usd


def _permit_usd(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        return Decimal(value)
    raise ValueError("permit USD values require exact decimal strings")


PermitUsd = Annotated[Usd, BeforeValidator(_permit_usd)]


def _permit_schema_two(value: object) -> int:
    if type(value) is not int or value != 2:
        raise ValueError("live permit schema_version must be integer 2")
    return value


def _reservation(actor_calls: int, judge_calls: int) -> Decimal:
    return (
        actor_calls * MODEL_POLICIES[ACTOR_MODEL].reservation_usd
        + judge_calls * MODEL_POLICIES[JUDGE_MODEL].reservation_usd
    )


class LivePermit(EvaluationModel):
    """Prospective funding policy; validation does not confirm funds or issue authority."""

    schema_version: Annotated[Literal[2], BeforeValidator(_permit_schema_two)]
    repository: Literal["angelaqaaa/rent-navigator"]
    workflow: Literal[".github/workflows/ci.yml"]
    source_sha: SourceCommit
    run_id: Annotated[str, Field(pattern=r"^[1-9][0-9]*$")]
    run_attempt: Annotated[int, Field(ge=1, le=1)]
    batch_uuid: CanonicalUUID
    purpose: Literal["bootstrap", "regression"]
    baseline_sha256: Sha256 | None
    funded_slot: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")]
    max_actor_calls: Annotated[int, Field(ge=77, le=77)]
    max_judge_calls: Annotated[int, Field(ge=39, le=39)]
    reserved_usd: PermitUsd
    incurred_usd: PermitUsd
    held_usd: PermitUsd
    still_required_usd: PermitUsd
    development_cap_usd: PermitUsd
    provider_funding_usd: PermitUsd
    demo_reserved_usd: PermitUsd
    development_slots_remaining: Annotated[int, Field(ge=0, le=7)]
    future_live_batches_remaining: Annotated[int, Field(ge=0, le=2)]
    prior_ledger_sha256: Sha256
    issued_at_utc: Annotated[datetime, BeforeValidator(_utc_timestamp)]
    expires_at_utc: Annotated[datetime, BeforeValidator(_utc_timestamp)]

    @model_validator(mode="after")
    def funded_envelope(self) -> Self:
        gate = _reservation(self.max_actor_calls, self.max_judge_calls)
        if (
            self.reserved_usd != gate
            or self.still_required_usd
            != (
                _reservation(298, 160)
                + self.future_live_batches_remaining * gate
                + self.development_slots_remaining * _reservation(3, 1)
            )
            or self.development_cap_usd != Decimal("36")
            or self.provider_funding_usd != Decimal("45")
            or self.demo_reserved_usd != Decimal("9")
            or self.incurred_usd < Decimal("0.232356")
            or self.held_usd != 0
            or sum(
                Fraction(value)
                for value in (
                    self.incurred_usd,
                    self.held_usd,
                    self.reserved_usd,
                    self.still_required_usd,
                )
            )
            > Fraction(self.development_cap_usd)
        ):
            raise ValueError("permit does not preserve the funded budget and required reserves")
        if self.purpose == "bootstrap":
            if (
                self.baseline_sha256 is not None
                or self.funded_slot != "wp9_bootstrap_savings_repair"
                or self.future_live_batches_remaining != 2
                or self.development_slots_remaining != 7
            ):
                raise ValueError("bootstrap must preserve the correction reserved allocation")
        elif self.baseline_sha256 is None or self.future_live_batches_remaining > 1:
            raise ValueError(
                "regression requires its frozen baseline and remaining gate allocation"
            )
        if not timedelta(0) < self.expires_at_utc - self.issued_at_utc <= timedelta(days=1):
            raise ValueError("permit validity must be positive and at most one day")
        return self

    def validate_phase(self, baseline_phase: str, baseline_sha256: str | None) -> Self:
        if (
            self.purpose == "bootstrap"
            and (baseline_phase != "pending" or baseline_sha256 is not None)
        ) or (
            self.purpose == "regression"
            and (baseline_phase != "active" or baseline_sha256 != self.baseline_sha256)
        ):
            raise ValueError("permit purpose does not match the frozen baseline phase and bytes")
        return self

    def validate_context(
        self,
        *,
        repository: str,
        source_sha: str,
        workflow: str,
        run_id: str,
        run_attempt: int,
        now: datetime | None = None,
    ) -> Self:
        observed = (repository, source_sha, workflow, run_id, run_attempt)
        expected = (self.repository, self.source_sha, self.workflow, self.run_id, self.run_attempt)
        if type(run_attempt) is not int or observed != expected or run_attempt != 1:
            raise ValueError("permit does not match this repository, source and first run attempt")
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None or not self.issued_at_utc <= current < self.expires_at_utc:
            raise ValueError("permit is not currently valid")
        return self
