"""Synthetic audit-basis boundary checks; no semantic accuracy or remote calls."""

import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from anthropic import AsyncAnthropic
from judge_wire_fixtures import grade_only_wire
from sdk_test_boundary import (
    FILESYSTEM_APIS,
    SDKBoundary,
    constructor_filesystem_boundary,
    install_sdk_boundary,
)
from test_eval_judge import harness, message, result

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RawJudgeRecord,
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    judge_schema,
    verify_judge_records,
)
from rent_navigator.eval.runner import JudgeAccounting, JudgeEvaluation, JudgeFailure, JudgeInput
from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.provider import MessagesPort, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


@pytest.fixture
def value() -> JudgeInput:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    return build_smoke_input(case, corpus)


@pytest.fixture
def sdk_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SDKBoundary:
    return install_sdk_boundary(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "swap_reference",
        "duplicate_evidence",
        "unbound_cited_by",
        "string_canonical",
        "extra_evidence",
        "missing_reference",
        "null_citations",
        "unavailable_citation",
        "foreign_packet_field",
    ],
)
def test_schema_rejects_malformed_packet_bindings(value: JudgeInput, mutation: str) -> None:
    packet = build_judge_packet(value)
    if mutation == "swap_reference":
        packet["evidence"][0]["reference"], packet["evidence"][1]["reference"] = "e2", "e1"
    elif mutation == "duplicate_evidence":
        packet["evidence"].append(deepcopy(packet["evidence"][0]))
    elif mutation == "unbound_cited_by":
        packet["evidence"][0]["cited_by"] = ["s6"]
    elif mutation == "string_canonical":
        packet["evidence"][0]["canonical"] = "true"
    elif mutation == "extra_evidence":
        packet["evidence"][0]["answer"] = "unexpected"
    elif mutation == "missing_reference":
        del packet["evidence"][0]["reference"]
    elif mutation == "null_citations":
        packet["statements"][0]["citation_ids"] = None
    elif mutation == "unavailable_citation":
        packet["statements"][0]["citation_ids"] = ["0" * 64]
    elif mutation == "foreign_packet_field":
        packet["expected_grade"] = "supported"
    with pytest.raises(ValueError):
        judge_schema(packet)


def test_output_limit_boundary_is_accepted_with_exact_usage_and_replay(value: JudgeInput) -> None:
    test = harness(value)
    test.port.response = message(
        value,
        usage={
            "input_tokens": 1000,
            "output_tokens": MODEL_POLICIES[JUDGE_MODEL].max_output_tokens,
        },
    )
    evaluated = test.run()
    assert evaluated.cost.actual_cost_usd == Decimal("0.08392")
    test.verify(evaluated, evaluated.judgment)


def test_output_over_limit_failure_cannot_be_rehashed_into_success(value: JudgeInput) -> None:
    test = harness(value)
    test.port.response = message(
        value,
        usage={
            "input_tokens": 1000,
            "output_tokens": MODEL_POLICIES[JUDGE_MODEL].max_output_tokens + 1,
        },
    )
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.code == "invalid_generated_output"
    accounting = caught.value.accounting
    assert accounting.cost.actual_cost_usd == test.budget.committed_usd == Decimal("0.08393")
    assert len(test.port.counts) == len(test.port.creates) == 1
    test.verify(accounting)
    # Keep complete usage, costs and response bytes, but forge every status as ok.
    # Matching totals and a fresh artifact hash cannot override the shared limit.
    forged = accounting.model_copy(
        update={
            "records": [
                record.model_copy(update={"response_code": "ok"}) for record in accounting.records
            ]
        }
    )
    assert (
        sha256(forged.model_dump_json().encode()).hexdigest()
        != sha256(accounting.model_dump_json().encode()).hexdigest()
    )
    with pytest.raises(ValueError, match="output budget"):
        test.verify(forged, result(value))


@pytest.mark.parametrize("bad_field", [False, True])
def test_locked_sdk_grade_text_and_shared_output_limit_with_exact_replay(
    value: JudgeInput,
    bad_field: bool,
    monkeypatch: pytest.MonkeyPatch,
    sdk_boundary: SDKBoundary,
) -> None:
    packet = build_judge_packet(value)
    wire = grade_only_wire(
        {
            "required_claims": {claim.id: {"result": "met"} for claim in value.required_claims},
            "statements": {
                item.id: {"factual": "supported", "citation_support": "supported"}
                for item in value.response.statements
            },
            "false_pass": False,
            "policy_violations": [],
        },
        packet,
    )
    if bad_field:
        wire["statements"][next(iter(wire["statements"]))]["basis"] = "Forbidden legacy field"
    original = " \n" + json.dumps(wire, indent=2) + "\n "
    requests: list[dict[str, Any]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if request.url.path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": 1000})
        return httpx2.Response(
            200,
            json={
                "id": "msg_synthetic_audit",
                "type": "message",
                "role": "assistant",
                "model": JUDGE_MODEL,
                "content": [{"type": "text", "text": original}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1000, "output_tokens": 400},
            },
        )

    raw, metadata = StringIO(), StringIO()
    run_id = UUID("10000000-0000-4000-8000-000000000009")
    context = TraceContext(
        attempt_id=value.response.attempt_id,
        trace_id=UUID("20000000-0000-4000-8000-000000000009"),
        phase="judge",
        source_commit="a" * 40,
        config_hash=judge_config_hash(),
        corpus_hash="b" * 64,
        pricing_hash=PRICING_HASH,
    )
    budget = SpendLedger(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        transport = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk_boundary.transports.append(transport)
        client: AsyncAnthropic | None = None
        before = [(owner, name, getattr(owner, name)) for owner, name in FILESYSTEM_APIS]
        try:
            with constructor_filesystem_boundary(monkeypatch, sdk_boundary):
                client = AsyncAnthropic(
                    api_key="synthetic-test-only", http_client=transport, max_retries=0
                )
            assert client.credentials is None and client._token_cache is None
            assert sdk_boundary.attempts == [("Path.open", sdk_boundary.pointer)]
            assert not requests
            assert all(getattr(owner, name) is original for owner, name, original in before)
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=run_id,
            ).evaluate(value, context=context)
        finally:
            try:
                if client is not None:
                    await client.close()
            finally:
                await transport.aclose()
            assert all(item.is_closed for item in sdk_boundary.transports)
            assert all(getattr(owner, name) is original for owner, name, original in before)

    accounting: JudgeAccounting
    if bad_field:
        with pytest.raises(JudgeFailure) as caught:
            asyncio.run(evaluate())
        assert caught.value.code == "invalid_generated_output"
        accounting, judgment = caught.value.accounting, None
    else:
        evaluated = asyncio.run(evaluate())
        accounting, judgment = evaluated, evaluated.judgment
    assert len(requests) == 2
    assert requests[1]["max_tokens"] == MODEL_POLICIES[JUDGE_MODEL].max_output_tokens == 8192
    assert requests[0]["output_config"] == requests[1]["output_config"]
    assert requests[1]["output_config"]["format"]["schema"] == judge_schema(packet)
    assert accounting.cost.actual_cost_usd == budget.committed_usd == Decimal("0.006")
    assert original not in metadata.getvalue()
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert records[-1].value["content"][0]["text"] == original
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        judgment=judgment,
        run_id=run_id,
    )
