"""Synthetic exact-run grants cannot refresh the shared development balance."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from rent_navigator.eval.permit import LivePermit

NOW = datetime(2026, 9, 23, 15, tzinfo=UTC)


def permit_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": "angelaqaaa/rent-navigator",
        "workflow": ".github/workflows/ci.yml",
        "source_sha": "a" * 40,
        "run_id": "123456",
        "run_attempt": 1,
        "batch_uuid": "00000000-0000-0000-0000-000000000019",
        "purpose": "bootstrap",
        "baseline_sha256": None,
        "funded_slot": "wp9-bootstrap",
        "max_actor_calls": 77,
        "max_judge_calls": 39,
        "reserved_usd": "2.399",
        "incurred_usd": "0.069685",
        "held_usd": "0",
        "still_required_usd": "17.752",
        "development_cap_usd": "21",
        "provider_funding_usd": "30",
        "demo_reserved_usd": "9",
        "development_slots_remaining": 13,
        "future_live_batches_remaining": 3,
        "prior_ledger_sha256": "b" * 64,
        "issued_at_utc": NOW.isoformat(),
        "expires_at_utc": (NOW + timedelta(hours=2)).isoformat(),
    }


def test_one_run_permit_preserves_existing_budget_and_is_context_bound() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    context: dict[str, Any] = dict(
        repository=permit.repository,
        source_sha=permit.source_sha,
        workflow=permit.workflow,
        run_id=permit.run_id,
        run_attempt=1,
        now=NOW,
    )
    assert permit.validate_context(**context) is permit
    for change in (
        dict(repository="fork/repo"),
        dict(source_sha="c" * 40),
        dict(workflow="other.yml"),
        dict(run_id="123457"),
        dict(run_attempt=2),
        dict(now=NOW - timedelta(seconds=1)),
        dict(now=NOW + timedelta(hours=2)),
        dict(now=NOW.replace(tzinfo=None)),
    ):
        with pytest.raises(ValueError):
            permit.validate_context(**{**context, **change})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("run_attempt", True),
        ("run_attempt", 2),
        ("max_actor_calls", 78),
        ("max_judge_calls", 40),
        ("reserved_usd", "2.4"),
        ("incurred_usd", "0"),
        ("incurred_usd", "1"),
        ("held_usd", "0.001"),
        ("still_required_usd", "0"),
        ("development_cap_usd", "30"),
        ("provider_funding_usd", "31"),
        ("demo_reserved_usd", "0"),
        ("development_slots_remaining", 15),
        ("prior_ledger_sha256", "unknown"),
        ("expires_at_utc", NOW.isoformat()),
        ("expires_at_utc", (NOW + timedelta(days=2)).isoformat()),
        ("purpose", "release"),
        ("extra", "secret"),
    ],
)
def test_invalid_or_unfunded_grants_are_rejected_before_clients(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**permit_payload(), field: value}))


def regression_payload() -> dict[str, Any]:
    return {
        **permit_payload(),
        "purpose": "regression",
        "baseline_sha256": "c" * 64,
        "future_live_batches_remaining": 2,
        "development_slots_remaining": 13,
        "still_required_usd": "15.353",
        "incurred_usd": "0.5",
    }


def test_regression_preserves_release_future_gates_and_remaining_development_slots() -> None:
    permit = LivePermit.model_validate_json(json.dumps(regression_payload()))
    assert permit.validate_phase("active", "c" * 64) is permit
    for phase, digest in (("pending", None), ("active", "d" * 64), ("active", None)):
        with pytest.raises(ValueError):
            permit.validate_phase(phase, digest)
    bootstrap = LivePermit.model_validate_json(json.dumps(permit_payload()))
    assert bootstrap.validate_phase("pending", None) is bootstrap
    for phase, digest in (("active", "c" * 64), ("pending", "c" * 64), ("unknown", None)):
        with pytest.raises(ValueError):
            bootstrap.validate_phase(phase, digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_sha256", None),
        ("future_live_batches_remaining", 3),
        ("future_live_batches_remaining", True),
        ("development_slots_remaining", -1),
        ("still_required_usd", "9.502"),
        ("still_required_usd", "15.352"),
        ("incurred_usd", "4"),
    ],
)
def test_regression_cannot_invent_a_lower_reserve_or_more_funding(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**regression_payload(), field: value}))


def test_exact_development_cap_is_accepted_but_not_one_cent_more() -> None:
    value = {**permit_payload(), "incurred_usd": "0.849"}
    permit = LivePermit.model_validate_json(json.dumps(value))
    assert permit.incurred_usd + permit.reserved_usd + permit.still_required_usd == 21
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**value, "incurred_usd": "0.859"}))


@pytest.mark.parametrize("field", ["baseline_sha256", "future_live_batches_remaining", "held_usd"])
def test_nullable_baseline_and_budget_counts_remain_required(field: str) -> None:
    value = permit_payload()
    del value[field]
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("field", ["incurred_usd", "held_usd", "still_required_usd"])
def test_negative_budget_values_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**permit_payload(), field: "-0.001"}))


@pytest.mark.parametrize(
    "field",
    [
        "reserved_usd",
        "incurred_usd",
        "held_usd",
        "still_required_usd",
        "development_cap_usd",
        "provider_funding_usd",
        "demo_reserved_usd",
    ],
)
@pytest.mark.parametrize("form", ["float", "true", "false"])
def test_budget_wire_values_require_exact_decimal_strings(field: str, form: str) -> None:
    value = permit_payload()
    value[field] = float(value[field]) if form == "float" else form == "true"
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "run_attempt",
        "max_actor_calls",
        "max_judge_calls",
        "development_slots_remaining",
        "future_live_batches_remaining",
    ],
)
@pytest.mark.parametrize("form", ["float", "string", "true", "false"])
def test_permit_integer_fields_reject_numeric_coercion(field: str, form: str) -> None:
    value = permit_payload()
    original = value[field]
    value[field] = {
        "float": float(original),
        "string": str(original),
        "true": True,
        "false": False,
    }[form]
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


def test_bootstrap_cannot_relabel_smaller_regression_reserves_with_correct_arithmetic() -> None:
    value = {
        **permit_payload(),
        "future_live_batches_remaining": 2,
        "development_slots_remaining": 13,
        "still_required_usd": "15.353",
    }
    with pytest.raises(ValidationError, match="initial reserved allocation"):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "field", ["held_usd", "development_cap_usd", "provider_funding_usd", "demo_reserved_usd"]
)
def test_integer_usd_wire_tokens_are_also_rejected(field: str) -> None:
    value = permit_payload()
    value[field] = int(value[field])
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


def test_decimal_objects_remain_usable_internally_and_serialize_as_strings() -> None:
    from decimal import Decimal

    original = LivePermit.model_validate_json(json.dumps(permit_payload()))
    internal = original.model_dump()
    assert isinstance(internal["reserved_usd"], Decimal)
    assert LivePermit.model_validate(internal) == original
    wire = json.loads(original.model_dump_json())
    assert all(isinstance(value, str) for field, value in wire.items() if field.endswith("_usd"))
    assert LivePermit.model_validate_json(original.model_dump_json()) == original


def test_tiny_positive_over_cap_cannot_round_down_into_a_valid_grant() -> None:
    value = {**permit_payload(), "incurred_usd": "0.84900000000000000000000000001"}
    with pytest.raises(ValidationError, match="funded budget"):
        LivePermit.model_validate_json(json.dumps(value))


def test_conditional_post_smoke_bootstrap_preserves_thirteen_development_slots() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    assert permit.development_slots_remaining == 13
    assert permit.future_live_batches_remaining == 3
    assert str(permit.still_required_usd) == "17.752"


@pytest.mark.parametrize("slots", [*range(13), 14])
def test_bootstrap_rejects_every_other_slot_count_even_with_correct_reserve(slots: int) -> None:
    from decimal import Decimal

    required = Decimal("9.502") + 3 * Decimal("2.399") + slots * Decimal("0.081")
    value = {
        **permit_payload(),
        "development_slots_remaining": slots,
        "still_required_usd": str(required),
    }
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("slots", [0, 13])
def test_regression_preserves_the_revised_development_slot_bounds(slots: int) -> None:
    from decimal import Decimal

    required = Decimal("9.502") + 2 * Decimal("2.399") + slots * Decimal("0.081")
    value = {
        **regression_payload(),
        "development_slots_remaining": slots,
        "still_required_usd": str(required),
    }
    assert LivePermit.model_validate_json(json.dumps(value)).development_slots_remaining == slots


def test_regression_cannot_restore_the_consumed_fourteenth_slot() -> None:
    value = {
        **regression_payload(),
        "development_slots_remaining": 14,
        "still_required_usd": "15.434",
    }
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))
