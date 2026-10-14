"""Synthetic keyed-grade wire fixtures; no semantic or remote acceptance claim."""

from copy import deepcopy
from typing import Any, get_args

from rent_navigator.security_cases import SecurityCaseId


def grade_only_wire(wire: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Translate fixture representation without repairing malformed values or IDs."""
    data = deepcopy(wire)
    for field in ("required_claims", "statements"):
        rows = data[field]
        if isinstance(rows, list) and all(isinstance(row, dict) and "id" in row for row in rows):
            ids = [row["id"] for row in rows]
            if all(isinstance(identifier, str) for identifier in ids) and len(set(ids)) == len(ids):
                data[field] = {
                    row["id"]: {key: value for key, value in row.items() if key != "id"}
                    for row in rows
                }
    policies = data["policy_violations"]
    if isinstance(policies, list) and all(isinstance(identifier, str) for identifier in policies):
        if len(set(policies)) == len(policies):
            data["policy_violations"] = {
                identifier: identifier in policies
                for identifier in dict.fromkeys([*policies, *get_args(SecurityCaseId)])
            }
    return data
