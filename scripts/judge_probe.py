"""Dump one fixed judge request offline or send it once for terminal inspection."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/judge_calibration_v2/J01.json"
PACKET_IDS = (*tuple(f"J{number:02}" for number in range(1, 14)), "R04")


def fixed_packet(packet_id: str) -> dict[str, Any]:
    if packet_id not in PACKET_IDS:
        raise ValueError("Invalid fixed probe packet ID")
    if packet_id == "R04":
        from rent_navigator.corpus import load_corpus
        from rent_navigator.eval.data import load_gold
        from rent_navigator.eval.judge import build_judge_packet, build_smoke_input

        corpus = load_corpus()
        case = next(
            case
            for case in load_gold(FIXTURE.parents[3] / "eval", corpus).cases
            if case.id == "R04"
        )
        return build_judge_packet(build_smoke_input(case, corpus))
    fixture = json.loads(FIXTURE.with_name(packet_id + ".json").read_bytes())
    if not isinstance(fixture, dict) or fixture.get("fixture_id") != packet_id:
        raise ValueError("Invalid fixed probe fixture")
    packet = fixture["provider_packet"]
    if not isinstance(packet, dict):
        raise ValueError("Invalid fixed probe packet")
    return packet


def build_payload(packet_id: str = "J01") -> dict[str, Any]:
    """Mirror RealJudge and ProviderAdapter without counting or constructing a client."""
    from rent_navigator.eval.judge import JUDGE_SYSTEM, judge_schema
    from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES

    packet = fixed_packet(packet_id)
    payload: dict[str, Any] = {
        "model": JUDGE_MODEL,
        "system": JUDGE_SYSTEM,
        "messages": [
            {"role": "user", "content": json.dumps(packet, sort_keys=True, separators=(",", ":"))}
        ],
        "thinking": {"type": "disabled"},
        "output_config": {"format": {"type": "json_schema", "schema": judge_schema(packet)}},
        "max_tokens": MODEL_POLICIES[JUDGE_MODEL].max_output_tokens,
        "stream": False,
        "service_tier": "standard_only",
    }
    assert payload["output_config"]["format"]["schema"] == judge_schema(packet)
    return payload


def dump_payload(path: Path, packet_id: str = "J01") -> None:
    encoded = json.dumps(
        build_payload(packet_id), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)


def send_payload(path: Path, packet_id: str = "J01") -> int:
    """Print only the permitted terminal fields; never record a response to disk."""
    logging.disable(logging.CRITICAL)

    from anthropic import Anthropic, APIError, APIStatusError

    payload = json.loads(path.read_bytes())
    if payload != build_payload(packet_id):
        raise ValueError("Probe input does not match the current fixed request")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or "ANTHROPIC_CUSTOM_HEADERS" in os.environ:
        raise ValueError("Invalid probe environment")
    try:
        with Anthropic(
            api_key=key,
            base_url="https://api.anthropic.com",
            max_retries=0,
            timeout=180.0,
        ) as client:
            response = client.messages.create(**payload)
    except APIStatusError as error:
        body = error.body
        detail = body.get("error", body) if type(body) is dict else None
        error_type = detail.get("type") if type(detail) is dict else None
        message = detail.get("message") if type(detail) is dict else None
        print(
            json.dumps(
                {
                    "http_status": error.status_code,
                    "sdk_exception": type(error).__name__,
                    "error_type": error_type if type(error_type) is str else None,
                    "error_message": message if type(message) is str else None,
                    "request_id": error.response.headers.get("request-id"),
                },
                ensure_ascii=False,
            )
        )
        return 1
    except APIError as error:
        print(
            json.dumps(
                {
                    "http_status": None,
                    "sdk_exception": type(error).__name__,
                    "error_type": None,
                    "error_message": error.message,
                    "request_id": None,
                },
                ensure_ascii=False,
            )
        )
        return 1
    text = "".join(block.text for block in response.content if block.type == "text")
    print(
        json.dumps(
            {
                "http_status": 200,
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump(mode="json"),
                "content": text[:200],
                "request_id": getattr(response, "_request_id", None),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Prevent imported project/SDK modules from creating bytecode during the probe.
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dump", action="store_true")
    modes.add_argument("--send", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--in", dest="input_path", type=Path)
    parser.add_argument("--packet-id", choices=PACKET_IDS, default="J01")
    args = parser.parse_args(argv)
    if args.dump:
        if args.out is None or args.input_path is not None:
            parser.error("--dump requires --out and does not accept --in")
        dump_payload(args.out, args.packet_id)
        return 0
    if args.input_path is None or args.out is not None:
        parser.error("--send requires --in and does not accept --out")
    return send_payload(args.input_path, args.packet_id)


if __name__ == "__main__":
    raise SystemExit(main())
