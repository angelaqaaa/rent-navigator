"""Locked SDK schema transformation and independently bound applicability replay."""

import json
import socket
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic import transform_schema
from test_eval_judge import RUN_ID, harness, message, result, wire_payload

from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RawJudgeRecord,
    build_judge_packet,
    build_smoke_input,
    judge_schema,
    verify_judge_records,
)
from rent_navigator.eval.models import JudgeStatementResult
from rent_navigator.eval.runner import JudgeInput


@pytest.fixture(autouse=True)
def prohibit_external_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External execution is prohibited")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", prohibited)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", prohibited)
    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def patterned_value(corpus: Corpus, pattern: str) -> JudgeInput:
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    original = build_smoke_input(case, corpus)
    statements = [
        statement.model_copy(
            update={
                "citation_ids": statement.citation_ids
                if pattern == "cited" or pattern == "mixed" and statement.id != "s2"
                else []
            }
        )
        for statement in original.response.statements
    ]
    used = {identifier for statement in statements for identifier in statement.citation_ids}
    return replace(
        original,
        response=original.response.model_copy(
            update={
                "statements": statements,
                "citations": [
                    citation for citation in original.response.citations if citation.id in used
                ],
            }
        ),
        cited_evidence=tuple(chunk for chunk in original.cited_evidence if chunk.id in used),
    )


def assert_applicability(schema: dict[str, Any], packet: dict[str, Any]) -> None:
    internal_factual = JudgeStatementResult.model_json_schema()["properties"]["factual"]["enum"]
    expected = {
        "JudgeCitedStatementResult": ["supported", "unsupported"],
        "JudgeUncitedStatementResult": ["not_applicable"],
    }
    for name, domain in expected.items():
        definition = schema["$defs"][name]
        assert definition["additionalProperties"] is False
        assert (
            set(definition["properties"])
            == set(definition["required"])
            == {"factual", "citation_support"}
        )
        assert definition["properties"]["factual"]["enum"] == internal_factual
        assert definition["properties"]["citation_support"]["enum"] == domain
    for statement in packet["statements"]:
        target = (
            "JudgeCitedStatementResult"
            if statement["citation_ids"]
            else "JudgeUncitedStatementResult"
        )
        assert schema["properties"]["statements"]["properties"][statement["id"]] == {
            "$ref": "#/$defs/" + target
        }
    assert schema["additionalProperties"] is False
    for field in ("required_claims", "statements"):
        mapping = schema["properties"][field]
        assert mapping["additionalProperties"] is False
        assert mapping["required"] == sorted(item["id"] for item in packet[field])
        assert set(mapping["properties"]) == set(mapping["required"])


@pytest.mark.parametrize("pattern", ["cited", "uncited", "mixed"])
def test_locked_sdk_transform_preserves_exact_applicability_schema(
    corpus: Corpus, pattern: str
) -> None:
    packet = build_judge_packet(patterned_value(corpus, pattern))
    before = deepcopy(packet)
    internal = deepcopy(JudgeStatementResult.model_json_schema())
    schema = judge_schema(packet)
    assert_applicability(schema, packet)
    assert transform_schema(deepcopy(schema)) == schema
    assert packet == before and JudgeStatementResult.model_json_schema() == internal
    schema["$defs"]["JudgeCitedStatementResult"]["properties"]["citation_support"]["enum"].append(
        "not_applicable"
    )
    assert_applicability(judge_schema(packet), packet)


