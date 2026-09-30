"""Private judge wire across the locked SDK using an in-memory HTTP transport."""

import asyncio
import builtins
import json
import os
import socket
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from anthropic import AsyncAnthropic
from judge_wire_fixtures import add_audit_bases

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
from rent_navigator.provider import MessagesPort, Reservation, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


@pytest.fixture(autouse=True)
def offline_sdk_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace the mapping without reading or retaining inherited environment values.
    monkeypatch.setattr(os, "environ", {})
    original_open, original_path_open = builtins.open, Path.open

    def reject_environment_file(path: object) -> None:
        if isinstance(path, str | Path) and any(
            part.startswith(".env") for part in Path(path).parts
        ):
            raise AssertionError("SDK controls must not read environment files")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        reject_environment_file(file)
        return original_open(file, *args, **kwargs)

    def guarded_path_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        reject_environment_file(path)
        return original_path_open(path, *args, **kwargs)

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SDK controls use only the in-memory transport")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)


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
    wire = add_audit_bases(wire, packet)
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
    assert create["max_tokens"] == 8192
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


def test_locked_sdk_honors_decreasing_judge_http_timeout_above_shared_client_default() -> None:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    value = build_smoke_input(case, corpus)
    packet = build_judge_packet(value)
    wire = add_audit_bases(
        {
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
        },
        packet,
    )
    clock = [10.0]
    requests: list[tuple[str, dict[str, Any], dict[str, float]]] = []

    class DelayedBudget(SpendLedger):
        def reserve(self, model: Any) -> Reservation:
            reservation = super().reserve(model)
            clock[0] += 5.0
            return reservation

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(
            (request.url.path, json.loads(request.content), dict(request.extensions["timeout"]))
        )
        if request.url.path == "/v1/messages/count_tokens":
            clock[0] += 55.0
            return httpx2.Response(200, json={"input_tokens": 1000})
        assert request.url.path == "/v1/messages"
        clock[0] += 60.0
        return httpx2.Response(
            200,
            json={
                "id": "msg_synthetic_deadline",
                "type": "message",
                "role": "assistant",
                "model": JUDGE_MODEL,
                "content": [{"type": "text", "text": json.dumps(wire)}],
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
    budget = DelayedBudget(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        assert dict(os.environ) == {}
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as transport:
            async with AsyncAnthropic(
                api_key="synthetic-test-only",
                http_client=transport,
                max_retries=0,
                timeout=45.0,
            ) as client:
                assert client.timeout == 45.0
                return await RealJudge(
                    cast(MessagesPort, client.messages),
                    budget=budget,
                    metadata=metadata,
                    raw_provider=raw,
                    run_id=run_id,
                    clock=lambda: clock[0],
                ).evaluate(value, context=context)

    evaluated = asyncio.run(evaluate())
    assert [path for path, _, _ in requests] == ["/v1/messages/count_tokens", "/v1/messages"]
    assert requests[0][2] == dict.fromkeys(("connect", "read", "write", "pool"), 180.0)
    assert requests[1][2] == dict.fromkeys(("connect", "read", "write", "pool"), 120.0)
    assert all(timeout > 45.0 for _, _, extensions in requests for timeout in extensions.values())
    assert requests[1][1]["max_tokens"] == 8192
    for key in ("model", "system", "messages", "thinking", "output_config"):
        assert requests[0][1][key] == requests[1][1][key]
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert records[0].value["timeout"] == 180.0 and records[2].value["timeout"] == 120.0
    assert evaluated.cost.actual_cost_usd == budget.committed_usd == Decimal("0.003")
    assert evaluated.cost.reserved_cost_usd == Decimal("0.13192")
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=evaluated,
        judgment=evaluated.judgment,
        run_id=run_id,
    )
