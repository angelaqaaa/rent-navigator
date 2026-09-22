"""Synthetic contact patterns; these checks do not establish anonymity."""

import hashlib
import json
from typing import cast

import pytest

from rent_navigator import guards
from rent_navigator.guards import REDACTION_POLICY_HASH, redact_text


@pytest.mark.parametrize(
    "address",
    [
        "unit@example.invalid",
        "UNIT@EXAMPLE.INVALID",
        "first.last+unit@rent-example.invalid",
        "a!#$%&'*+/=?^_`{|}~-b@a-b.example.invalid",
        "0@0.1",
        "a.b.c@one.two.three",
    ],
)
def test_complete_ascii_email_patterns(address: str) -> None:
    assert (
        redact_text(f'Contact <{address}>, or "{address}".') == 'Contact <[EMAIL]>, or "[EMAIL]".'
    )


@pytest.mark.parametrize(
    "text",
    [
        "unit@example",
        "unit@-example.invalid",
        "unit@example-.invalid",
        "unit@example..invalid",
        "unit..name@example.invalid",
        "unit@example.invalid-",
        "unit@example.invalid..other",
        "unit＠example.invalid",
        "unit at example dot invalid",
        "éunit@example.invalid",
        "unit@exämple.invalid",
        "unit@example.invalíd",
        "K@example.invalid",
        "unit@ſite.invalid",
        "unit\n@example.invalid",
        "unit@\nexample.invalid",
    ],
)
def test_email_negatives_are_not_partially_redacted(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "phone",
    [
        "4165550123",
        "416-555-0123",
        "416.555.0123",
        "416 555 0123",
        "416\t555\t0123",
        "416\u00a0555\u00a00123",
        "416\u202f555\u202f0123",
        "(416)5550123",
        "(416) 555-0123",
        "14165550123",
        "+14165550123",
        "1-416-555-0123",
        "+1 (416) 555-0123",
        "+1\u202f(416)\u00a0555-0123",
        "4165550123x1",
        "4165550123 X 123456",
        "4165550123ext123",
        "4165550123 EXT 123",
        "4165550123 ext. 123",
        "4165550123#123",
        "4165550123 # 123",
        "(416) 555-0123\text.\t123456",
        "9999999999",
        "2002000000",
    ],
)
def test_complete_nanp_patterns(phone: str) -> None:
    assert redact_text(f"Call {phone}; thanks.") == "Call [PHONE]; thanks."


@pytest.mark.parametrize(
    "text",
    [
        "1165550123",
        "0165550123",
        "4161550123",
        "4160550123",
        "555-0123",
        "24165550123",
        "141655501234",
        "41655501234",
        "4165550123x1234567",
        "abc4165550123",
        "4165550123abc",
        "é4165550123",
        "4165550123é",
        "_4165550123_",
        "+4165550123",
        "+442079460958",
        "４１６５５５０１２３",
        "٤١٦٥٥٥٠١٢٣",
        "416–555–0123",
        "416\n555\n0123",
        "416\r555\r0123",
        "416\v555\v0123",
        "416\f555\f0123",
        "416\u2009555\u20090123",
    ],
)
def test_phone_negatives_are_not_partially_redacted(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "postal",
    [
        "M5V2T6",
        "m5v2t6",
        "M5V 2T6",
        "m5v-2t6",
        "M5V\t2T6",
        "M5V\u00a02T6",
        "M5V\u202f2T6",
        "M5V \t\u00a0\u202f2T6",
        "A0A0A0",
        "Y9Z9Z9",
    ],
)
def test_canadian_postal_patterns(postal: str) -> None:
    assert redact_text(f"Postal: ({postal}).") == "Postal: ([POSTAL])."


@pytest.mark.parametrize(
    "postal",
    [
        "D5V2T6",
        "F5V2T6",
        "I5V2T6",
        "O5V2T6",
        "Q5V2T6",
        "U5V2T6",
        "W5V2T6",
        "Z5V2T6",
        "M5D2T6",
        "M5V2U6",
        "M5V--2T6",
        "M5V–2T6",
        "M5V\n2T6",
        "M5V\r2T6",
        "M5V\v2T6",
        "M5V\u20092T6",
        "M５V２T６",
        "AM5V2T6",
        "M5V2T6A",
        "1M5V2T6",
        "M5V2T61",
        "éM5V2T6",
        "M5V2T6é",
    ],
)
def test_postal_negatives_are_not_partially_redacted(postal: str) -> None:
    assert redact_text(postal) == postal


