"""Deterministic pattern redaction for user free text, not typed facts."""

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Final

_POLICY_VERSION: Final = "pattern-redaction-v3"
_HORIZONTAL: Final = r"[ \t\u00a0\u202f]"
_SEPARATOR: Final = r"[ \t\u00a0\u202f.-]*"
_LOCAL_ATOM: Final = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOMAIN_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"

# Scoped ASCII case folding keeps the outer Unicode token boundaries intact.
_EMAIL_PATTERN: Final = (
    r"(?<![\w.!#$%&'*+/=?^`{|}~-])"
    rf"(?ai:{_LOCAL_ATOM}(?:\.{_LOCAL_ATOM})*@"
    rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+)"
    r"(?![\w@-]|\.[\w.-])"
)
_PHONE_PATTERN: Final = (
    r"(?<![\w+])"
    rf"(?:\+?1{_SEPARATOR})?"
    rf"(?:\([2-9][0-9]{{2}}\)|[2-9][0-9]{{2}}){_SEPARATOR}"
    rf"[2-9][0-9]{{2}}{_SEPARATOR}[0-9]{{4}}"
    rf"(?:{_HORIZONTAL}*(?ai:x|ext\.?|#){_HORIZONTAL}*[0-9]{{1,6}})?"
    r"(?!\w)"
)
_POSTAL_PATTERN: Final = (
    r"(?<!\w)"
    r"(?ai:[abceghjklmnprstvxy][0-9][abceghjklmnprstvwxyz]"
    rf"(?:{_HORIZONTAL}+|-)?[0-9][abceghjklmnprstvwxyz][0-9])"
    r"(?!\w)"
)
_CURRENCY_PATTERN: Final = (
    rf"(?ai:C\$|\$|(?<!\w)CAD){_HORIZONTAL}*[+-]?{_HORIZONTAL}*"
    r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
    r"(?![0-9]|,[0-9]|\.[0-9])"
)
_FLAGS: Final = 0
_EMAIL: Final = re.compile(_EMAIL_PATTERN, _FLAGS)
_PHONE: Final = re.compile(_PHONE_PATTERN, _FLAGS)
_POSTAL: Final = re.compile(_POSTAL_PATTERN, _FLAGS)
_CURRENCY: Final = re.compile(_CURRENCY_PATTERN, _FLAGS)


def _policy_manifest() -> dict[str, object]:
    return {
        "version": _POLICY_VERSION,
        "patterns": {
            "email": _EMAIL_PATTERN,
            "phone": _PHONE_PATTERN,
            "postal": _POSTAL_PATTERN,
        },
        "flags": _FLAGS,
        "order": ["email", "phone", "postal"],
        "placeholders": {"email": "[EMAIL]", "phone": "[PHONE]", "postal": "[POSTAL]"},
        "email_processing": "replace the leftmost supported candidate, then rescan until stable",
        "pass_processing": "repeat the complete ordered pass until the string is unchanged",
        "currency": {
            "pattern": _CURRENCY_PATTERN,
            "flags": _FLAGS,
            "protection": (
                "preserve marked currency spans exactly; match phones wholly within each "
                "unprotected region using original outer token boundaries"
            ),
            "timing": "after email replacement, before phone replacement",
            "unlabelled_nanp": "redact",
        },
    }


def _policy_hash(material: Mapping[str, object]) -> str:
    canonical = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


REDACTION_POLICY_HASH: Final = _policy_hash(_policy_manifest())


def _redact_phones(text: str) -> str:
    currency_spans = tuple(match.span() for match in _CURRENCY.finditer(text))
    parts: list[str] = []
    cursor = 0
    for start, end in (*currency_spans, (len(text), len(text))):
        # Use the original string and its true end so both token boundaries survive.
        for match in _PHONE.finditer(text, cursor):
            if match.start() >= start:
                break
            if match.end() <= start:
                parts.extend((text[cursor : match.start()], "[PHONE]"))
                cursor = match.end()
        parts.extend((text[cursor:start], text[start:end]))
        cursor = end
    return "".join(parts)


def redact_text(text: str) -> str:
    """Replace supported contact patterns while preserving all unmatched text.

    Marked currency is protected; an unlabelled ten-digit NANP token is redacted.
    This policy does not recognize names, street addresses or obfuscated contacts.
    """
    if not isinstance(text, str):
        raise TypeError("redaction input must be a string")
    redacted = text
    while True:
        previous = redacted
        while True:
            redacted, count = _EMAIL.subn("[EMAIL]", redacted, count=1)
            if count == 0:
                break
        redacted = _POSTAL.sub("[POSTAL]", _redact_phones(redacted))
        if redacted == previous:
            return redacted
        # A changed pass consumes @ or ASCII digits; placeholders introduce neither.
