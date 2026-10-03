"""Private judge wire across the locked SDK using an in-memory HTTP transport."""

import asyncio
import builtins
import json
import os
import socket
from collections.abc import Callable, Coroutine
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import anthropic
import httpx2
import pytest
from anthropic import AsyncAnthropic as RealAsyncAnthropic
from anthropic import DefaultAsyncHttpxClient
from judge_wire_fixtures import add_audit_bases

import rent_navigator.provider as provider_module
from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    JUDGE_SYSTEM,
    RawJudgeRecord,
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    judge_schema,
    validate_smoke_result,
    verify_judge_records,
)
from rent_navigator.eval.runner import (
    JudgeAccounting,
    JudgeEvaluation,
    JudgeFailure,
    JudgeInput,
)
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.provider import MessagesPort, Reservation, SpendLedger, create_client
from rent_navigator.trace import PRICING_HASH, TraceContext, provider_cost_totals

RUN_ID = UUID("10000000-0000-4000-8000-000000000009")
SYNTHETIC_KEY = "synthetic-test-only"
GRAMMAR_MESSAGE = "Schema is too complex for compilation."


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


def synthetic_input() -> tuple[JudgeInput, dict[str, Any], TraceContext]:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    value = build_smoke_input(case, corpus)
    context = TraceContext(
        attempt_id=UUID("20000000-0000-4000-8000-000000000009"),
        trace_id=UUID("30000000-0000-4000-8000-000000000009"),
        phase="judge",
        source_commit="a" * 40,
        config_hash=judge_config_hash(),
        corpus_hash=corpus.corpus_hash,
        pricing_hash=PRICING_HASH,
    )
    return value, build_judge_packet(value), context


def synthetic_wire(value: JudgeInput, packet: dict[str, Any]) -> dict[str, Any]:
    return add_audit_bases(
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


def synthetic_message(text: str) -> dict[str, Any]:
    return {
        "id": "msg_synthetic_private_judge",
        "type": "message",
        "role": "assistant",
        "model": JUDGE_MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }


def production_client(
    monkeypatch: pytest.MonkeyPatch,
    handle: Callable[[httpx2.Request], Coroutine[None, None, httpx2.Response]],
) -> RealAsyncAnthropic:
    assert anthropic.__version__ == "1.7.0" and httpx2.__version__ == "2.13.0"
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    assert dict(os.environ) == {}
    constructor_calls: list[dict[str, Any]] = []

    def construct(**kwargs: Any) -> RealAsyncAnthropic:
        constructor_calls.append(kwargs)
        return RealAsyncAnthropic(
            **kwargs,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)),
        )

    monkeypatch.setattr(provider_module, "AsyncAnthropic", construct)
    client = create_client(api_key=SYNTHETIC_KEY)
    assert constructor_calls == [
        {
            "api_key": SYNTHETIC_KEY,
            "base_url": "https://api.anthropic.com",
            "max_retries": 0,
            "timeout": 45.0,
        }
    ]
    assert client.max_retries == 0 and client.timeout == 45.0
    assert client._strict_response_validation is False
    return client


def assert_closed_objects(value: object) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
            assert value["required"] == list(value["properties"])
        for child in value.values():
            assert_closed_objects(child)
    elif isinstance(value, list):
        for child in value:
            assert_closed_objects(child)