@pytest.mark.parametrize("pattern", ["cited", "uncited", "mixed"])
def test_real_judge_recorder_count_create_use_the_bound_packet_full_schema(
    corpus: Corpus, pattern: str
) -> None:
    value = patterned_value(corpus, pattern)
    packet = build_judge_packet(value)
    test = harness(value)
    payload = result(value).model_dump(mode="json")
    presence = {
        statement.id: bool(statement.citation_ids) for statement in value.response.statements
    }
    for statement in payload["statements"]:
        if not presence[statement["id"]]:
            statement["citation_support"] = "not_applicable"
    original_text = json.dumps(wire_payload(payload), indent=2)
    test.port.response = message(value, content=[{"type": "text", "text": original_text}])
    evaluated = test.run()
    assert len(test.port.counts) == len(test.port.creates) == 1
    count, create = test.port.counts[0], test.port.creates[0]
    assert set(count) == {"model", "system", "messages", "output_config", "thinking", "timeout"}
    assert {
        key: value
        for key, value in create.items()
        if key not in {"max_tokens", "stream", "service_tier", "timeout"}
    } == {key: value for key, value in count.items() if key != "timeout"}
    assert create["max_tokens"] == 800 and create["stream"] is False
    assert create["service_tier"] == "standard_only"
    assert count["timeout"] == 45.0 and create["timeout"] == pytest.approx(44.9)
    assert json.loads(count["messages"][0]["content"]) == packet
    expected_schema = judge_schema(packet)
    assert_applicability(expected_schema, packet)
    assert transform_schema(deepcopy(expected_schema)) == expected_schema
    assert (
        count["output_config"]
        == create["output_config"]
        == {"format": {"type": "json_schema", "schema": expected_schema}}
    )
    raw = test.records()
    assert raw[0].value == count and raw[2].value == create
    assert raw[-1].value["content"][0]["text"] == original_text
    assert evaluated.cost.actual_cost_usd == test.budget.committed_usd == Decimal("0.003")
    verify_judge_records(
        raw,
        value=value,
        context=test.context,
        accounting=evaluated,
        judgment=evaluated.judgment,
        run_id=RUN_ID,
    )


@pytest.mark.parametrize("mutation", ["wrong_ref", "wrong_packet", "matching_wrong_packet_schema"])
def test_rehashed_real_collection_judge_artifacts_reject_unbound_applicability(
    corpus: Corpus,
    tmp_path: Path,
    mutation: str,
) -> None:
    from eval_fixtures import write_toy_data
    from test_eval_real_collection import SyntheticRealCompositionPort, run
    from test_eval_usage_evidence import replace_rows, rows, verify_partial

    dataset = load_gold(write_toy_data(tmp_path / "data", corpus), corpus)
    directory = tmp_path / "partial"
    manifest = run(directory, dataset, corpus, SyntheticRealCompositionPort(fail_judge=True))
    assert len(manifest.started_attempts) == 7
    assert not manifest.judge_evaluations and not manifest.reportable
    verify_partial(directory, corpus, dataset)
    name = "judge-raw-provider.jsonl"
    old_hash = sha256((directory / name).read_bytes()).hexdigest()
    raw = rows(directory, name)
    for record in raw:
        if record["event"] != "request":
            continue
        prepared = record["value"]
        schema = prepared["output_config"]["format"]["schema"]
        packet = json.loads(prepared["messages"][0]["content"])
        target = packet["statements"][0]
        if mutation == "wrong_ref":
            selected = (
                "JudgeUncitedStatementResult"
                if target["citation_ids"]
                else "JudgeCitedStatementResult"
            )
            schema["properties"]["statements"]["properties"][target["id"]] = {
                "$ref": "#/$defs/" + selected
            }
        else:
            target["citation_ids"] = [] if target["citation_ids"] else [packet["evidence"][0]["id"]]
            prepared["messages"][0]["content"] = json.dumps(packet)
            if mutation == "matching_wrong_packet_schema":
                prepared["output_config"]["format"]["schema"] = judge_schema(packet)
    # Refresh actual artifact hashes: independent replay must reject content, not a stale digest.
    replace_rows(directory, name, raw)
    current_hash = sha256((directory / name).read_bytes()).hexdigest()
    assert current_hash != old_hash
    assert json.loads((directory / "manifest.json").read_text())["files"][name] == current_hash
    assert all(RawJudgeRecord.model_validate_json(json.dumps(item)) for item in raw)
    with pytest.raises(ValueError):
        verify_partial(directory, corpus, dataset)
