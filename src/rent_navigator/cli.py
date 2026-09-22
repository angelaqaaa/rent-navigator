"""Restricted development entry for one fixed anonymous rent scenario."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from anthropic import AsyncAnthropic
from anthropic.types import Message, MessageTokensCount, TextBlock, ToolUseBlock, Usage
from pydantic import BaseModel, TypeAdapter

from rent_navigator.agent import TOOL_STATUS_TEXT, agent_config_hash, answer
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.index import build_index, search
from rent_navigator.model_policy import MODEL_POLICIES
from rent_navigator.models import AskResponse, RentRequest, SourceCommit
from rent_navigator.provider import (
    Deadline,
    MessagesPort,
    ProviderAdapter,
    ProviderFailure,
    SpendLedger,
    create_client,
)
from rent_navigator.trace import (
    ACTOR_MODEL,
    PRICING_HASH,
    MetadataSink,
    TraceContext,
    TraceRecord,
    provider_cost_totals,
)


def synthetic_request() -> RentRequest:
    """Create the sole permitted scenario with a fresh correlation identifier."""
    return RentRequest.model_validate_json(
        json.dumps(
            {
                "attempt_id": str(uuid4()),
                "mode": "rent",
                "confirmed": True,
                "facts": {
                    "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
                    "effective_on": "2026-09-01",
                    "served_on": "2026-07-03",
                    "service_method": "hand",
                    "current_cents": 200000,
                    "proposed_cents": 204800,
                    "tenancy_start": "2024-09-01",
                    "last_increase": {"state": "known", "date": "2025-09-01"},
                    "guideline_status": "controlled",
                    "form": "N1",
                },
            }
        )
    )


def synthetic_redactor(value: str) -> str:
    """Permit only the exact preset anonymous fact context, never arbitrary text."""
    expected = json.dumps(
        {
            "mode": "rent",
            "confirmed": True,
            "facts": synthetic_request().facts.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if value != expected:
        raise ValueError("Only the preset synthetic context is permitted")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", warnings=False)
    raise TypeError("Unsupported synthetic evidence value")


def _append(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, default=_json_value, sort_keys=True) + "\n")


def _request_fields(kwargs: dict[str, Any]) -> dict[str, Any]:
    # Never retain transport options, headers, or credentials.
    fields = (
        "model",
        "system",
        "messages",
        "tools",
        "tool_choice",
        "output_config",
        "thinking",
        "max_tokens",
        "stream",
        "service_tier",
        "extra_body",
    )
    return {key: kwargs[key] for key in fields if key in kwargs}


class _RecordingMessages:
    """Audit only this entry's preset synthetic exchange; never headers or errors."""

    def __init__(self, delegate: MessagesPort, directory: Path) -> None:
        self.delegate = delegate
        self.directory = directory
        self.counts: list[int] = []
        self.count_attempts = 0
        self.generation_attempts = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        if self.count_attempts >= 2:
            raise ProviderFailure("budget_exhausted")
        self.count_attempts += 1
        _append(
            self.directory / "synthetic_preflight_requests.jsonl",
            {
                "call": self.count_attempts,
                "request": _request_fields(kwargs),
            },
        )
        result = await self.delegate.count_tokens(**kwargs)
        self.counts.append(result.input_tokens)
        _append(self.directory / "preflight.jsonl", {"input_tokens": result.input_tokens})
        return result

    async def create(self, **kwargs: Any) -> Message:
        if self.generation_attempts >= 2:
            raise ProviderFailure("budget_exhausted")
        _append(
            self.directory / "synthetic_requests.jsonl",
            {
                "call": self.generation_attempts + 1,
                "request": _request_fields(kwargs),
            },
        )
        self.generation_attempts += 1
        result = await self.delegate.create(**kwargs)
        _append(
            self.directory / "synthetic_returns.jsonl",
            {
                "call": self.generation_attempts,
                "response": result.model_dump(mode="json", warnings=False),
            },
        )
        return result


class _OfflineMessages:
    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.calls = 0

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=100)

    async def create(self, **kwargs: Any) -> Message:
        self.calls += 1
        if self.calls == 1:
            content: list[TextBlock | ToolUseBlock] = [
                ToolUseBlock(
                    type="tool_use",
                    id="toolu_synthetic_rent",
                    name="rent_increase_check",
                    input=synthetic_request().facts.model_dump(mode="json"),
                )
            ]
            stop_reason = "tool_use"
        else:
            statements = [
                {
                    "id": "s1",
                    "text": "The 60-day notice interval fails the 90-day requirement.",
                    "citation_ids": [self.corpus.rule("notice.90_days").evidence_ids[0]],
                },
                {
                    "id": "s2",
                    "text": "The proposed $2,048 exceeds the 2026 guideline cap of $2,042.",
                    "citation_ids": [self.corpus.rule("guideline.2026").evidence_ids[0]],
                },
            ]
            content = [
                TextBlock(
                    type="text",
                    text=json.dumps(
                        {
                            "kind": "answer",
                            "refusal_reason": None,
                            "statements": statements,
                        }
                    ),
                )
            ]
            stop_reason = "end_turn"
        return Message.model_validate(
            {
                "id": f"msg_synthetic_{self.calls}",
                "type": "message",
                "role": "assistant",
                "model": ACTOR_MODEL,
                "content": content,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": Usage(input_tokens=100, output_tokens=20),
            }
        )


