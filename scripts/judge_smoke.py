"""Restricted fourteen-vote judge smoke with content-free per-vote evidence."""

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, cast, get_args
from uuid import uuid4

from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types import Message, MessageTokensCount, TextBlock
from anthropic.types import Usage as SDKUsage
from pydantic import TypeAdapter

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RealJudge,
    build_smoke_input,
    judge_config_hash,
    validate_smoke_result,
)
from rent_navigator.eval.models import JudgeResult
from rent_navigator.eval.runner import JudgeAccounting, JudgeFailure, JudgeInput
from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.models import SourceCommit
from rent_navigator.provider import (
    MessagesPort,
    ProviderFailure,
    SpendLedger,
    _raise_failure,
    create_client,
)
from rent_navigator.trace import PRICING_HASH, TraceContext, provider_cost_totals

ROOT = Path(__file__).resolve().parents[1]
CAP = Decimal("2.00")


def fixed_votes() -> list[tuple[str, JudgeInput, dict[str, Any] | None]]:
    sys.path.insert(0, str(ROOT / "tests"))
    from judge_calibration import (
        CALIBRATION_IDS,
        calibration_input,
        grade_only_output,
        load_calibration,
    )

    corpus = load_corpus()
    votes: list[tuple[str, JudgeInput, dict[str, Any] | None]] = []
    for identifier in CALIBRATION_IDS:
        fixture = load_calibration(identifier, corpus=corpus)
        votes.append(
            (
                identifier,
                calibration_input(fixture["provider_packet"], corpus),
                grade_only_output(fixture),
            )
        )
    case = next(case for case in load_gold(ROOT / "eval", corpus).cases if case.id == "R04")
    votes.append(("R04", build_smoke_input(case, corpus), None))
    return votes


def head_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def agreement(
    actual: dict[str, Any] | None, expected: dict[str, Any] | None
) -> dict[str, Any] | None:
    if expected is None or actual is None:
        return None
    claims = {row["id"]: row["result"] for row in actual["required_claims"]}
    statements = {row["id"]: row for row in actual["statements"]}
    result = {
        "claims": {
            row["id"]: claims.get(row["id"]) == row["result"] for row in expected["required_claims"]
        },
        "statements": {
            row["id"]: {
                field: statements.get(row["id"], {}).get(field) == row[field]
                for field in ("factual", "citation_support")
            }
            for row in expected["statements"]
        },
        "false_pass": actual["false_pass"] == expected["false_pass"],
        "policy_violations": actual["policy_violations"] == expected["policy_violations"],
    }
    result["exact"] = (
        all(result["claims"].values())
        and all(all(row.values()) for row in result["statements"].values())
        and result["false_pass"]
        and result["policy_violations"]
    )
    return result


class VotePort:
    """Capture only the R14 identifiers and validated diagnostic before normalization."""

    def __init__(self, messages: MessagesPort) -> None:
        self.messages = messages
        self.request_id: str | None = None
        self.error_type: str | None = None
        self.stop_reason: str | None = None
        self.diagnostic: dict[str, Any] | None = None

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        failure = None
        try:
            return await self.messages.count_tokens(**kwargs)
        except BaseException as error:
            failure = self.capture_failure(error)
        assert failure is not None
        _raise_failure(failure)

    def capture_failure(self, error: BaseException) -> ProviderFailure:
        failure = ProviderFailure.from_exception(error)
        self.diagnostic = failure.diagnostic.model_dump(mode="json")
        if isinstance(error, APIStatusError):
            try:
                # Read only the SDK's already-stored type field, never its message.
                # This avoids a second invocation of a dynamic body getter.
                body = vars(error).get("body")
                detail = body.get("error", body) if type(body) is dict else None
                value = detail.get("type") if type(detail) is dict else None
                if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value):
                    self.error_type = value
                value = error.response.headers.get("request-id")
                if type(value) is str and re.fullmatch(r"req_[A-Za-z0-9_-]{1,120}", value):
                    self.request_id = value
            except BaseException:
                self.error_type = self.request_id = None
        return failure

    async def create(self, **kwargs: Any) -> Message:
        failure = None
        response = None
        try:
            response = await self.messages.create(**kwargs)
        except BaseException as error:
            failure = self.capture_failure(error)
        if failure is not None:
            _raise_failure(failure)
        assert response is not None
        try:
            self.stop_reason = TypeAdapter(
                Message.model_fields["stop_reason"].rebuild_annotation()
            ).validate_python(response.stop_reason, strict=True)
        except Exception:
            self.stop_reason = None
        value = getattr(response, "_request_id", None)
        if type(value) is str and re.fullmatch(r"req_[A-Za-z0-9_-]{1,120}", value):
            self.request_id = value
        return response


