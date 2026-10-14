"""Closed judge-only request settings; serving defaults remain separate."""

from typing import Any


def judge_request_settings() -> dict[str, Any]:
    return {
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
    }
