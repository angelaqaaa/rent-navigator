"""Synthetic future permits verify fixed funding; fixtures authorize no execution."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from rent_navigator.eval.models import Activation
from rent_navigator.eval.permit import LivePermit, _reservation

NOW = datetime(2026, 9, 23, 15, tzinfo=UTC)


def permit_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "repository": "angelaqaaa/rent-navigator",
        "workflow": ".github/workflows/ci.yml",
        "source_sha": "a" * 40,
        "run_id": "123456",
        "run_attempt": 1,
        "batch_uuid": "00000000-0000-0000-0000-000000000019",
        "purpose": "bootstrap",
        "baseline_sha256": None,
        "funded_slot": "wp9_bootstrap_savings_repair",
        "max_actor_calls": 77,
        "max_judge_calls": 39,
        "reserved_usd": "4.341",
        "incurred_usd": "0.232356",
        "held_usd": "0",
        "still_required_usd": "26.981",
        "development_cap_usd": "36",
        "provider_funding_usd": "50",
        "demo_reserved_usd": "9",
        "development_slots_remaining": 7,
        "future_live_batches_remaining": 2,
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
        ("schema_version", 1),
        ("schema_version", True),
        ("run_attempt", True),
        ("run_attempt", 2),
        ("max_actor_calls", 78),
        ("max_judge_calls", 40),
        ("reserved_usd", "2.4"),
        ("incurred_usd", "0"),
        ("incurred_usd", "0.172127"),
        ("incurred_usd", "0.232355"),
        ("incurred_usd", "5"),
        ("held_usd", "0.001"),
        ("still_required_usd", "0"),
        ("development_cap_usd", "30"),
        ("development_cap_usd", "21"),
        ("provider_funding_usd", "31"),
        ("provider_funding_usd", "30"),
        ("provider_funding_usd", "45"),
        ("provider_funding_usd", "49"),
        ("provider_funding_usd", "51"),
        ("development_cap_usd", "41"),
        ("demo_reserved_usd", "14"),
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


def test_confirmed_funding_keeps_five_unallocated_and_existing_call_limits() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    assert permit.provider_funding_usd == Decimal("50")
    assert permit.development_cap_usd == Decimal("36")
    assert permit.demo_reserved_usd == Decimal("9")
    assert permit.provider_funding_usd - permit.development_cap_usd - permit.demo_reserved_usd == 5
    assert (permit.max_actor_calls, permit.max_judge_calls) == (77, 39)
    assert permit.reserved_usd == Decimal("4.341")
    assert permit.still_required_usd == Decimal("26.981")


def regression_payload() -> dict[str, Any]:
    return {
        **permit_payload(),
        "purpose": "regression",
        "baseline_sha256": "c" * 64,
        "future_live_batches_remaining": 1,
        "development_slots_remaining": 7,
        "still_required_usd": "22.640",
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
        ("future_live_batches_remaining", 2),
        ("future_live_batches_remaining", True),
        ("development_slots_remaining", -1),
        ("still_required_usd", "17.326"),
        ("still_required_usd", "22.639"),
        ("incurred_usd", "10"),
    ],
)
def test_regression_cannot_invent_a_lower_reserve_or_more_funding(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**regression_payload(), field: value}))


def test_exact_development_cap_is_accepted_but_not_one_cent_more() -> None:
    value = {**permit_payload(), "incurred_usd": "4.678"}
    permit = LivePermit.model_validate_json(json.dumps(value))
    assert permit.incurred_usd + permit.reserved_usd + permit.still_required_usd == 36
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps({**value, "incurred_usd": "4.688"}))


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
        "future_live_batches_remaining": 1,
        "development_slots_remaining": 7,
        "still_required_usd": "22.640",
    }
    with pytest.raises(ValidationError, match="correction reserved allocation"):
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
    value = {**permit_payload(), "incurred_usd": "4.67800000000000000000000000001"}
    with pytest.raises(ValidationError, match="funded budget"):
        LivePermit.model_validate_json(json.dumps(value))


def test_correction_bootstrap_preserves_exact_slot_and_remaining_allocations() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    assert permit.development_slots_remaining == 7
    assert permit.future_live_batches_remaining == 2
    assert permit.funded_slot == "wp9_bootstrap_savings_repair"
    assert str(permit.still_required_usd) == "26.981"


@pytest.mark.parametrize("slots", [*range(7), *range(8, 15)])
def test_bootstrap_rejects_every_other_slot_count_even_with_correct_reserve(slots: int) -> None:
    from decimal import Decimal

    required = Decimal("17.326") + 2 * Decimal("4.341") + slots * Decimal("0.139")
    value = {
        **permit_payload(),
        "development_slots_remaining": slots,
        "still_required_usd": str(required),
    }
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("slots", [0, 7])
def test_regression_preserves_the_revised_development_slot_bounds(slots: int) -> None:
    from decimal import Decimal

    required = Decimal("17.326") + Decimal("4.341") + slots * Decimal("0.139")
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
        "still_required_usd": "23.613",
    }
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "slot",
    [
        "wp9-bootstrap",
        "wp9_bootstrap",
        "wp9_bootstrap_correction",
        "synthetic-fixture",
        "regression",
        "other",
    ],
)
def test_bootstrap_requires_the_named_existing_correction_slot(slot: str) -> None:
    with pytest.raises(ValidationError, match="correction reserved allocation"):
        LivePermit.model_validate_json(json.dumps({**permit_payload(), "funded_slot": slot}))


@pytest.mark.parametrize("batches", [0, 1, 3])
def test_bootstrap_rejects_other_future_counts_with_correct_arithmetic(batches: int) -> None:
    from decimal import Decimal

    required = Decimal("17.326") + batches * Decimal("4.341") + 7 * Decimal("0.139")
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(
            json.dumps(
                {
                    **permit_payload(),
                    "future_live_batches_remaining": batches,
                    "still_required_usd": str(required),
                }
            )
        )


@pytest.mark.parametrize("batches", [0, 1])
def test_regression_remaining_gate_bounds_preserve_exact_reserves(batches: int) -> None:
    from decimal import Decimal

    required = Decimal("17.326") + batches * Decimal("4.341") + 7 * Decimal("0.139")
    value = {
        **regression_payload(),
        "future_live_batches_remaining": batches,
        "still_required_usd": str(required),
    }
    permit = LivePermit.model_validate_json(json.dumps(value))
    assert permit.future_live_batches_remaining == batches
    assert permit.still_required_usd == required


def test_regression_cannot_restore_consumed_correction_even_with_correct_arithmetic() -> None:
    value = {
        **regression_payload(),
        "future_live_batches_remaining": 2,
        "still_required_usd": "26.981",
        "incurred_usd": "0.232356",
    }
    with pytest.raises(ValidationError, match="remaining gate allocation"):
        LivePermit.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("baseline", [None, "c" * 64])
def test_correction_allocation_cannot_be_relabelled_as_regression(baseline: str | None) -> None:
    value = {**permit_payload(), "purpose": "regression", "baseline_sha256": baseline}
    with pytest.raises(ValidationError, match="remaining gate allocation"):
        LivePermit.model_validate_json(json.dumps(value))


def test_correction_forecast_reconciles_without_consuming_or_refunding_a_slot() -> None:
    from decimal import Decimal

    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    assert permit.incurred_usd + permit.reserved_usd + permit.still_required_usd == Decimal(
        "31.554356"
    )
    assert permit.development_cap_usd - (
        permit.incurred_usd + permit.reserved_usd + permit.still_required_usd
    ) == Decimal("4.445644")


def test_only_live_permit_advances_its_strict_schema_version() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    wire = json.loads(permit.model_dump_json())
    assert type(wire["schema_version"]) is int and wire["schema_version"] == 2
    assert (
        Activation.model_validate_json(
            '{"schema_version":1,"baseline_phase":"pending"}'
        ).schema_version
        == 1
    )
    with pytest.raises(ValidationError):
        Activation.model_validate_json('{"schema_version":2,"baseline_phase":"pending"}')


@pytest.mark.parametrize(
    "actor_calls,judge_calls,expected",
    [
        (1, 0, "0.027"),
        (0, 1, "0.058"),
        (26, 7, "1.108"),
        (298, 160, "17.326"),
        (77, 39, "4.341"),
        (3, 1, "0.139"),
        (576, 291, "32.430"),
        (550, 284, "31.322"),
        (473, 245, "26.981"),
    ],
)
def test_prospective_reservations_match_independent_approved_arithmetic(
    actor_calls: int, judge_calls: int, expected: str
) -> None:
    assert _reservation(actor_calls, judge_calls) == Decimal(expected)


def test_diagnostics_and_remaining_work_close_without_claiming_confirmed_funding() -> None:
    permit = LivePermit.model_validate_json(json.dumps(permit_payload()))
    diagnostic_reserve = _reservation(26, 7)
    future_reserve = diagnostic_reserve + permit.reserved_usd + permit.still_required_usd
    assert future_reserve == Decimal("32.430")
    forecast = future_reserve + permit.incurred_usd
    assert forecast == Decimal("32.662356")
    assert forecast + permit.demo_reserved_usd == Decimal("41.662356")
    assert forecast + permit.demo_reserved_usd - Decimal("30") == Decimal("11.662356")
    assert permit.development_cap_usd - forecast == Decimal("3.337644")


def test_regression_cannot_restore_an_eighth_generic_allowance() -> None:
    value = {
        **regression_payload(),
        "development_slots_remaining": 8,
        "still_required_usd": "22.779",
    }
    with pytest.raises(ValidationError):
        LivePermit.model_validate_json(json.dumps(value))