def assert_final_sdk_requests(
    requests: list[httpx2.Request],
    records: list[RawJudgeRecord],
    packet: dict[str, Any],
    timeouts: tuple[float, ...] = (180.0, 180.0),
) -> None:
    paths = ["/v1/messages/count_tokens", "/v1/messages"][: len(timeouts)]
    assert [request.url.path for request in requests] == paths
    retained = [record for record in records if record.event == "request"]
    assert len(requests) == len(retained) == len(timeouts)
    bodies: list[dict[str, Any]] = []
    for request, prepared, timeout in zip(requests, retained, timeouts, strict=True):
        assert request.method == "POST"
        assert str(request.url) == "https://api.anthropic.com" + request.url.path
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["x-api-key"] == SYNTHETIC_KEY
        assert request.headers["x-stainless-retry-count"] == "0"
        assert "authorization" not in request.headers
        assert not any("beta" in name or "workspace" in name for name in request.headers)
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), timeout
        )
        assert prepared.value["timeout"] == timeout
        body = json.loads(request.content)
        assert body == {key: value for key, value in prepared.value.items() if key != "timeout"}
        bodies.append(body)

    # These fixed contract assertions are independent of retained prepared payloads.
    shared = {"model", "system", "messages", "thinking", "output_config"}
    assert set(bodies[0]) == shared
    assert bodies[0]["model"] == "claude-sonnet-5" == JUDGE_MODEL
    assert bodies[0]["system"] == JUDGE_SYSTEM
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["messages"] == [
        {"role": "user", "content": json.dumps(packet, sort_keys=True, separators=(",", ":"))}
    ]
    assert set(bodies[0]["output_config"]) == {"format"}
    assert set(bodies[0]["output_config"]["format"]) == {"type", "schema"}
    assert bodies[0]["output_config"]["format"]["type"] == "json_schema"
    schema = bodies[0]["output_config"]["format"]["schema"]
    assert schema == judge_schema(packet)
    assert schema["required"] == [
        "required_claims",
        "statements",
        "false_pass",
        "policy_violations",
    ]
    assert schema["properties"]["required_claims"]["required"] == ["c1", "c2"]
    assert schema["properties"]["statements"]["required"] == ["s1", "s2", "s3"]
    assert list(schema["$defs"]) == [
        "JudgeClaim_c1",
        "JudgeClaim_c2",
        "JudgeStatement_s1",
        "JudgeStatement_s2",
        "JudgeStatement_s3",
    ]
    for identifier in ("c1", "c2"):
        assert schema["properties"]["required_claims"]["properties"][identifier] == {
            "$ref": "#/$defs/JudgeClaim_" + identifier
        }
        claim = schema["$defs"]["JudgeClaim_" + identifier]
        assert claim["required"] == ["kind", "result", "parts"]
        assert claim["properties"]["parts"]["items"]["required"] == [
            "text",
            "observation",
            "answer_anchors",
            "basis",
        ]
    for identifier in ("s1", "s2", "s3"):
        assert schema["properties"]["statements"]["properties"][identifier] == {
            "$ref": "#/$defs/JudgeStatement_" + identifier
        }
        statement = schema["$defs"]["JudgeStatement_" + identifier]
        assert statement["required"] == ["factual", "citation_support", "parts"]
        assert statement["properties"]["parts"]["items"]["required"] == [
            "text",
            "factual_audit",
            "citation_audit",
        ]
    assert_closed_objects(schema)
    if len(bodies) == 2:
        assert set(bodies[1]) == shared | {"max_tokens", "stream", "service_tier"}
        assert bodies[1]["max_tokens"] == 8192
        assert bodies[1]["stream"] is False
        assert bodies[1]["service_tier"] == "standard_only"
        for key in shared:
            assert bodies[0][key] == bodies[1][key]


@pytest.mark.parametrize("duplicate_root", [False, True])
def test_locked_sdk_preserves_wire_schema_original_text_and_failed_billing(
    duplicate_root: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, packet, context = synthetic_input()
    original_text = "\n " + json.dumps(synthetic_wire(value, packet), indent=2) + " \n"
    original_text = original_text.replace('"required_claims"', '"\\u0072equired_claims"')
    if duplicate_root:
        original_text = original_text.replace(
            '"false_pass": true', '"false_pass": true, "false_pass": true'
        )
    requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        return httpx2.Response(200, json=synthetic_message(original_text))

    raw, metadata = StringIO(), StringIO()
    budget = SpendLedger(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with production_client(monkeypatch, handle) as client:
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=RUN_ID,
                clock=lambda: 10.0,
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
    assert accounting.cost.actual_cost_usd == budget.committed_usd == Decimal("0.003")
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert_final_sdk_requests(requests, records, packet)
    assert records[-1].value["content"][0]["text"] == original_text
    assert original_text not in metadata.getvalue()
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        judgment=judgment,
        run_id=RUN_ID,
    )


def test_locked_sdk_honors_decreasing_judge_http_timeout_above_shared_client_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, packet, context = synthetic_input()
    text = json.dumps(synthetic_wire(value, packet))
    clock = [10.0]
    requests: list[httpx2.Request] = []

    class DelayedBudget(SpendLedger):
        def reserve(self, model: Any) -> Reservation:
            reservation = super().reserve(model)
            clock[0] += 5.0
            return reservation

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/v1/messages/count_tokens":
            clock[0] += 55.0
            return httpx2.Response(200, json={"input_tokens": 1000})
        clock[0] += 60.0
        return httpx2.Response(200, json=synthetic_message(text))

    raw, metadata = StringIO(), StringIO()
    budget = DelayedBudget(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with production_client(monkeypatch, handle) as client:
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=RUN_ID,
                clock=lambda: clock[0],
            ).evaluate(value, context=context)

    evaluated = asyncio.run(evaluate())
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert_final_sdk_requests(requests, records, packet, (180.0, 120.0))
    assert evaluated.cost.actual_cost_usd == budget.committed_usd == Decimal("0.003")
    assert evaluated.cost.reserved_cost_usd == Decimal("0.13192")
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=evaluated,
        judgment=evaluated.judgment,
        run_id=RUN_ID,
    )


