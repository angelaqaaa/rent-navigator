"""Synthetic audit-basis boundary checks; no semantic accuracy or remote calls."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from anthropic import AsyncAnthropic, transform_schema
from judge_wire_fixtures import add_audit_bases, answer_anchor, source_anchor
from test_eval_judge import harness, message, result, wire_payload

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RawJudgeRecord,
    RealJudge,
    _decode_judgment,
    _packet_binding,
    _scalar_pointers,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    judge_schema,
    verify_judge_records,
)
from rent_navigator.eval.runner import JudgeAccounting, JudgeEvaluation, JudgeFailure, JudgeInput
from rent_navigator.model_policy import JUDGE_MODEL, MODEL_POLICIES
from rent_navigator.models import Statement
from rent_navigator.provider import MessagesPort, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


@pytest.fixture
def value() -> JudgeInput:
    corpus = load_corpus()
    case = next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04")
    return build_smoke_input(case, corpus)


def test_handles_are_sorted_bijective_and_schema_binds_each_own_citation(value: JudgeInput) -> None:
    packet = build_judge_packet(value)
    entries = packet["evidence"]
    assert [entry["id"] for entry in entries] == sorted(entry["id"] for entry in entries)
    assert [entry["reference"] for entry in entries] == [
        f"e{i}" for i in range(1, len(entries) + 1)
    ]
    assert build_judge_packet(replace(value, evidence=tuple(reversed(value.evidence)))) == packet
    schema = judge_schema(packet)
    assert transform_schema(schema) == schema
    claim = schema["$defs"]["JudgeClaim_c1"]["properties"]["parts"]["items"]["properties"]
    anchors = claim["answer_anchors"]["items"]["anyOf"]
    assert [item["properties"]["statement_id"]["enum"][0] for item in anchors] == ["s1", "s2", "s3"]
    assert "maxLength: 360" in claim["basis"]["description"]
    for statement in value.response.statements:
        ref = schema["properties"]["statements"]["properties"][statement.id]["$ref"]
        leaf = schema["$defs"][ref.rsplit("/", 1)[1]]
        assert set(leaf["required"]) == {"factual", "citation_support", "parts"}
        assert leaf["additionalProperties"] is False
        citation = leaf["properties"]["parts"]["items"]["properties"]["citation_audit"]
        own = citation["properties"]["source_anchors"]["items"]["anyOf"]
        assert [item["properties"]["reference"]["enum"][0] for item in own] == [
            entry["reference"] for entry in entries if statement.id in entry["cited_by"]
        ]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_basis",
        "extra_basis_field",
        "blank_summary",
        "long_summary",
        "long_coverage",
        "blank_coverage",
        "numeric_summary",
        "null_basis",
        "string_refs",
        "duplicate_refs",
        "foreign_refs",
        "canonical_id_instead_of_handle",
        "foreign_answer_id",
        "duplicate_answer_id",
        "met_without_answer",
        "wrong_own_reference",
        "supported_without_reference",
        "citation_supported_tool_only",
        "object_pointer",
        "missing_pointer",
        "wildcard_pointer",
        "noncanonical_index",
        "duplicate_tool_pointer",
        "null_tool_array",
        "bad_claim_grade",
        "bad_factual_grade",
        "false_pass_integer",
        "duplicate_policy",
    ],
)
def test_malformed_audit_fields_are_rejected_without_grade_repair(
    value: JudgeInput, mutation: str
) -> None:
    packet = build_judge_packet(value)
    wire = wire_payload(result(value), value)
    claim = next(iter(wire["required_claims"].values()))
    leaf = wire["statements"]["s1"]
    factual, citation = leaf["parts"][0]["factual_audit"], leaf["parts"][0]["citation_audit"]
    if mutation == "missing_basis":
        del leaf["parts"][0]["factual_audit"]
    elif mutation == "extra_basis_field":
        factual["quote"] = "unexpected"
    elif mutation == "blank_summary":
        factual["basis"] = " \n\t"
    elif mutation == "long_summary":
        factual["basis"] = "x" * 321
    elif mutation == "long_coverage":
        claim["parts"][0]["basis"] = "x" * 361
    elif mutation == "blank_coverage":
        claim["parts"][0]["basis"] = "  "
    elif mutation == "numeric_summary":
        factual["basis"] = 3
    elif mutation == "null_basis":
        leaf["parts"][0]["factual_audit"] = None
    elif mutation == "string_refs":
        factual["source_anchors"] = "e1"
    elif mutation == "duplicate_refs":
        factual["source_anchors"] = [source_anchor(packet["evidence"][0])] * 2
    elif mutation == "foreign_refs":
        factual["source_anchors"] = [{"reference": "e99999", "quote": "Unbound"}]
    elif mutation == "canonical_id_instead_of_handle":
        factual["source_anchors"] = [
            {"reference": packet["evidence"][0]["id"], "quote": packet["evidence"][0]["text"]}
        ]
    elif mutation == "foreign_answer_id":
        claim["parts"][0]["answer_anchors"] = [{"statement_id": "s6", "quote": "Unbound"}]
    elif mutation == "duplicate_answer_id":
        claim["parts"][0]["answer_anchors"] = [answer_anchor(packet["statements"][0])] * 2
    elif mutation == "met_without_answer":
        claim["parts"][0]["answer_anchors"] = []
    elif mutation == "wrong_own_reference":
        citation["source_anchors"] = [
            source_anchor(
                next(entry for entry in packet["evidence"] if "s1" not in entry["cited_by"])
            )
        ]
    elif mutation == "supported_without_reference":
        factual["source_anchors"] = []
    elif mutation == "citation_supported_tool_only":
        citation["source_anchors"] = []
        citation["tool_fields"] = ["/observed_tool_result/notice_days"]
    elif mutation == "object_pointer":
        factual["tool_fields"] = ["/observed_tool_result/checks/0"]
    elif mutation == "missing_pointer":
        factual["tool_fields"] = ["/observed_tool_result/invented"]
    elif mutation == "wildcard_pointer":
        factual["tool_fields"] = ["/observed_tool_result/checks/*/status"]
    elif mutation == "noncanonical_index":
        factual["tool_fields"] = ["/observed_tool_result/checks/00/status"]
    elif mutation == "duplicate_tool_pointer":
        factual["tool_fields"] = ["/observed_tool_result/notice_days"] * 2
    elif mutation == "null_tool_array":
        factual["tool_fields"] = None
    elif mutation == "bad_claim_grade":
        claim["result"] = "supported"
    elif mutation == "bad_factual_grade":
        leaf["factual"] = "met"
    elif mutation == "false_pass_integer":
        wire["false_pass"] = 1
    elif mutation == "duplicate_policy":
        wire["policy_violations"] = ["S01", "S01"]
    else:
        raise AssertionError(mutation)
    original = json.dumps(wire)
    with pytest.raises(ValueError):
        _decode_judgment(original, packet)
    assert json.dumps(wire) == original


@pytest.mark.parametrize(
    "key", ["basis", "source_anchors", "tool_fields", "text", "answer_anchors"]
)
def test_recursive_equal_duplicate_audit_keys_are_rejected(value: JudgeInput, key: str) -> None:
    wire = wire_payload(result(value), value)
    text = json.dumps(wire)
    target: dict[str, Any] = (
        next(iter(wire["required_claims"].values()))["parts"][0]
        if key in {"text", "answer_anchors"}
        else wire["statements"]["s1"]["parts"][0]["factual_audit"]
    )
    encoded = json.dumps(key) + ": " + json.dumps(target[key])
    escaped_key = f'"\\u{ord(key[0]):04x}{key[1:]}"'
    changed = text.replace(
        encoded, encoded + ", " + escaped_key + ": " + json.dumps(target[key]), 1
    )
    with pytest.raises(ValueError, match="duplicate"):
        _decode_judgment(changed, build_judge_packet(value))


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


def test_cross_statement_coverage_empty_missing_claim_and_grade_independence(
    value: JudgeInput,
) -> None:
    wire = wire_payload(result(value), value)
    claims = list(wire["required_claims"].values())
    claims[0]["parts"][0]["answer_anchors"] = [
        answer_anchor(item) for item in build_judge_packet(value)["statements"][:2]
    ]
    claims[0]["parts"][0]["basis"] = "Synthetic qualification split across two answer statements."
    claims[1]["result"] = "missing"
    claims[1]["parts"][0].update(observation="omitted", answer_anchors=[])
    wire["statements"]["s3"]["citation_support"] = "supported"
    wire["statements"]["s3"]["parts"][0]["citation_audit"]["observation"] = "explicit"
    wire["statements"]["s3"]["parts"][0]["citation_audit"]["basis"] = (
        "Synthetic basis deliberately differs from the factual grade."
    )
    decoded = _decode_judgment(json.dumps(wire), build_judge_packet(value))
    assert decoded.required_claims[1].result == "missing"
    assert decoded.statements[2].factual == "contradicted"
    assert decoded.statements[2].citation_support == "supported"
    assert decoded.false_pass is True
    assert all(
        set(item) == {"id", "factual", "citation_support"}
        for item in decoded.model_dump()["statements"]
    )


def test_valid_scalar_null_tool_fields_and_rfc6901_escaping(value: JudgeInput) -> None:
    packet = build_judge_packet(value)
    assert _scalar_pointers({"a/b": {"~c": [None, True, 3]}}, "/observed_tool_result") == (
        "/observed_tool_result/a~1b/~0c/0",
        "/observed_tool_result/a~1b/~0c/1",
        "/observed_tool_result/a~1b/~0c/2",
    )
    wire = wire_payload(result(value), value)
    wire["statements"]["s1"]["parts"][0]["factual_audit"].update(
        source_anchors=[],
        tool_fields=["/expected_tool_result/checks/0/status", "/observed_tool_result/notice_days"],
    )
    assert _decode_judgment(json.dumps(wire), packet) == result(value)
    packet["expected_tool_result"]["cap_cents_exact"] = None
    nullable = "/expected_tool_result/cap_cents_exact"
    assert nullable in _packet_binding(packet).tool_fields
    wire["statements"]["s1"]["parts"][0]["factual_audit"]["tool_fields"] = [nullable]
    # A valid reference is a structural constraint, never inferred entailment.
    assert _decode_judgment(json.dumps(wire), packet) == result(value)
    packet["expected_tool_result"] = None
    packet["observed_tool_result"] = None
    assert _packet_binding(packet).tool_fields == ()
    wire["statements"]["s1"]["parts"][0]["factual_audit"]["tool_fields"] = [nullable]
    with pytest.raises(ValueError):
        _decode_judgment(json.dumps(wire), packet)


@pytest.mark.parametrize("mutate_fixed", [False, True])
def test_uncited_basis_is_fixed_and_factual_supported_remains_possible(
    value: JudgeInput, mutate_fixed: bool
) -> None:
    statements = [
        Statement(id=item.id, text=item.text, citation_ids=[]) for item in value.response.statements
    ]
    response = value.response.model_copy(update={"statements": statements, "citations": []})
    changed = replace(value, response=response, cited_evidence=())
    grades = result(value).model_dump(mode="json")
    for leaf in grades["statements"]:
        leaf["citation_support"] = "not_applicable"
    wire = wire_payload(grades, changed)
    if mutate_fixed:
        wire["statements"]["s1"]["parts"][0]["citation_audit"]["basis"] = "Nothing was cited."
        with pytest.raises(ValueError):
            _decode_judgment(json.dumps(wire), build_judge_packet(changed))
    else:
        decoded = _decode_judgment(json.dumps(wire), build_judge_packet(changed))
        assert decoded.statements[0].factual == "supported"
        assert all(
            statement.citation_support == "not_applicable" for statement in decoded.statements
        )


def test_rehashed_cross_packet_request_and_basis_tampering_fail_replay(value: JudgeInput) -> None:
    test = harness(value)
    evaluated = test.run()
    records = test.records()
    original_hash = sha256(
        "\n".join(record.model_dump_json() for record in records).encode()
    ).hexdigest()
    for record in records:
        if record.event == "request":
            packet = json.loads(record.value["messages"][0]["content"])
            packet["evidence"][0]["text"] += " Synthetic foreign packet passage."
            record.value["messages"][0]["content"] = json.dumps(
                packet, sort_keys=True, separators=(",", ":")
            )
            record.value["output_config"]["format"]["schema"] = judge_schema(packet)
    rehashed = sha256(
        "\n".join(record.model_dump_json() for record in records).encode()
    ).hexdigest()
    assert rehashed != original_hash
    with pytest.raises(ValueError, match="configuration or packet"):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluated,
            judgment=evaluated.judgment,
        )

    records = test.records()
    raw = records[-1].value["content"][0]
    wire = json.loads(raw["text"])
    wire["statements"]["s1"]["parts"][0]["citation_audit"]["source_anchors"] = [
        source_anchor(
            next(
                entry
                for entry in build_judge_packet(value)["evidence"]
                if "s1" not in entry["cited_by"]
            )
        )
    ]
    raw["text"] = json.dumps(wire)
    with pytest.raises(ValueError, match="not a valid judgment"):
        verify_judge_records(
            records,
            value=value,
            context=test.context,
            accounting=evaluated,
            judgment=evaluated.judgment,
        )


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


@pytest.mark.parametrize("bad_basis", [False, True])
def test_locked_sdk_audit_text_and_shared_output_limit_with_exact_replay(
    value: JudgeInput, bad_basis: bool
) -> None:
    packet = build_judge_packet(value)
    wire = add_audit_bases(
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
    if bad_basis:
        wire["statements"]["s1"]["parts"][0]["factual_audit"]["tool_fields"] = [
            "/observed_tool_result/unknown"
        ]
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
    if bad_basis:
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


@pytest.mark.parametrize(
    "mutation",
    ["claim_grade", "factual_grade", "claim_id", "statement_id", "claim_kind", "statement_parts"],
)
def test_structural_fixture_helper_does_not_repair_invalid_grades_or_ids(
    value: JudgeInput, mutation: str
) -> None:
    packet = build_judge_packet(value)
    grades = result(value).model_dump(mode="json")
    keyed = {
        **grades,
        "required_claims": {
            row["id"]: {"result": row["result"]} for row in grades["required_claims"]
        },
        "statements": {
            row["id"]: {"factual": row["factual"], "citation_support": row["citation_support"]}
            for row in grades["statements"]
        },
    }
    if mutation == "claim_grade":
        keyed["required_claims"]["c1"]["result"] = {"invalid": True}
    elif mutation == "factual_grade":
        keyed["statements"]["s1"]["factual"] = False
    elif mutation == "claim_id":
        keyed["required_claims"]["unbound"] = keyed["required_claims"].pop("c1")
    elif mutation == "statement_id":
        keyed["statements"]["unbound"] = keyed["statements"].pop("s1")
    elif mutation == "claim_kind":
        keyed["required_claims"]["c1"]["kind"] = "invalid_branch"
    else:
        keyed["statements"]["s1"]["parts"] = False
    original = deepcopy(keyed)
    wire = add_audit_bases(keyed, packet)
    assert keyed == original
    assert set(wire["required_claims"]) == set(keyed["required_claims"])
    assert set(wire["statements"]) == set(keyed["statements"])
    for field in ("required_claims", "statements"):
        for identifier, leaf in keyed[field].items():
            for key, supplied in leaf.items():
                assert wire[field][identifier][key] == supplied
    for identifier, leaf in keyed["required_claims"].items():
        assert wire["required_claims"][identifier]["result"] == leaf["result"]
    for identifier, leaf in keyed["statements"].items():
        assert wire["statements"][identifier]["factual"] == leaf["factual"]
        assert wire["statements"][identifier]["citation_support"] == leaf["citation_support"]
    with pytest.raises(ValueError):
        _decode_judgment(json.dumps(wire), packet)