class OfflineMessages:
    """One deterministic fake vote per fixed input; never a quality measurement."""

    def __init__(self, value: JudgeInput, expected: dict[str, Any] | None) -> None:
        self.value, self.expected = value, expected

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=1000)

    async def create(self, **kwargs: Any) -> Message:
        output = self.expected or {
            "required_claims": [
                {"id": claim.id, "result": "met"} for claim in self.value.required_claims
            ],
            "statements": [
                {
                    "id": row.id,
                    "factual": "contradicted" if row.id == "s3" else "supported",
                    "citation_support": "unsupported" if row.id == "s3" else "supported",
                }
                for row in self.value.response.statements
            ],
            "false_pass": True,
            "policy_violations": [],
        }
        return Message.model_construct(
            id="msg_synthetic_judge_smoke",
            type="message",
            role=get_args(Message.model_fields["role"].annotation)[0],
            model=JUDGE_MODEL,
            content=[TextBlock(type="text", text=json.dumps(output))],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=SDKUsage(input_tokens=1000, output_tokens=100),
        )


async def run_smoke(
    *,
    source_commit: str,
    evidence_dir: Path,
    live: bool = False,
    billing_ready: bool = False,
    messages_factory: Callable[[JudgeInput, dict[str, Any] | None], MessagesPort] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    source_commit = TypeAdapter(SourceCommit).validate_python(source_commit, strict=True)
    if source_commit != head_sha():
        raise ValueError("Source commit must equal HEAD")
    if live and not billing_ready:
        raise ProviderFailure("budget_exhausted")
    if live and subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True):
        raise ValueError("Live smoke requires a clean checkout")
    votes = fixed_votes()
    corpus = load_corpus()
    config = judge_config_hash()
    evidence_dir.mkdir(parents=True, exist_ok=False)
    ledger = SpendLedger(CAP)
    client: AsyncAnthropic | None = None
    records = []
    rows: list[dict[str, Any]] = []
    run_id = uuid4()
    stopped: str | None = None
    genuine_live = live and messages_factory is None
    try:
        if live and messages_factory is None:
            client = create_client(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        for identifier, value, expected in votes:
            port = VotePort(
                messages_factory(value, expected)
                if messages_factory is not None
                else cast(MessagesPort, client.messages)
                if client is not None
                else OfflineMessages(value, expected)
            )
            metadata, raw = StringIO(), StringIO()
            context = TraceContext(
                attempt_id=uuid4(),
                trace_id=uuid4(),
                phase="judge",
                source_commit=source_commit,
                config_hash=config,
                corpus_hash=corpus.corpus_hash,
                pricing_hash=PRICING_HASH,
            )
            start = clock()
            accounting: JudgeAccounting
            decoded = None
            code = None
            smoke_passed = None
            try:
                evaluated = await RealJudge(
                    port,
                    budget=ledger,
                    metadata=metadata,
                    raw_provider=raw,
                    run_id=run_id,
                    clock=clock,
                ).evaluate(value, context=context)
                accounting = evaluated
                decoded = evaluated.judgment.model_dump(mode="json")
                if identifier == "R04":
                    try:
                        validate_smoke_result(value, evaluated.judgment)
                        smoke_passed = True
                    except Exception:
                        smoke_passed = False
            except JudgeFailure as error:
                accounting, code = error.accounting, error.code
                if port.diagnostic is None:
                    port.diagnostic = ProviderFailure(code).diagnostic.model_dump(mode="json")
            finally:
                elapsed = clock() - start
                metadata.close()
                raw.close()
            records.extend(accounting.records)
            row = {
                "packet_id": identifier,
                "source_commit": source_commit,
                "judge_config_hash": config,
                "execution_mode": "live" if genuine_live else "synthetic",
                "decoded_judgment": decoded,
                "decode_error": code if code == "invalid_generated_output" else None,
                "failure_code": code,
                "agreement": agreement(decoded, expected),
                "r04_smoke_passed": smoke_passed,
                "stop_reason": port.stop_reason,
                "usage": {
                    "input_tokens": accounting.cost.input_tokens,
                    "output_tokens": accounting.cost.output_tokens,
                },
                "usage_complete": accounting.cost.usage_complete,
                "elapsed_seconds": elapsed,
                "requested_model_id": accounting.requested_model_id,
                "returned_model_id": accounting.returned_model_id,
                "actual_cost_usd": accounting.cost.model_dump(mode="json")["actual_cost_usd"],
                "reserved_cost_usd": accounting.cost.model_dump(mode="json")["reserved_cost_usd"],
                "failure_diagnostic": port.diagnostic,
                "provider_request_id": port.request_id,
                "provider_error_type": port.error_type,
            }
            (evidence_dir / (identifier + ".json")).write_text(json.dumps(row, indent=2) + "\n")
            rows.append(row)
            if code == "budget_exhausted" or ledger.stopped:
                stopped = code or "provider_error"
                break
    except ProviderFailure as error:
        stopped = error.code
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                stopped = "provider_error"
        cost = provider_cost_totals(records)
        complete = len(rows) == 14 and all(row["decoded_judgment"] is not None for row in rows)
        matched = sum(row["agreement"] is not None and row["agreement"]["exact"] for row in rows)
        generations = sum(record.provider_operation == "generation" for record in records)
        summary = {
            "source_commit": source_commit,
            "judge_config_hash": config,
            "pricing_hash": PRICING_HASH,
            "corpus_hash": corpus.corpus_hash,
            "execution_mode": "live" if genuine_live else "synthetic",
            "planned_votes": 14,
            "recorded_votes": len(rows),
            "complete": complete and stopped is None,
            "expectations": "proposed_not_independently_reviewed",
            "calibration_matched": matched,
            "calibration_denominator": 13,
            "r04_smoke_passed": rows[-1]["r04_smoke_passed"]
            if rows and rows[-1]["packet_id"] == "R04"
            else None,
            "reportable": False,
            "official_n16_evaluation": False,
            "generation_attempts": generations,
            "real_generation_attempts": generations if genuine_live else 0,
            "budget_limit_usd": "2.00",
            "reservation_forecast_usd": format(
                14 * MODEL_POLICIES[JUDGE_MODEL].reservation_usd, "f"
            ),
            "batch_committed_usd": format(ledger.committed_usd, "f"),
            "cost": cost.model_dump(mode="json"),
            "stop_reason": stopped,
        }
        (evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _passes_gate(summary: dict[str, Any], evidence_dir: Path, source_commit: str) -> bool:
    """Check recorded vote criteria; proposed-label agreement remains a measurement."""
    config = judge_config_hash()
    if (
        source_commit != head_sha()
        or summary.get("source_commit") != source_commit
        or summary.get("judge_config_hash") != config
        or summary.get("complete") is not True
        or summary.get("planned_votes") != 14
        or summary.get("recorded_votes") != 14
        or summary.get("generation_attempts") != 14
        or summary.get("stop_reason") is not None
        or summary.get("r04_smoke_passed") is not True
        or summary.get("calibration_denominator") != 13
    ):
        return False
    matched = 0
    for identifier, value, _ in fixed_votes():
        try:
            row = json.loads((evidence_dir / (identifier + ".json")).read_text())
        except (OSError, ValueError):
            return False
        if type(row) is not dict:
            return False
        if (
            row.get("packet_id") != identifier
            or row.get("source_commit") != source_commit
            or row.get("judge_config_hash") != config
            or type(row.get("decoded_judgment")) is not dict
            or row.get("decode_error") is not None
            or row.get("failure_code") is not None
            or row.get("failure_diagnostic") is not None
            or row.get("stop_reason") != "end_turn"
            or row.get("requested_model_id") != JUDGE_MODEL
            or row.get("returned_model_id") != JUDGE_MODEL
            or row.get("usage_complete") is not True
        ):
            return False
        try:
            judgment = JudgeResult.model_validate(row["decoded_judgment"], strict=True)
            judgment.validate_coverage(
                (claim.id for claim in value.required_claims),
                (statement.id for statement in value.response.statements),
            )
        except ValueError:
            return False
        usage = row.get("usage")
        if (
            type(usage) is not dict
            or type(usage.get("input_tokens")) is not int
            or usage["input_tokens"] < 0
            or type(usage.get("output_tokens")) is not int
            or not 0 <= usage["output_tokens"] <= 6000
        ):
            return False
        elapsed = row.get("elapsed_seconds")
        if type(elapsed) not in (int, float):
            return False
        elapsed = cast(int | float, elapsed)
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 90:
            return False
        if identifier == "R04":
            if row.get("r04_smoke_passed") is not True:
                return False
            try:
                validate_smoke_result(value, judgment)
            except ValueError:
                return False
        else:
            comparison = row.get("agreement")
            if type(comparison) is not dict or type(comparison.get("exact")) is not bool:
                return False
            matched += comparison["exact"]
    return (
        type(summary.get("calibration_matched")) is int
        and summary["calibration_matched"] == matched
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    parser.add_argument("--billing-ready", action="store_true")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(
            run_smoke(
                source_commit=args.source_commit,
                evidence_dir=args.evidence_dir,
                live=args.live,
                billing_ready=args.billing_ready,
            )
        )
    except (ValueError, OSError, ProviderFailure):
        print("Judge smoke admission failed.")
        return 2
    print(json.dumps(summary))
    return 0 if _passes_gate(summary, args.evidence_dir, args.source_commit) else 1


if __name__ == "__main__":
    raise SystemExit(main())
