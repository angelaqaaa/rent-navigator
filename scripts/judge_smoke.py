"""Restricted fourteen-vote judge smoke with content-free per-vote evidence."""

import argparse
import ast
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
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
from rent_navigator.eval.judge_projection import project_judge_response, thinking_metrics
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
from rent_navigator.security_cases import SecurityCaseId
from rent_navigator.trace import PRICING_HASH, TraceContext, provider_cost_totals

ROOT = Path(__file__).resolve().parents[1]
CAP = Decimal("3.00")
PACKET_IDS = (*tuple(f"J{number:02}" for number in range(1, 14)), "R04")
MEASUREMENT_JUDGE_HASH = "23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8"


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
        value = calibration_input(fixture["provider_packet"], corpus)
        wire = grade_only_output(fixture)
        expected = {
            "required_claims": [
                {"id": key, **leaf} for key, leaf in wire["required_claims"].items()
            ],
            "statements": [{"id": key, **leaf} for key, leaf in wire["statements"].items()],
            "false_pass": wire["false_pass"],
            "policy_violations": [key for key, flag in wire["policy_violations"].items() if flag],
        }
        votes.append((identifier, value, expected))
    case = next(case for case in load_gold(ROOT / "eval", corpus).cases if case.id == "R04")
    votes.append(("R04", build_smoke_input(case, corpus), None))
    return votes


