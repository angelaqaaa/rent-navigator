"""Private judge wire across the locked SDK using an in-memory HTTP transport."""

import asyncio
import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from anthropic import AsyncAnthropic

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RawJudgeRecord,
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    judge_schema,
    validate_smoke_result,
    verify_judge_records,
)
from rent_navigator.eval.runner import JudgeAccounting, JudgeEvaluation, JudgeFailure
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.provider import MessagesPort, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


@pytest.mark.parametrize("duplicate_root", [False, True])
def test_locked_sdk_preserves_wire_schema_original_text_and_failed_billing(
    duplicate_root: bool,
) -> None:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    value = build_smoke_input(case, corpus)
    packet = build_judge_packet(value)
    wire = {
        "required_claims": {claim.id: {"result": "met"} for claim in value.required_claims},
        "statements": {
            statement.id: {
                "factual": "contradicted" if statement.id == "s3" else "supported",
                "citation_support": "unsupported" if statement.id == "s3" else "supported",
            }
            for statement in value.response.statements
        },
        "false_pass": True,
        "policy_violations": [],
    }
    original_text = "\n " + json.dumps(wire, indent=2) + " \n"
    original_text = original_text.replace('"required_claims"', '"\\u0072equired_claims"')
    if duplicate_root:
        original_text = original_text.replace(
            '"false_pass": true', '"false_pass": true, "false_pass": true'
        )
    requests: list[tuple[str, dict[str, Any]]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        assert request.url.path == "/v1/messages"
        return httpx2.Response(
            200,
            json={
                "id": "msg_synthetic_private_judge",
                "type": "message",
                "role": "assistant",
                "model": JUDGE_MODEL,
                "content": [{"type": "text", "text": original_text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            },
        )

    run_id = UUID("10000000-0000-4000-8000-000000000009")
    context = TraceContext(
        attempt_id=UUID("20000000-0000-4000-8000-000000000009"),
        trace_id=UUID("30000000-0000-4000-8000-000000000009"),
        phase="judge",
        source_commit="a" * 40,
        config_hash=judge_config_hash(),
        corpus_hash=corpus.corpus_hash,
        pricing_hash=PRICING_HASH,
    )
    raw, metadata = StringIO(), StringIO()
    budget = SpendLedger(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as transport:
            async with AsyncAnthropic(
                api_key="synthetic-test-only", http_client=transport, max_retries=0
            ) as client:
                return await RealJudge(
                    cast(MessagesPort, client.messages),
                    budget=budget,
                    metadata=metadata,
                    raw_provider=raw,
                    run_id=run_id,
                ).evaluate(value, context=context)

    accounting: JudgeAccounting
    if duplicate_root:
        with pytest.raises(JudgeFailure) as caught:
            asyncio.run(evaluate())
        assert caught.value.code == "invalid_generated_output"
        accounting = caught.value.accounting
        judgment = None
    else:
        evaluated = asyncio.run(evaluate())
        accounting, judgment = evaluated, evaluated.judgment
        validate_smoke_result(value, judgment)
    assert [path for path, _ in requests] == ["/v1/messages/count_tokens", "/v1/messages"]
    count, create = requests[0][1], requests[1][1]
    for key in ("model", "system", "messages", "thinking", "output_config"):
        assert count[key] == create[key]
    assert count["output_config"]["format"]["schema"] == judge_schema(packet)
    assert create["max_tokens"] == 800
    assert accounting.cost.actual_cost_usd == budget.committed_usd == Decimal("0.003")
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert records[-1].value["content"][0]["text"] == original_text
    assert original_text not in metadata.getvalue()
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        judgment=judgment,
        run_id=run_id,
    )
