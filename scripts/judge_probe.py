"""Dump one fixed judge request offline or send it once for terminal inspection."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from rent_navigator.eval.runner import JudgeInput
    from rent_navigator.trace import CostSummary

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/judge_calibration_v2/J01.json"
PACKET_IDS = (*tuple(f"J{number:02}" for number in range(1, 14)), "R04")
DIAGNOSTIC_PROFILES = ("current", "medium", "high-output")


@dataclass(frozen=True)
class RetainedReplay:
    payload: dict[str, Any]
    value: JudgeInput


def load_replay(
    directory: Path,
    *,
    raw_sha256: str,
    manifest_sha256: str,
    attempt_id: str,
    trace_id: str,
    operation_index: int,
) -> RetainedReplay:
    """Bind one immutable generation request to its actual retained result and packet."""
    try:
        return _load_replay(
            directory,
            raw_sha256=raw_sha256,
            manifest_sha256=manifest_sha256,
            attempt_id=attempt_id,
            trace_id=trace_id,
            operation_index=operation_index,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Invalid retained judge replay") from None


def _load_replay(
    directory: Path,
    *,
    raw_sha256: str,
    manifest_sha256: str,
    attempt_id: str,
    trace_id: str,
    operation_index: int,
) -> RetainedReplay:
    from rent_navigator.corpus import load_corpus
    from rent_navigator.eval.collection import REAL_ARTIFACT_FILES, RealCollectionManifest
    from rent_navigator.eval.data import load_gold
    from rent_navigator.eval.judge import (
        JUDGE_SYSTEM,
        RawJudgeRecord,
        build_judge_packet,
        judge_config_hash,
        judge_schema,
    )
    from rent_navigator.eval.models import ResultRow
    from rent_navigator.eval.runner import JudgeInput, approved_facts_from_case
    from rent_navigator.judge_profile import judge_request_settings
    from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
    from rent_navigator.models import AskResponse
    from rent_navigator.trace import PRICING_HASH

    if (
        any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (raw_sha256, manifest_sha256))
        or any(str(UUID(value)) != value for value in (attempt_id, trace_id))
        or type(operation_index) is not int
        or operation_index <= 0
    ):
        raise ValueError("Invalid replay selectors")
    manifest_bytes = (directory / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise ValueError("Manifest identity differs")
    manifest = RealCollectionManifest.model_validate_json(manifest_bytes)
    if set(manifest.files) != set(REAL_ARTIFACT_FILES):
        raise ValueError("Manifest artifact inventory differs")
    files = {name: (directory / name).read_bytes() for name in REAL_ARTIFACT_FILES}
    if any(sha256(data).hexdigest() != manifest.files[name] for name, data in files.items()):
        raise ValueError("Retained artifact identity differs")
    if manifest.files["judge-raw-provider.jsonl"] != raw_sha256:
        raise ValueError("Retained request identity differs")
    records = [
        RawJudgeRecord.model_validate_json(line)
        for line in files["judge-raw-provider.jsonl"].splitlines()
    ]
    selected = [
        record
        for record in records
        if str(record.attempt_id) == attempt_id
        and str(record.trace_id) == trace_id
        and record.operation_index == operation_index
        and record.operation == "generation"
        and record.event == "request"
    ]
    rows = [ResultRow.model_validate_json(line) for line in files["results.jsonl"].splitlines()]
    matched = [row for row in rows if str(row.attempt_id) == attempt_id]
    if len(selected) != 1 or len(matched) != 1:
        raise ValueError("Replay selection must be unique")
    record, row = selected[0], matched[0]
    accounting = manifest.judge_accounting.get(attempt_id)
    if (
        record.run_id != manifest.run_id
        or row.run_id != manifest.run_id
        or row.source_sha != manifest.source_sha
        or accounting is None
        or accounting.config_hash != judge_config_hash()
        or manifest.judge_config_hash != judge_config_hash()
        or not isinstance(row.response, AskResponse)
        or row.response.status != "answered"
        or row.response.attempt_id != row.attempt_id
        or (row.case_id, row.arm, row.repeat) != ("Q04", "baseline", 0)
        or not any(
            not item.warmup
            and item.entry.case_id == row.case_id
            and item.entry.arm == row.arm
            and item.entry.repeat == row.repeat
            for item in manifest.started_attempts
        )
    ):
        raise ValueError("Replay result binding differs")
    calls = [
        call
        for call in accounting.records
        if str(call.trace_id) == trace_id
        and call.attempt_id == row.attempt_id
        and call.record_kind == "provider_call"
        and call.provider_operation == "generation"
        and call.provider_call_index == operation_index
        and call.requested_model_id == JUDGE_MODEL
    ]
    if len(calls) != 1:
        raise ValueError("Replay call binding differs")
    counts = [
        item
        for item in records
        if item.run_id == manifest.run_id
        and str(item.attempt_id) == attempt_id
        and str(item.trace_id) == trace_id
        and item.operation_index == operation_index - 1
        and item.operation == "count_tokens"
        and item.event == "response"
    ]
    if (
        len(counts) != 1
        or type(counts[0].value.get("input_tokens")) is not int
        or not 0 <= counts[0].value["input_tokens"] <= MODEL_POLICIES[JUDGE_MODEL].preflight_limit
    ):
        raise ValueError("Retained preflight binding differs")
    corpus = load_corpus()
    dataset = load_gold(FIXTURE.parents[3] / "eval", corpus)
    if (
        manifest.corpus_hash != corpus.corpus_hash
        or row.corpus_hash != corpus.corpus_hash
        or manifest.gold_hash != dataset.gold_hash
        or row.gold_hash != dataset.gold_hash
        or manifest.approval_hash != dataset.approval_hash
        or manifest.pricing_hash != PRICING_HASH
        or row.pricing_hash != PRICING_HASH
    ):
        raise ValueError("Replay fact provenance differs")
    case = next(case for case in dataset.cases if case.id == row.case_id)
    value = JudgeInput(
        approved_facts=approved_facts_from_case(case),
        required_claims=tuple(case.required_claims),
        evidence=tuple(corpus.chunk(identifier) for identifier in sorted(set(case.evidence_ids))),
        expected_tool_result=case.expected_tool_result,
        response=row.response,
        actual_tool_result=row.actual_tool_result,
        cited_evidence=tuple(corpus.chunk(citation.id) for citation in row.response.citations),
    )
    payload = record.value
    if set(payload) != {
        "max_tokens",
        "messages",
        "model",
        "output_config",
        "service_tier",
        "stream",
        "system",
        "thinking",
        "timeout",
    }:
        raise ValueError("Replay request fields differ")
    timeout = payload["timeout"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 180:
        raise ValueError("Invalid retained timeout")
    settings = judge_request_settings()
    messages = payload["messages"]
    if (
        payload["model"] != JUDGE_MODEL
        or payload["system"] != JUDGE_SYSTEM
        or type(payload["max_tokens"]) is not int
        or payload["max_tokens"] != MODEL_POLICIES[JUDGE_MODEL].max_output_tokens
        or payload["stream"] is not False
        or payload["service_tier"] != "standard_only"
        or payload["thinking"] != settings["thinking"]
        or payload["output_config"]
        != {
            **settings["output_config"],
            "format": {"type": "json_schema", "schema": judge_schema(build_judge_packet(value))},
        }
        or type(messages) is not list
        or len(messages) != 1
        or type(messages[0]) is not dict
        or set(messages[0]) != {"role", "content"}
        or messages[0]["role"] != "user"
        or type(messages[0]["content"]) is not str
        or json.loads(messages[0]["content"]) != build_judge_packet(value)
    ):
        raise ValueError("Replay packet or settings differ")
    return RetainedReplay(deepcopy(payload), value)


def diagnostic_payload(replay: RetainedReplay, profile: str) -> dict[str, Any]:
    if profile not in DIAGNOSTIC_PROFILES:
        raise ValueError("Invalid diagnostic profile")
    payload = deepcopy(replay.payload)
    if profile == "medium":
        payload["output_config"]["effort"] = "medium"
    elif profile == "high-output":
        payload["max_tokens"] = 16384
        payload["timeout"] = 300.0
    return payload


@dataclass(frozen=True)
class DiagnosticResult:
    decode_result: str
    stop_reason: str | None
    output_tokens: int | None
    thinking_tokens: int | None
    elapsed_seconds: float
    cost: CostSummary

    def terminal_fields(self) -> dict[str, str | int | float | None]:
        return {
            "decode_result": self.decode_result,
            "stop_reason": self.stop_reason,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "elapsed_seconds": self.elapsed_seconds,
        }


async def diagnose_replay(
    replay: RetainedReplay,
    profile: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> DiagnosticResult:
    """One bounded generation, full decode, content-free observation and inclusive cost."""
    from anthropic import APITimeoutError, AsyncAnthropic
    from anthropic.types import Message

    from rent_navigator.eval.judge import parse_judgment
    from rent_navigator.eval.judge_decode import JudgeDecodeFailure
    from rent_navigator.eval.judge_projection import project_judge_response
    from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
    from rent_navigator.provider import _usage
    from rent_navigator.trace import CostSummary, cost_for_usage

    payload = diagnostic_payload(replay, profile)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or "ANTHROPIC_CUSTOM_HEADERS" in os.environ:
        raise ValueError("Invalid probe environment")
    policy = MODEL_POLICIES[JUDGE_MODEL]
    reservation = (
        policy.reserved_input_tokens * policy.input_per_million
        + payload["max_tokens"] * policy.output_per_million
    ) / Decimal(1_000_000)
    logging.disable(logging.CRITICAL)
    started = clock()
    response: Message | None = None
    decoded: str | None = None
    try:
        async with asyncio.timeout(payload["timeout"]):
            async with AsyncAnthropic(
                api_key=key,
                base_url="https://api.anthropic.com",
                max_retries=0,
                timeout=payload["timeout"],
            ) as client:
                response = await client.messages.create(**payload)
    except (TimeoutError, APITimeoutError):
        decoded = "deadline_exceeded"
    except Exception:
        decoded = "provider_error"
    try:
        observed_usage = _usage(response) if isinstance(response, Message) else None
    except Exception:
        observed_usage = None
    cost = cost_for_usage(JUDGE_MODEL, observed_usage)
    cost = CostSummary(
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        usage_complete=cost.usage_complete,
        actual_cost_usd=cost.actual_cost_usd,
        reserved_cost_usd=reservation,
    )
    stop: str | None = None
    thinking: int | None = None
    output: int | None = None
    if isinstance(response, Message):
        raw_stop = getattr(response, "stop_reason", None)
        stop = (
            raw_stop
            if raw_stop
            in (
                "end_turn",
                "max_tokens",
                "stop_sequence",
                "tool_use",
                "pause_turn",
                "refusal",
                "context_window_exceeded",
            )
            else None
        )
        usage = getattr(response, "usage", None)
        raw_output = (
            usage.get("output_tokens")
            if type(usage) is dict
            else getattr(usage, "output_tokens", None)
        )
        output = raw_output if type(raw_output) is int and raw_output >= 0 else None
        try:
            response, projection = project_judge_response(response)
            thinking = projection.thinking_tokens
            parse_judgment(response, replay.value)
            local_result = "decoded"
        except JudgeDecodeFailure as error:
            local_result = error.decode_rejection_reason
        except Exception:
            local_result = "model_invalid"
        if decoded is None:
            decoded = local_result if cost.usage_complete else "provider_error"
    elif decoded is None:
        decoded = "model_invalid"
    elapsed = clock() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Invalid diagnostic clock")
    if elapsed > payload["timeout"]:
        decoded = "deadline_exceeded"
    return DiagnosticResult(decoded, stop, output, thinking, elapsed, cost)


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
    from rent_navigator.judge_profile import judge_request_settings
    from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES

    packet = fixed_packet(packet_id)
    payload: dict[str, Any] = {
        "model": JUDGE_MODEL,
        "system": JUDGE_SYSTEM,
        "messages": [
            {"role": "user", "content": json.dumps(packet, sort_keys=True, separators=(",", ":"))}
        ],
        "thinking": judge_request_settings()["thinking"],
        "output_config": {
            **judge_request_settings()["output_config"],
            "format": {"type": "json_schema", "schema": judge_schema(packet)},
        },
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
    from rent_navigator.eval.judge import parse_packet_judgment
    from rent_navigator.eval.judge_decode import JudgeDecodeFailure
    from rent_navigator.eval.judge_projection import project_judge_response

    response, _ = project_judge_response(response)
    decoded = "decoded"
    try:
        parse_packet_judgment(response, fixed_packet(packet_id))
    except JudgeDecodeFailure as error:
        decoded = error.decode_rejection_reason
    print(
        json.dumps(
            {
                "http_status": 200,
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump(mode="json"),
                "decode_result": decoded,
                "request_id": getattr(response, "_request_id", None),
            },
            ensure_ascii=False,
        )
    )
    return 0 if decoded == "decoded" else 1


def main(argv: list[str] | None = None) -> int:
    # Prevent imported project/SDK modules from creating bytecode during the probe.
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dump", action="store_true")
    modes.add_argument("--send", action="store_true")
    modes.add_argument("--diagnose", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--in", dest="input_path", type=Path)
    parser.add_argument("--packet-id", choices=PACKET_IDS)
    parser.add_argument("--replay-dir", type=Path)
    parser.add_argument("--raw-sha256")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--attempt-id")
    parser.add_argument("--trace-id")
    parser.add_argument("--operation-index", type=int)
    parser.add_argument("--diagnostic-profile", choices=DIAGNOSTIC_PROFILES)
    args = parser.parse_args(argv)
    selectors = (
        args.replay_dir,
        args.raw_sha256,
        args.manifest_sha256,
        args.attempt_id,
        args.trace_id,
        args.operation_index,
        args.diagnostic_profile,
    )
    if args.diagnose:
        if any(value is None for value in selectors) or any(
            value is not None for value in (args.out, args.input_path, args.packet_id)
        ):
            parser.error("--diagnose requires all replay selectors and profile only")
        try:
            replay = load_replay(
                args.replay_dir,
                raw_sha256=args.raw_sha256,
                manifest_sha256=args.manifest_sha256,
                attempt_id=args.attempt_id,
                trace_id=args.trace_id,
                operation_index=args.operation_index,
            )
            result = asyncio.run(diagnose_replay(replay, args.diagnostic_profile))
        except ValueError:
            parser.error("Invalid retained judge diagnostic")
        print(json.dumps(result.terminal_fields(), allow_nan=False))
        return 0 if result.decode_result == "decoded" else 1
    if any(value is not None for value in selectors):
        parser.error("Replay selectors require --diagnose")
    if args.dump:
        if args.out is None or args.input_path is not None:
            parser.error("--dump requires --out and does not accept --in")
        dump_payload(args.out, args.packet_id or "J01")
        return 0
    if args.input_path is None or args.out is not None:
        parser.error("--send requires --in and does not accept --out")
    return send_payload(args.input_path, args.packet_id or "J01")


if __name__ == "__main__":
    raise SystemExit(main())