@pytest.mark.parametrize(
    "amount",
    [
        "$4165550123.00",
        "$4165550123",
        "C$4165550123.00",
        "c$4165550123.00",
        "CAD 4165550123.00",
        "cad4165550123.00",
        "CaD\t4165550123.00",
        "$ -4165550123.00",
        "CAD +4165550123.00",
        "C$\u00a0-\u202f4165550123.00",
        "$4,165,550,123.00",
        "CAD 0.4165550123",
        "CAD 14165550123.0001",
        "$1,234.50",
        "CAD 1,259.80",
        "$0.00",
    ],
)
def test_explicit_currency_spans_survive_exactly(amount: str) -> None:
    text = f"Rent {amount}; contact 4165550123."
    assert redact_text(text) == f"Rent {amount}; contact [PHONE]."


def test_unlabelled_phone_shaped_numbers_are_phone_candidates() -> None:
    assert redact_text("Value 4165550123; CAD 4165550123.") == "Value [PHONE]; CAD 4165550123."


def test_currency_cannot_protect_across_a_line_break() -> None:
    assert redact_text("CAD\n4165550123\n$\r4165550123") == "CAD\n[PHONE]\n$\r[PHONE]"


@pytest.mark.parametrize(
    "text",
    [
        "The current rent is $1,234.50, proposed $1,259.80, effective 2027-04-01.",
        "Dates: 2026-09-01, September 1, 2026; guideline 2.1%, increase 2.4%.",
        "Names and street addresses are outside this policy: Pat Example, 12 Example St.",
        "[EMAIL] [PHONE] [POSTAL] unchanged; tabs\tand\r\nline endings stay.",
        "",
    ],
)
def test_non_contact_text_is_preserved_exactly(text: str) -> None:
    assert redact_text(text) == text


def test_all_replacements_preserve_unmatched_characters_and_are_idempotent() -> None:
    text = (
        "Contact unit@example.invalid at +1 (416) 555-0123; postal code M5V 2T6.\r\n"
        "The current rent is $1,234.50, the proposed rent is $1,259.80, effective 2027-04-01."
    )
    expected = (
        "Contact [EMAIL] at [PHONE]; postal code [POSTAL].\r\n"
        "The current rent is $1,234.50, the proposed rent is $1,259.80, effective 2027-04-01."
    )
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


def test_email_replacement_precedes_phone_and_postal_replacement() -> None:
    assert redact_text("4165550123@example.invalid M5V2T6@example.invalid") == "[EMAIL] [EMAIL]"


@pytest.mark.parametrize("value", [None, 123, True, b"private synthetic payload", ["private"]])
def test_non_string_arguments_fail_without_echoing_payload(value: object) -> None:
    with pytest.raises(TypeError) as caught:
        redact_text(cast(str, value))
    assert str(caught.value) == "redaction input must be a string"


def test_policy_hash_is_canonical_stable_and_independent_of_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material = guards._policy_manifest()
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    assert REDACTION_POLICY_HASH == expected
    assert len(REDACTION_POLICY_HASH) == 64
    assert guards._policy_hash(dict(reversed(tuple(material.items())))) == expected
    monkeypatch.setenv("REDACTION_POLICY_VERSION", "unrelated-environment-value")
    redact_text("Different synthetic input unit@example.invalid")
    assert guards._policy_hash(guards._policy_manifest()) == expected


@pytest.mark.parametrize(
    "key", ["version", "patterns", "flags", "order", "placeholders", "currency"]
)
def test_every_policy_component_changes_derived_hash(key: str) -> None:
    material = guards._policy_manifest()
    material[key] = "changed synthetic policy material"
    assert guards._policy_hash(material) != REDACTION_POLICY_HASH