def head_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _measurement_provenance(source_commit: str, runner_commit: str | None) -> str:
    """Bind the current complete judging configuration and runner to the same HEAD."""
    if runner_commit is None:
        raise ValueError("Measurement requires its runner commit")
    runner = TypeAdapter(SourceCommit).validate_python(runner_commit, strict=True)
    if (
        runner != head_sha()
        or source_commit != runner
        or judge_config_hash() != MEASUREMENT_JUDGE_HASH
    ):
        raise ValueError("Measurement runner or judge identity differs")
    try:
        resolved = subprocess.check_output(
            ["git", "rev-parse", "--verify", source_commit + "^{commit}"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if resolved != source_commit:
            raise ValueError("Measurement judging source differs")
        subprocess.check_output(
            ["git", "merge-base", "--is-ancestor", source_commit, runner],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
        )
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", "-z", source_commit, runner],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        helper_sources = [
            subprocess.check_output(
                ["git", "show", revision + ":scripts/judge_smoke.py"],
                cwd=ROOT,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for revision in (source_commit, runner)
        ]
    except subprocess.CalledProcessError:
        raise ValueError("Measurement requires a verified ancestor") from None
    if set(changed.rstrip("\0").split("\0")) - {
        "",
        "scripts/judge_smoke.py",
        "tests/test_judge_smoke.py",
    }:
        raise ValueError("Measurement source delta contains unrelated changes")
    helpers = [
        {
            node.name: ast.get_source_segment(source, node)
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef | ast.ClassDef)
            and node.name in {"fixed_votes", "VotePort", "OfflineMessages"}
        }
        for source in helper_sources
    ]
    if (
        set(helpers[0]) != {"fixed_votes", "VotePort", "OfflineMessages"}
        or helpers[0] != helpers[1]
    ):
        raise ValueError("Measurement judging helpers differ from the original source")
    return runner


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
        self.thinking_metrics: dict[str, Any] | None = None

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
        response, _ = project_judge_response(response)
        self.thinking_metrics = thinking_metrics(response)
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
        output = {
            **output,
            "required_claims": {
                row["id"]: {"result": row["result"]} for row in output["required_claims"]
            },
            "statements": {
                row["id"]: {"factual": row["factual"], "citation_support": row["citation_support"]}
                for row in output["statements"]
            },
            "policy_violations": {
                identifier: identifier in output["policy_violations"]
                for identifier in get_args(SecurityCaseId)
            },
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
    measurement: bool = False,
    packets: list[str] | None = None,
    runner_commit: str | None = None,
    cap_usd: Decimal = CAP,
    forecast_usd: Decimal | None = None,
) -> dict[str, Any]:
    source_commit = TypeAdapter(SourceCommit).validate_python(source_commit, strict=True)
    if measurement:
        if (
            not packets
            or len(set(packets)) != len(packets)
            or any(identifier not in PACKET_IDS for identifier in packets)
            or packets != [identifier for identifier in PACKET_IDS if identifier in packets]
        ):
            raise ValueError("Measurement requires a unique nonempty registered-order subset")
        runner_commit = _measurement_provenance(source_commit, runner_commit)
    else:
        if packets is not None or runner_commit is not None:
            raise ValueError("Named packets and runner commit require measurement mode")
        if source_commit != head_sha():
            raise ValueError("Source commit must equal HEAD")
    expected_forecast = (len(packets) if measurement and packets else 14) * MODEL_POLICIES[
        JUDGE_MODEL
    ].reservation_usd
    forecast = expected_forecast if forecast_usd is None else forecast_usd
    if (
        not cap_usd.is_finite()
        or not 0 <= cap_usd <= CAP
        or not forecast.is_finite()
        or forecast != expected_forecast
        or forecast > cap_usd
    ):
        raise ValueError("Judge forecast and cap violate the registered bounds")
    if live and not billing_ready:
        raise ProviderFailure("budget_exhausted")
    if live and subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True):
        raise ValueError("Live smoke requires a clean checkout")
    votes = fixed_votes()
    if measurement:
        votes = [vote for vote in votes if vote[0] in cast(list[str], packets)]
    corpus = load_corpus()
    config = judge_config_hash()
    evidence_dir.mkdir(parents=True, exist_ok=False)
    ledger = SpendLedger(cap_usd)
    client: AsyncAnthropic | None = None
    records = []
    rows: list[dict[str, Any]] = []
    attempted: list[str] = []
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
            decode_reason = None
            attempted.append(identifier)
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
                decode_reason = error.decode_rejection_reason
                if port.diagnostic is None:
                    port.diagnostic = error.diagnostic.model_dump(mode="json")
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
                "decode_rejection_reason": decode_reason,
                "provider_request_id": port.request_id,
                "provider_error_type": port.error_type,
                "thinking_metrics": port.thinking_metrics,
                "expected_task_success": task_success(expected) if expected is not None else None,
                "observed_task_success": task_success(decoded) if decoded is not None else None,
                "fail_to_pass": (
                    not task_success(expected) and task_success(decoded)
                    if expected is not None and decoded is not None
                    else None
                ),
            }
            if measurement:
                row.update(judging_source_commit=source_commit, runner_commit=runner_commit)
            criterion = failed_criterion(row, value, expected, source_commit, config)
            row["failed_criterion"] = criterion
            (evidence_dir / (identifier + ".json")).write_text(json.dumps(row, indent=2) + "\n")
            rows.append(row)
            hard_stop = (
                ledger.stopped
                or code is not None
                or criterion
                in {"identity", "model", "stop_reason", "decode", "task_success_evidence"}
                or (port.diagnostic is not None and port.diagnostic.get("category") == "cancelled")
            )
            if hard_stop or criterion is not None and not measurement:
                stopped = code or "registered_criterion_failed"
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
        planned_votes = len(votes) if measurement else 14
        complete = len(rows) == planned_votes and (
            measurement or all(row["decoded_judgment"] is not None for row in rows)
        )
        matched = sum(row["agreement"] is not None and row["agreement"]["exact"] for row in rows)
        generations = sum(record.provider_operation == "generation" for record in records)
        summary = {
            "source_commit": source_commit,
            "judge_config_hash": config,
            "pricing_hash": PRICING_HASH,
            "corpus_hash": corpus.corpus_hash,
            "execution_mode": "live" if genuine_live else "synthetic",
            "planned_votes": planned_votes,
            "recorded_votes": len(rows),
            "complete": complete and stopped is None,
            "expectations": "proposed_not_independently_reviewed",
            "calibration_matched": matched,
            "calibration_denominator": sum(expected is not None for _, _, expected in votes)
            if measurement
            else 13,
            "field_agreement": field_agreement(rows),
            "fail_to_pass_flips": sum(row["fail_to_pass"] is True for row in rows),
            "completed_votes": [row["packet_id"] for row in rows],
            "unstarted_votes": [identifier for identifier, _, _ in votes[len(rows) :]],
            "r04_smoke_passed": rows[-1]["r04_smoke_passed"]
            if rows and rows[-1]["packet_id"] == "R04"
            else None,
            "reportable": False,
            "official_n16_evaluation": False,
            "generation_attempts": generations,
            "real_generation_attempts": generations if genuine_live else 0,
            "budget_limit_usd": format(cap_usd, "f"),
            "reservation_forecast_usd": format(forecast, "f"),
            "batch_committed_usd": format(ledger.committed_usd, "f"),
            "cost": cost.model_dump(mode="json"),
            "stop_reason": stopped,
        }
        if measurement:
            summary.update(
                measurement=True,
                purpose="measurement after a failed gate",
                gate_passed=False,
                judging_source_commit=source_commit,
                runner_commit=runner_commit,
                requested_votes=[identifier for identifier, _, _ in votes],
                attempted_votes=attempted,
                unstarted_votes=[identifier for identifier, _, _ in votes[len(attempted) :]],
                measurement_complete=complete and stopped is None,
                decoded_votes=sum(row["decoded_judgment"] is not None for row in rows),
            )
        (evidence_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def task_success(grades: dict[str, Any]) -> bool:
    """Use the factual task predicate; citation support is a separate metric."""
    judgment = JudgeResult.model_validate(grades, strict=True)
    return (
        all(row.result == "met" for row in judgment.required_claims)
        and all(row.factual == "supported" for row in judgment.statements)
        and not judgment.false_pass
        and not judgment.policy_violations
    )


def field_agreement(rows: list[dict[str, Any]]) -> dict[str, int]:
    fields = [
        flag
        for row in rows
        if row["agreement"] is not None
        for comparison in (row["agreement"],)
        for flag in (
            *comparison["claims"].values(),
            *(flag for fields in comparison["statements"].values() for flag in fields.values()),
            comparison["false_pass"],
            comparison["policy_violations"],
        )
    ]
    return {"matched": sum(fields), "denominator": len(fields)}


def failed_criterion(
    row: dict[str, Any],
    value: JudgeInput,
    expected: dict[str, Any] | None,
    source_commit: str,
    config: str,
) -> str | None:
    if row.get("source_commit") != source_commit or row.get("judge_config_hash") != config:
        return "identity"
    if row.get("failure_code") is not None or row.get("failure_diagnostic") is not None:
        return "call_failure"
    if (
        row.get("decode_error") is not None
        or row.get("decode_rejection_reason") is not None
        or type(row.get("decoded_judgment")) is not dict
    ):
        return "decode"
    if row.get("requested_model_id") != JUDGE_MODEL or row.get("returned_model_id") != JUDGE_MODEL:
        return "model"
    if row.get("stop_reason") != "end_turn":
        return "stop_reason"
    usage = row.get("usage")
    if (
        row.get("usage_complete") is not True
        or type(usage) is not dict
        or type(usage.get("input_tokens")) is not int
        or usage["input_tokens"] < 0
        or type(usage.get("output_tokens")) is not int
        or not 0 <= usage["output_tokens"] <= 6000
    ):
        return "usage_bound"
    elapsed = row.get("elapsed_seconds")
    if type(elapsed) not in (int, float):
        return "elapsed_bound"
    elapsed = cast(int | float, elapsed)
    if not math.isfinite(elapsed) or not 0 <= elapsed <= 90:
        return "elapsed_bound"
    try:
        judgment = JudgeResult.model_validate(row["decoded_judgment"], strict=True)
        judgment.validate_coverage(
            (claim.id for claim in value.required_claims),
            (statement.id for statement in value.response.statements),
        )
        citations: dict[str, bool] = {
            statement.id: bool(statement.citation_ids) for statement in value.response.statements
        }
        if any(
            citations[statement.id] == (statement.citation_support == "not_applicable")
            for statement in judgment.statements
        ):
            raise ValueError("citation_applicability")
        if expected is None:
            if row.get("r04_smoke_passed") is not True:
                return "r04_smoke"
            try:
                validate_smoke_result(value, judgment)
            except ValueError:
                return "r04_smoke"
        else:
            expected_success = task_success(expected)
            observed_success = task_success(row["decoded_judgment"])
            flip = not expected_success and observed_success
            if (
                type(row.get("expected_task_success")) is not bool
                or type(row.get("observed_task_success")) is not bool
                or type(row.get("fail_to_pass")) is not bool
                or row.get("expected_task_success") != expected_success
                or row.get("observed_task_success") != observed_success
                or row.get("fail_to_pass") != flip
            ):
                return "task_success_evidence"
            if flip:
                return "fail_to_pass"
    except ValueError:
        return "decode"
    return None


def _passes_gate(summary: dict[str, Any], evidence_dir: Path, source_commit: str) -> bool:
    """Check recorded vote criteria; proposed-label agreement remains a measurement."""
    config = judge_config_hash()
    if (
        summary.get("measurement") is not None
        or summary.get("purpose") == "measurement after a failed gate"
        or source_commit != head_sha()
        or summary.get("source_commit") != source_commit
        or summary.get("judge_config_hash") != config
        or summary.get("complete") is not True
        or summary.get("planned_votes") != 14
        or summary.get("recorded_votes") != 14
        or summary.get("generation_attempts") != 14
        or summary.get("stop_reason") is not None
        or summary.get("r04_smoke_passed") is not True
        or summary.get("calibration_denominator") != 13
        or type(summary.get("fail_to_pass_flips")) is not int
        or summary["fail_to_pass_flips"] != 0
        or summary.get("unstarted_votes") != []
    ):
        return False
    matched = 0
    for identifier, value, expected in fixed_votes():
        try:
            row = json.loads((evidence_dir / (identifier + ".json")).read_text())
        except (OSError, ValueError):
            return False
        if type(row) is not dict:
            return False
        if failed_criterion(row, value, expected, source_commit, config) is not None:
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
            if comparison != agreement(row["decoded_judgment"], expected):
                return False
            matched += comparison["exact"]
    return (
        type(summary.get("calibration_matched")) is int
        and summary["calibration_matched"] == matched
    )


def _amount(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Amount must be a decimal") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    parser.add_argument("--billing-ready", action="store_true")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--measurement",
        action="store_true",
        help=(
            "Record current-config registered votes, including criterion misses; "
            "never qualify a gate"
        ),
    )
    parser.add_argument("--packets", nargs="+", help="Measurement packet IDs in registered order")
    parser.add_argument(
        "--runner-commit", help="Measurement runner HEAD, equal to the judging source commit"
    )
    parser.add_argument("--cap-usd", type=_amount, default=CAP, help="Flat cap, at most 3.00 USD")
    parser.add_argument(
        "--forecast-usd", type=_amount, help="Exact selected-vote reservation, no greater than cap"
    )
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(
            run_smoke(
                source_commit=args.source_commit,
                evidence_dir=args.evidence_dir,
                live=args.live,
                billing_ready=args.billing_ready,
                measurement=args.measurement,
                packets=args.packets,
                runner_commit=args.runner_commit,
                cap_usd=args.cap_usd,
                forecast_usd=args.forecast_usd,
            )
        )
    except (ValueError, OSError, ProviderFailure):
        print("Judge smoke admission failed.")
        return 2
    print(json.dumps(summary))
    if args.measurement:
        return 0 if summary.get("measurement_complete") is True else 1
    return 0 if _passes_gate(summary, args.evidence_dir, args.source_commit) else 1


if __name__ == "__main__":
    raise SystemExit(main())
