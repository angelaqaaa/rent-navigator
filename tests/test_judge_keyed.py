"""Keyed grammar, bounded local reasons and synthetic failure-accounting controls."""

import json
from copy import deepcopy
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any, get_args

import pytest
from anthropic import transform_schema
from anthropic.types import Message, TextBlock, ThinkingBlock
from anthropic.types import Usage as SDKUsage
from judge_schema_resolution import INITIAL_GRADE_LIMITS, lint_grade_schema, resolve_judge_schema
from test_eval_judge import harness, message, result, wire_payload
from test_judge_probe import load_probe

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    _grade_schema,
    build_judge_packet,
    build_smoke_input,
    judge_schema,
    parse_judgment,
    parse_packet_judgment,
)
from rent_navigator.eval.judge_decode import JudgeDecodeFailure, JudgeDecodeReason
from rent_navigator.eval.runner import JudgeFailure, JudgeInput


@pytest.fixture
def decode_value() -> JudgeInput:
    corpus = load_corpus()
    case = next(row for row in load_gold(Path("eval"), corpus).cases if row.id == "R04")
    return build_smoke_input(case, corpus)


def accepts(schema: dict[str, Any], value: Any) -> bool:
    """Independent evaluator for the emitted closed object/enum/boolean subset."""
    kind = schema["type"]
    if kind == "object":
        return (
            type(value) is dict
            and set(schema["required"]) <= set(value)
            and (schema["additionalProperties"] or set(value) <= set(schema["properties"]))
            and all(accepts(schema["properties"][key], item) for key, item in value.items())
        )
    if kind == "boolean":
        return type(value) is bool
    if kind == "string":
        return type(value) is str and value in schema["enum"]
    raise AssertionError("Unexpected schema keyword subset")


