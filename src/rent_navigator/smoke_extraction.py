"""Explicit development smoke over two fixed anonymous fixtures; never a public entry."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from anthropic import AsyncAnthropic
from anthropic.types import Message, MessageTokensCount, TextBlock, Usage
from pydantic import TypeAdapter

from rent_navigator.corpus import load_corpus
from rent_navigator.extract import extract_letter, extraction_config_hash
from rent_navigator.models import Extraction, ExtractRequest, SourceCommit
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
    JUDGE_MODEL,
    PRICING_HASH,
    MetadataSink,
    ReturnedModel,
    TraceContext,
    TraceRecord,
    TraceRecorder,
    provider_cost_totals,
)

CASES = (
    (
        "explicit",
        "The current rent is $1,234.50. The proposed rent is $1,259.80, effective April 1, 2027.",
        '{"current_cents":123450,"proposed_cents":125980,"effective_on":"2027-04-01"}',
    ),
    (
        "undetermined",
        "The current rent, proposed rent and effective date have not yet been determined.",
        '{"current_cents":null,"proposed_cents":null,"effective_on":null}',
    ),
)


def synthetic_redactor(letter: str) -> str:
    """Accept only the preset anonymous fixtures until the real redactor exists."""
    if letter not in {case[1] for case in CASES}:
        raise ValueError("Only preset synthetic fixtures are permitted")
    return letter


class _OfflineMessages:
    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        return MessageTokensCount(input_tokens=100)

    async def create(self, **kwargs: Any) -> Message:
        letter = kwargs["messages"][0]["content"]
        expected = next(case[2] for case in CASES if case[1] == letter)
        return Message(
            id="msg_synthetic",
            type="message",
            role="assistant",
            model=ACTOR_MODEL,
            content=[TextBlock(type="text", text=expected)],
            stop_reason="end_turn",
            stop_sequence=None,
            usage=Usage(input_tokens=100, output_tokens=20),
        )


async def run_smoke(
    *, source_commit: str, evidence_dir: Path, live: bool = False, billing_ready: bool = False
) -> dict[str, Any]:
    """Write a new evidence directory, stopping after any error or result mismatch."""
    source_commit = TypeAdapter(SourceCommit).validate_python(source_commit, strict=True)
    if live and not billing_ready:
        raise ProviderFailure("budget_exhausted")
    # Refuse to overwrite a previous attempt, including an incomplete attempt.
    evidence_dir.mkdir(parents=True, exist_ok=False)
    corpus = load_corpus()
    report: dict[str, Any] = {
        "mode": "live_development" if live else "offline_synthetic",
        "source_commit": source_commit,
        "config_hash": extraction_config_hash(),
        "corpus_hash": corpus.corpus_hash,
        "pricing_hash": PRICING_HASH,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "availability": [],
        "cases": [],
        "generation_attempts": 0,
        "real_generation_attempts": 0,
        "live_acceptance": "NOT RUN" if not live else "INCOMPLETE",
        "budget_reservation_limit_usd": "0.022",
        "purpose": "synthetic development verification; not release measurements",
    }
    client: AsyncAnthropic | None = None
    ledger = SpendLedger(Decimal("0.022"))
    try:
        messages: MessagesPort
        if live:
            client = create_client(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            for model in (ACTOR_MODEL, JUDGE_MODEL):
                availability: dict[str, Any] = {"requested_model": model, "available": None}
                report["availability"].append(availability)
                info = await client.models.retrieve(model, timeout=15.0)
                returned_id = TypeAdapter(ReturnedModel).validate_python(info.id, strict=True)
                availability.update(returned_model=returned_id, available=True)
            messages = cast(MessagesPort, client.messages)
        else:
            messages = _OfflineMessages()
        provider = ProviderAdapter(messages, budget=ledger)
        with (evidence_dir / "metadata.jsonl").open("x", encoding="utf-8") as stream:
            for name, letter, expected_json in CASES:
                attempt_id = uuid4()
                request = ExtractRequest(attempt_id=attempt_id, letter=letter)
                trace = TraceRecorder(
                    TraceContext(
                        attempt_id=attempt_id,
                        trace_id=uuid4(),
                        phase="extraction",
                        source_commit=source_commit,
                        config_hash=report["config_hash"],
                        corpus_hash=corpus.corpus_hash,
                        pricing_hash=PRICING_HASH,
                    ),
                    MetadataSink(stream),
                )
                deadline = Deadline.start()
                item: dict[str, Any] = {"case": name, "matches_expected": False}
                try:
                    extraction = await extract_letter(
                        request,
                        provider=provider,
                        trace=trace,
                        redact=synthetic_redactor,
                        deadline=deadline,
                    )
                    expected = Extraction.model_validate_json(expected_json)
                    item["matches_expected"] = extraction == expected
                    item["extraction"] = extraction.model_dump(mode="json")
                    endpoint = trace.finish(
                        "ok" if extraction == expected else "invalid_generated_output"
                    )
                except asyncio.CancelledError:
                    trace.finish("deadline_exceeded")
                    raise
                except ProviderFailure as error:
                    endpoint = trace.finish(error.code)
                except Exception:
                    endpoint = trace.finish("provider_error")
                item["endpoint"] = endpoint.model_dump(mode="json")
                report["cases"].append(item)
                if not item["matches_expected"] or ledger.stopped:
                    break
            if (
                live
                and len(report["cases"]) == 2
                and all(case["matches_expected"] for case in report["cases"])
            ):
                report["live_acceptance"] = "PASS"
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
        if live and report.get("error"):
            report["live_acceptance"] = "INCOMPLETE"
        path = evidence_dir / "metadata.jsonl"
        records = (
            [TraceRecord.model_validate_json(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        report["generation_attempts"] = sum(
            record.provider_operation == "generation" for record in records
        )
        report["real_generation_attempts"] = report["generation_attempts"] if live else 0
        report["token_accounting"] = provider_cost_totals(records).model_dump(mode="json")
        report["batch_committed_usd"] = format(ledger.committed_usd, "f")
        report["reforecast_required"] = ledger.reforecast_required
        (evidence_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the two preset extraction fixtures")
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
    if (
        report.get("error")
        or len(report["cases"]) != 2
        or not all(case["matches_expected"] for case in report["cases"])
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