def _matches(response: AskResponse, corpus: Corpus) -> bool:
    result = response.tool_result
    return (
        response.status == "answered"
        and result is not None
        and result.tool == "rent_increase_check"
        and result.status == "fails_checked_rules"
        and result.notice_days == 60
        and result.cap_cents_exact == "204200"
        and {check.id: check.status for check in result.checks}
        == {
            "scope": "pass",
            "supported_year": "pass",
            "period_start": "pass",
            "notice": "fail",
            "spacing": "pass",
            "guideline": "fail",
            "form": "pass",
        }
        and bool(response.citations)
        and all(citation == corpus.citation(citation.id) for citation in response.citations)
    )


async def run_smoke(
    *, source_commit: str, evidence_dir: Path, live: bool = False, billing_ready: bool = False
) -> dict[str, Any]:
    """Run once; preserve both successful and incomplete synthetic attempts."""
    source_commit = TypeAdapter(SourceCommit).validate_python(source_commit, strict=True)
    if live and not billing_ready:
        raise ProviderFailure("budget_exhausted")
    evidence_dir.mkdir(parents=True, exist_ok=False)
    batch_reservation = 2 * MODEL_POLICIES[ACTOR_MODEL].reservation_usd
    report: dict[str, Any] = {
        "mode": "live_development" if live else "offline_synthetic",
        "purpose": "one synthetic development fixture; not evaluation or performance measurement",
        "source_commit": source_commit,
        "config_hash": agent_config_hash("rent", "production"),
        "pricing_hash": PRICING_HASH,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "budget_reservation_limit_usd": format(batch_reservation, "f"),
        "mechanical_acceptance": "INCOMPLETE",
        "explanation_review": "PENDING" if live else "NOT APPLICABLE: synthetic response",
        "response": None,
    }
    client: AsyncAnthropic | None = None
    recording: _RecordingMessages | None = None
    ledger = SpendLedger(batch_reservation)
    try:
        corpus = load_corpus()
        report["corpus_hash"] = corpus.corpus_hash
        index = evidence_dir / "index.sqlite3"
        build_index(index, corpus)
        request = synthetic_request()
        report["synthetic_request"] = request.model_dump(mode="json")
        messages: MessagesPort
        if live:
            client = create_client(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            messages = cast(MessagesPort, client.messages)
        else:
            messages = _OfflineMessages(corpus)
        recording = _RecordingMessages(messages, evidence_dir)
        with (evidence_dir / "metadata.jsonl").open("x", encoding="utf-8") as stream:
            response = await answer(
                request,
                provider=ProviderAdapter(recording, budget=ledger),
                corpus=corpus,
                retrieve=lambda query: search(
                    index, query, expected_corpus_hash=corpus.corpus_hash
                ),
                redact=synthetic_redactor,
                deadline=Deadline.start(),
                context=TraceContext(
                    attempt_id=request.attempt_id,
                    trace_id=uuid4(),
                    phase="analysis",
                    source_commit=source_commit,
                    config_hash=report["config_hash"],
                    corpus_hash=corpus.corpus_hash,
                    pricing_hash=PRICING_HASH,
                ),
                sink=MetadataSink(stream),
            )
        report["response"] = response.model_dump(mode="json")
        if _matches(response, corpus) and recording.generation_attempts == 2 and not ledger.stopped:
            report["mechanical_acceptance"] = "PASS"
            assert response.tool_result is not None
            report["status_explanation"] = TOOL_STATUS_TEXT[response.tool_result.status]
        else:
            report["error"] = "invalid_generated_output"
    except asyncio.CancelledError:
        report["error"] = "deadline_exceeded"
        raise
    except ProviderFailure as error:
        report["error"] = error.code
    except Exception:
        report["error"] = "provider_error"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                report["error"] = "provider_error"
        path = evidence_dir / "metadata.jsonl"
        records = (
            [TraceRecord.model_validate_json(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        report["generation_attempts"] = sum(r.provider_operation == "generation" for r in records)
        report["count_attempts"] = sum(r.provider_operation == "count_tokens" for r in records)
        report["real_generation_attempts"] = report["generation_attempts"] if live else 0
        report["preflight_estimates"] = recording.counts if recording is not None else []
        report["token_accounting"] = provider_cost_totals(records).model_dump(mode="json")
        report["batch_committed_usd"] = format(ledger.committed_usd, "f")
        report["reforecast_required"] = ledger.reforecast_required
        if report.get("error"):
            report["mechanical_acceptance"] = "INCOMPLETE"
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        (evidence_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the preset anonymous rent development fixture"
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--offline", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument("--billing-ready", action="store_true")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.live and not args.billing_ready:
        parser.error("Live mode requires confirmed prepaid billing with replenishment disabled")
    try:
        report = asyncio.run(
            run_smoke(
                source_commit=args.source_commit,
                evidence_dir=args.evidence_dir,
                live=args.live,
                billing_ready=args.billing_ready,
            )
        )
    except Exception:
        parser.exit(
            1, "Smoke could not start; use a valid revision and a new evidence directory.\n"
        )
    print(json.dumps(report, sort_keys=True))
    if report["mechanical_acceptance"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