@pytest.mark.parametrize("field", ["required_claims", "statements"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_schema_rejects_missing_duplicate_and_foreign_ids(
    decode_value: JudgeInput, field: str, mutation: str
) -> None:
    schema = resolve_judge_schema(transform_schema(judge_schema(build_judge_packet(decode_value))))
    wire = wire_payload(result(decode_value), decode_value)
    assert accepts(schema, wire)
    first = next(iter(wire[field]))
    if mutation == "missing":
        wire[field].pop(first)
    elif mutation == "foreign":
        wire[field]["foreign"] = wire[field].pop(first)
    else:
        entries = [{"id": key, **leaf} for key, leaf in wire[field].items()]
        wire[field] = [*entries, deepcopy(entries[0])]
    assert not accepts(schema, wire)


def test_schema_binds_citation_applicability_and_policy_ids(decode_value: JudgeInput) -> None:
    schema = resolve_judge_schema(judge_schema(build_judge_packet(decode_value)))
    original = wire_payload(result(decode_value), decode_value)
    assert accepts(schema, original)
    wire = deepcopy(original)
    wire["statements"]["s1"]["citation_support"] = "not_applicable"
    assert not accepts(schema, wire)
    wire = deepcopy(original)
    wire["policy_violations"]["S09"] = True
    assert not accepts(schema, wire)
    wire = deepcopy(original)
    wire["policy_violations"]["S01"] = 1
    assert not accepts(schema, wire)


@pytest.mark.parametrize("mix", list(product((False, True), repeat=6)))
def test_contract_cardinality_maximum_for_frozen_approved_ids(mix: tuple[bool, ...]) -> None:
    schema = _grade_schema(
        ("c1", "c2", "c3", "c4"),
        {f"s{index}": cited for index, cited in enumerate(mix, 1)},
    )
    assert transform_schema(schema) == schema
    metrics = lint_grade_schema(schema)
    assert metrics["bytes"] == 1971 - 2 * sum(mix)
    assert metrics["enum_members"] == 9 and metrics["depth"] == 5
    assert metrics["unions"] == metrics["optional"] == 0
    assert INITIAL_GRADE_LIMITS == {"depth": 5, "enum_members": 9, "bytes": 2169}


def test_claim_id_length_is_not_an_invented_contract_limit() -> None:
    schema = _grade_schema(("synthetic" * 1000,), {"s1": False})
    with pytest.raises(ValueError, match="bytes"):
        lint_grade_schema(schema)


@pytest.mark.parametrize("reason", get_args(JudgeDecodeReason))
def test_every_local_decode_reason_is_closed_and_shared(
    decode_value: JudgeInput, reason: JudgeDecodeReason
) -> None:
    packet = build_judge_packet(decode_value)
    wire = wire_payload(result(decode_value), decode_value)
    changes: dict[str, Any] = {}
    if reason == "layout_invalid":
        changes["content"] = []
    elif reason == "model_mismatch":
        changes["model"] = "claude-haiku-4-5-20251001"
    elif reason == "stop_reason":
        changes["stop_reason"] = "max_tokens"
    elif reason == "json_invalid":
        changes["content"] = [{"type": "text", "text": "synthetic-secret-invalid-json"}]
    elif reason == "keys_differ":
        wire["synthetic-secret-extra"] = "synthetic-secret-value"
    elif reason == "model_invalid":
        wire["false_pass"] = 1
    elif reason == "coverage":
        wire["required_claims"].pop("c1")
    elif reason == "citation_applicability":
        wire["statements"]["s1"]["citation_support"] = "not_applicable"
    elif reason == "policy_ids":
        wire["policy_violations"].pop("S01")
    else:
        packet["approved_facts"] = {"synthetic-secret-packet": True}
    response = message(
        decode_value,
        **{"content": [{"type": "text", "text": json.dumps(wire)}], **changes},
    )
    with pytest.raises(JudgeDecodeFailure) as caught:
        parse_packet_judgment(response, packet)
    assert caught.value.decode_rejection_reason == reason
    assert caught.value.diagnostic.category == "local_guard"
    assert caught.value.diagnostic.schema_version == 2
    assert "synthetic-secret" not in str(caught.value)
    if reason != "packet_invalid":
        with pytest.raises(JudgeDecodeFailure) as paired:
            parse_judgment(response, decode_value)
        assert paired.value.decode_rejection_reason == reason


def test_decode_failure_keeps_known_cost_and_sanitized_observation(
    decode_value: JudgeInput,
) -> None:
    test = harness(decode_value)
    wire = wire_payload(result(decode_value), decode_value)
    wire["statements"]["s1"]["citation_support"] = "not_applicable"
    test.port.response = message(decode_value, content=[{"type": "text", "text": json.dumps(wire)}])
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    failure = caught.value
    assert failure.decode_rejection_reason == "citation_applicability"
    assert failure.diagnostic.schema_version == 2 and failure.diagnostic.category == "local_guard"
    assert failure.accounting.cost.actual_cost_usd == Decimal("0.003")
    assert failure.accounting.cost.usage_complete
    assert failure.accounting.cost.input_tokens == 1000
    assert failure.accounting.cost.output_tokens == 100
    test.verify(failure.accounting, None)


@pytest.mark.parametrize("reason", ["stop_reason", "model_mismatch"])
def test_adapter_rejections_keep_observed_local_reason(
    decode_value: JudgeInput, reason: str
) -> None:
    test = harness(decode_value)
    test.port.response = message(
        decode_value,
        **(
            {"stop_reason": "max_tokens"}
            if reason == "stop_reason"
            else {"model": "claude-haiku-4-5-20251001"}
        ),
    )
    with pytest.raises(JudgeFailure) as caught:
        test.run()
    assert caught.value.decode_rejection_reason == reason
    assert caught.value.diagnostic.schema_version == 2
    assert caught.value.accounting.cost.actual_cost_usd == (
        Decimal("0.003") if reason == "stop_reason" else None
    )
    assert caught.value.accounting.cost.reserved_cost_usd == Decimal("0.21384")
    test.verify(caught.value.accounting)


@pytest.mark.parametrize("packet_id", ["J01", "J03"])
@pytest.mark.parametrize(
    "rejection", [None, "json_invalid", "citation_applicability", "stop_reason"]
)
def test_probe_runs_shared_decode_once_without_payload_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    packet_id: str,
    rejection: str | None,
) -> None:
    import anthropic

    probe = load_probe()
    packet = probe.fixed_packet(packet_id)
    wire: dict[str, Any] = {
        "required_claims": {row["id"]: {"result": "met"} for row in packet["required_claims"]},
        "statements": {
            row["id"]: {
                "factual": "supported",
                "citation_support": "supported" if row["citation_ids"] else "not_applicable",
            }
            for row in packet["statements"]
        },
        "false_pass": False,
        "policy_violations": {f"S{i:02}": False for i in range(1, 9)},
    }
    if rejection == "citation_applicability":
        cited = next(row["id"] for row in packet["statements"] if row["citation_ids"])
        wire["statements"][cited]["citation_support"] = "not_applicable"
    text = (
        "synthetic-output-secret-invalid-json" if rejection == "json_invalid" else json.dumps(wire)
    )
    calls: list[dict[str, Any]] = []
    payload = json.dumps(probe.build_payload(packet_id)).encode()

    class MemoryPayload(Path):
        def read_bytes(self) -> bytes:
            return payload

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = self

        def __enter__(self) -> "RecordingClient":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def create(self, **kwargs: Any) -> Message:
            calls.append(kwargs)
            return Message.model_construct(
                id="msg_synthetic",
                type="message",
                role="assistant",
                model="claude-sonnet-5",
                content=[
                    ThinkingBlock(
                        type="thinking",
                        thinking="synthetic-thinking-secret",
                        signature="synthetic-signature-secret",
                    ),
                    TextBlock(type="text", text=text),
                ],
                stop_reason="max_tokens" if rejection == "stop_reason" else "end_turn",
                usage=SDKUsage(input_tokens=1000, output_tokens=100),
            )

    monkeypatch.setattr(anthropic, "Anthropic", RecordingClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key-secret")
    assert probe.send_payload(MemoryPayload("synthetic-memory-only"), packet_id) == int(
        rejection is not None
    )
    output = capsys.readouterr().out
    observation = json.loads(output)
    assert len(calls) == 1
    assert observation["decode_result"] == (rejection or "decoded")
    assert set(observation) == {
        "http_status",
        "stop_reason",
        "usage",
        "decode_result",
        "request_id",
    }
    assert observation["usage"]["input_tokens"] == 1000
    assert observation["usage"]["output_tokens"] == 100
    assert "synthetic-" not in output


@pytest.mark.parametrize("role", ["assistant", "user"])
def test_probe_and_real_judge_share_response_envelope_validation(
    decode_value: JudgeInput,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
) -> None:
    import anthropic

    probe = load_probe()
    response = message(decode_value).model_copy(update={"role": role})
    payload = json.dumps(probe.build_payload("R04")).encode()
    calls: list[dict[str, Any]] = []

    class MemoryPayload(Path):
        def read_bytes(self) -> bytes:
            return payload

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = self

        def __enter__(self) -> "RecordingClient":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def create(self, **kwargs: Any) -> Message:
            calls.append(kwargs)
            return response

    monkeypatch.setattr(anthropic, "Anthropic", RecordingClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-envelope-key-secret")
    probe_exit = probe.send_payload(MemoryPayload("synthetic-memory-only"), "R04")
    output = capsys.readouterr().out
    observation = json.loads(output)
    test = harness(decode_value)
    test.port.response = response
    code, reason = None, None
    if role == "user":
        with pytest.raises(JudgeFailure) as caught:
            test.run()
        failure = caught.value
        accounting = failure.accounting
        code, reason = failure.code, failure.decode_rejection_reason
        assert failure.diagnostic.schema_version == 2
        assert failure.diagnostic.category == "local_guard"
    else:
        evaluated = test.run()
        accounting = evaluated
        assert evaluated.judgment == result(decode_value)
        test.verify(evaluated, evaluated.judgment)
    assert len(calls) == len(test.port.creates) == len(test.port.counts) == 1
    assert accounting.cost.actual_cost_usd == Decimal("0.003")
    assert accounting.cost.reserved_cost_usd == Decimal("0.21384")
    assert accounting.cost.usage_complete
    assert accounting.cost.input_tokens == 1000 and accounting.cost.output_tokens == 100
    assert observation["usage"]["input_tokens"] == 1000
    assert observation["usage"]["output_tokens"] == 100
    assert set(observation) == {
        "http_status",
        "stop_reason",
        "usage",
        "decode_result",
        "request_id",
    }
    assert "synthetic-envelope-key-secret" not in output
    assert (probe_exit, observation["decode_result"], code, reason) == (
        (1, "model_invalid", "provider_error", "model_invalid")
        if role == "user"
        else (0, "decoded", None, None)
    )