@pytest.mark.parametrize("body_shape", ["nested", "direct", "sensitive"])
def test_locked_sdk_generation_400_diagnostic_privacy_and_unknown_hold(
    body_shape: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    value, packet, context = synthetic_input()
    body_sentinel, header_sentinel = "synthetic-sensitive-error-body", "synthetic-sensitive-header"
    error = {"type": "invalid_request_error", "message": GRAMMAR_MESSAGE}
    if body_shape == "sensitive":
        error["message"] = body_sentinel
    body = error if body_shape == "direct" else {"type": "error", "error": error}
    requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        return httpx2.Response(400, json=body, headers={"x-request-id": header_sentinel})

    raw, metadata = StringIO(), StringIO()
    budget = SpendLedger(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with production_client(monkeypatch, handle) as client:
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=RUN_ID,
                clock=lambda: 10.0,
            ).evaluate(value, context=context)

    with pytest.raises(JudgeFailure) as caught:
        asyncio.run(evaluate())
    failure = caught.value
    assert failure.code == "provider_error"
    assert failure.__cause__ is None and failure.__context__ is None
    accounting = failure.accounting
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert_final_sdk_requests(requests, records, packet)
    assert records[-1].event == "failure"
    assert records[-1].value == {
        "code": "provider_error",
        "diagnostic": {
            "schema_version": 2,
            "category": "http_error",
            "http_status": 400,
            "rejection_reason": "unclassified"
            if body_shape == "sensitive"
            else "schema_complexity",
        },
    }
    assert accounting.cost.actual_cost_usd is None
    assert accounting.cost.usage_complete is False
    assert accounting.cost.reserved_cost_usd == budget.committed_usd == Decimal("0.13192")
    assert budget.stopped
    assert provider_cost_totals(accounting.records) == accounting.cost
    generation = [r for r in accounting.records if r.provider_operation == "generation"]
    assert len(generation) == 1 and generation[0].actual_cost_usd is None
    assert generation[0].input_tokens is None and generation[0].output_tokens is None
    retained = raw.getvalue() + metadata.getvalue() + caplog.text + str(failure)
    assert all(text not in retained for text in (body_sentinel, header_sentinel, GRAMMAR_MESSAGE))
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        run_id=RUN_ID,
    )


@pytest.mark.parametrize("usage", [None, {"input_tokens": 1000}])
def test_locked_sdk_missing_serving_usage_does_not_infer_free_cost(
    usage: dict[str, int] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, packet, context = synthetic_input()
    response = synthetic_message(json.dumps(synthetic_wire(value, packet)))
    if usage is None:
        del response["usage"]
    else:
        response["usage"] = usage
    requests: list[httpx2.Request] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        return httpx2.Response(200, json=response)

    raw, metadata = StringIO(), StringIO()
    budget = SpendLedger(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with production_client(monkeypatch, handle) as client:
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=RUN_ID,
                clock=lambda: 10.0,
            ).evaluate(value, context=context)

    with pytest.raises(JudgeFailure) as caught:
        asyncio.run(evaluate())
    accounting = caught.value.accounting
    assert caught.value.code == "provider_error"
    assert accounting.cost.actual_cost_usd is None
    assert accounting.cost.usage_complete is False
    assert accounting.cost.reserved_cost_usd == budget.committed_usd == Decimal("0.13192")
    assert budget.stopped
    assert provider_cost_totals(accounting.records) == accounting.cost
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert_final_sdk_requests(requests, records, packet)
    assert records[-1].event == "response"
    assert records[-1].value.get("usage") == usage
    generation = [r for r in accounting.records if r.provider_operation == "generation"]
    assert len(generation) == 1 and generation[0].output_tokens is None
    assert generation[0].input_tokens == (None if usage is None else 1000)
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        run_id=RUN_ID,
    )


def test_locked_sdk_count_400_never_admits_generation_or_reserves_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, packet, context = synthetic_input()
    requests: list[httpx2.Request] = []

    class CountOnlyBudget(SpendLedger):
        reservation_attempts = 0

        def reserve(self, model: Any) -> Reservation:
            self.reservation_attempts += 1
            raise AssertionError("Failed counting must not admit generation")

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "Rejected synthetic count."},
            },
        )

    raw, metadata = StringIO(), StringIO()
    budget = CountOnlyBudget(Decimal("1"))

    async def evaluate() -> JudgeEvaluation:
        async with production_client(monkeypatch, handle) as client:
            return await RealJudge(
                cast(MessagesPort, client.messages),
                budget=budget,
                metadata=metadata,
                raw_provider=raw,
                run_id=RUN_ID,
                clock=lambda: 10.0,
            ).evaluate(value, context=context)

    with pytest.raises(JudgeFailure) as caught:
        asyncio.run(evaluate())
    accounting = caught.value.accounting
    records = [RawJudgeRecord.model_validate_json(line) for line in raw.getvalue().splitlines()]
    assert_final_sdk_requests(requests, records, packet, (180.0,))
    assert records[-1].value == {
        "code": "provider_error",
        "diagnostic": {
            "schema_version": 2,
            "category": "http_error",
            "http_status": 400,
            "rejection_reason": "unclassified",
        },
    }
    assert accounting.cost.actual_cost_usd == accounting.cost.reserved_cost_usd == Decimal("0")
    assert accounting.cost.usage_complete and budget.committed_usd == 0 and not budget.stopped
    assert budget.reservation_attempts == 0
    assert not any(r.provider_operation == "generation" for r in accounting.records)
    assert provider_cost_totals(accounting.records) == accounting.cost
    verify_judge_records(
        records,
        value=value,
        context=context,
        accounting=accounting,
        run_id=RUN_ID,
    )
