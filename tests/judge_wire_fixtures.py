"""Synthetic grade-only wire fixtures; no semantic or remote acceptance claim."""

from copy import deepcopy
from typing import Any


def grade_only_wire(wire: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    """Convert fixture ID maps to arrays, retaining all supplied fields and bad values.

    This helper does not remove extra fields, repair labels, pad IDs or infer grades.
    It accepts existing internal arrays unchanged for recording-fake compositions.
    """
    data = deepcopy(wire)
    for field in ("required_claims", "statements"):
        rows = data[field]
        if isinstance(rows, dict):
            data[field] = [
                {"id": identifier, **leaf} if isinstance(leaf, dict) else leaf
                for identifier, leaf in rows.items()
            ]
    return data
