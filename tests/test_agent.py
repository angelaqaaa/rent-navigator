"""Synthetic orchestration boundaries; all provider traffic stays in recording fakes."""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from io import StringIO
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from anthropic import AsyncAnthropic, transform_schema
from anthropic.types import Message, MessageTokensCount, TextBlock, ToolUseBlock

import rent_navigator.agent as agent_module
from rent_navigator.agent import agent_config_hash, answer
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.index import NOTICE_QUERY, RENT_QUERY, SearchHit, build_index, search
from rent_navigator.models import (
    ASK_REQUEST_ADAPTER,
    AskRequest,
    AskResponse,
    GeneratedResult,
    NoticeRequest,
    RentRequest,
    ToolResult,
    disclaimer_for,
    provider_tool_definitions,
)
from rent_navigator.notice import notice_deadline_check
from rent_navigator.provider import (
    Deadline,
    MessagesPort,
    ProviderAdapter,
    ProviderFailure,
    SpendLedger,
)
from rent_navigator.rent import rent_increase_check
from rent_navigator.trace import (
    ACTOR_MODEL,
    PRICING_HASH,
    MetadataSink,
    TraceContext,
    TraceRecord,
    provider_cost_totals,
)

ATTEMPT = UUID("fc54ed9e-4cbe-4a1e-9257-8b8cec981aca")
RAW = "SYNTHETIC PRIVATE QUESTION SENTINEL"
SAFE = "SYNTHETIC REDACTED QUESTION"
TOOL_ID = "toolu_synthetic_native_id"
REFUSALS = {
    "needs_confirmation": (
        "Case-specific calculations require confirmed facts. "
        "Use the structured form to confirm the relevant details."
    ),
    "out_of_scope": (
        "This question is outside the ordinary Ontario rent-increase and notice "
        "scope supported by this project."
    ),
    "insufficient_evidence": (
        "The available snapshot evidence is insufficient to answer this question."
    ),
}


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def request_for(mode: Literal["question", "notice", "rent"] = "rent") -> AskRequest:
    if mode == "question":
        value: dict[str, Any] = {"mode": mode, "attempt_id": str(ATTEMPT), "question": RAW}
    else:
        facts: dict[str, Any] = {
            "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
            "effective_on": "2026-09-01",
            "served_on": "2026-07-03",
            "service_method": "hand",
        }
        if mode == "rent":
            facts.update(
                current_cents=200000,
                proposed_cents=204800,
                tenancy_start="2024-09-01",
                last_increase={"state": "known", "date": "2025-09-01"},
                guideline_status="controlled",
                form="N1",
            )
        value = {"mode": mode, "attempt_id": str(ATTEMPT), "confirmed": True, "facts": facts}
    return ASK_REQUEST_ADAPTER.validate_json(json.dumps(value))


def expected_tool(request: AskRequest, corpus: Corpus) -> ToolResult:
    if isinstance(request, RentRequest):
        return rent_increase_check(request.facts, corpus=corpus)
    assert isinstance(request, NoticeRequest)
    return notice_deadline_check(request.facts, corpus=corpus)


def message(content: list[dict[str, Any]], *, stop: str = "end_turn") -> Message:
    return Message.model_validate(
        {
            "id": "msg_synthetic_agent",
            "type": "message",
            "role": "assistant",
            "model": ACTOR_MODEL,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 100},
        }
    )


def selection(request: AskRequest, *, narration: bool = False) -> Message:
    assert isinstance(request, (NoticeRequest, RentRequest))
    name = "rent_increase_check" if request.mode == "rent" else "notice_deadline_check"
    blocks: list[dict[str, Any]] = []
    if narration:
        blocks.append({"type": "text", "text": "SYNTHETIC SELECTION NARRATION"})
    blocks.append(
        {
            "type": "tool_use",
            "id": TOOL_ID,
            "name": name,
            "input": request.facts.model_dump(mode="json"),
        }
    )
    return message(blocks, stop="tool_use")


def final_message(ids: list[str], *, reason: str | None = None) -> Message:
    result = {
        "kind": "refusal" if reason else "answer",
        "refusal_reason": reason,
        "statements": []
        if reason
        else [
            {"id": "s1", "text": "Synthetic first proposition.", "citation_ids": ids},
            {"id": "s2", "text": "Synthetic second proposition.", "citation_ids": ids},
        ],
    }
    return message([{"type": "text", "text": json.dumps(result)}])


class RecordingMessages:
    def __init__(self, responses: Sequence[Message | BaseException]) -> None:
        self.responses = responses
        self.counts: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.before_count: Callable[[int], Awaitable[None]] | None = None
        self.before_create: Callable[[int], Awaitable[None]] | None = None
        self.estimates: list[int] = [1000, 1000]

    async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
        self.events.append("count")
        self.counts.append(deepcopy(kwargs))
        if self.before_count is not None:
            await self.before_count(len(self.counts))
        return MessageTokensCount(input_tokens=self.estimates[len(self.counts) - 1])

    async def create(self, **kwargs: Any) -> Message:
        self.events.append("create")
        self.creates.append(deepcopy(kwargs))
        if self.before_create is not None:
            await self.before_create(len(self.creates))
        result = self.responses[len(self.creates) - 1]
        if isinstance(result, BaseException):
            raise result
        return result


@dataclass
class Harness:
    request: AskRequest
    corpus: Corpus
    fake: RecordingMessages
    arm: Literal["production", "baseline"] = "production"

    def __post_init__(self) -> None:
        self.stream = StringIO()
        self.ledger = SpendLedger(Decimal("0.1"))
        self.context = TraceContext(
            attempt_id=self.request.attempt_id,
            trace_id=uuid4(),
            phase="analysis",
            source_commit="a" * 40,
            config_hash=agent_config_hash(self.request.mode, self.arm),
            corpus_hash=self.corpus.corpus_hash,
            pricing_hash=PRICING_HASH,
        )
        self.hits: tuple[SearchHit, ...] = (SearchHit(self.corpus.chunks[0], -1.0),)
        self.queries: list[str] = []
        self.redacted: list[str] = []

    def retrieve(self, query: str) -> tuple[SearchHit, ...]:
        self.fake.events.append("retrieve")
        self.queries.append(query)
        return self.hits

    def redact(self, text: str) -> str:
        self.fake.events.append("redact")
        self.redacted.append(text)
        return text.replace(RAW, SAFE)

    async def run(self, **overrides: Any) -> AskResponse:
        kwargs: dict[str, Any] = {
            "provider": ProviderAdapter(self.fake, budget=self.ledger),
            "corpus": self.corpus,
            "retrieve": self.retrieve,
            "redact": self.redact,
            "deadline": Deadline.start(),
            "context": self.context,
            "sink": MetadataSink(self.stream),
            "arm": self.arm,
        }
        kwargs.update(overrides)
        return await answer(self.request, **kwargs)

    def records(self) -> list[TraceRecord]:
        return [
            TraceRecord.model_validate_json(line) for line in self.stream.getvalue().splitlines()
        ]

    def endpoint(self) -> TraceRecord:
        records = [row for row in self.records() if row.record_kind == "endpoint"]
        assert len(records) == 1
        provider_cost_totals(self.records())
        return records[0]


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        found = [value]
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return found
        if not isinstance(decoded, str):
            found.extend(strings(decoded))
        return found
    if isinstance(value, dict):
        return [part for item in value.values() for part in strings(item)]
    if isinstance(value, (list, tuple)):
        return [part for item in value for part in strings(item)]
    return []


def rule_ids(result: ToolResult, corpus: Corpus) -> set[str]:
    return {evidence for rule in result.rule_ids for evidence in corpus.rule(rule).evidence_ids}


def assert_count_matches_generation(fake: RecordingMessages) -> None:
    for count, create in zip(fake.counts, fake.creates, strict=True):
        for field in (
            "model",
            "system",
            "messages",
            "tools",
            "tool_choice",
            "output_config",
            "thinking",
        ):
            assert count.get(field) == create.get(field)
        assert "max_tokens" not in count
        assert create["model"] == ACTOR_MODEL
        assert create["max_tokens"] == 600
        assert create["thinking"] == {"type": "disabled"}
        assert create["extra_body"] == {"temperature": 0}


def test_question_retrieves_verbatim_and_sends_only_redacted_one_call(corpus: Corpus) -> None:
    request = request_for("question")
    evidence = corpus.chunks[0].id
    harness = Harness(request, corpus, RecordingMessages([final_message([evidence])]))
    response = asyncio.run(harness.run())
    assert harness.queries == [RAW]
    assert harness.redacted == [RAW]
    assert len(harness.fake.creates) == len(harness.fake.counts) == 1
    assert harness.fake.events.index("redact") < harness.fake.events.index("count")
    for payload in (*harness.fake.counts, *harness.fake.creates):
        assert RAW not in json.dumps(payload)
        assert SAFE in strings(payload)
        assert not payload.get("tools")
    assert response.tool_result is None
    assert response.answer == "Synthetic first proposition.\nSynthetic second proposition."
    assert response.attempt_id == request.attempt_id
    assert response.trace_id == harness.context.trace_id
    assert response.disclaimer == disclaimer_for(corpus.snapshot_date)
    assert response.citations == [corpus.citation(evidence)]
    schema = harness.fake.creates[0]["output_config"]["format"]["schema"]
    assert schema["$defs"]["Statement"]["properties"]["citation_ids"] == {
        "type": "array",
        "title": "Citation Ids",
        "items": {"type": "string", "enum": [evidence]},
        "minItems": 1,
    }
    endpoint = harness.endpoint()
    assert endpoint.response_code == "answered"
    assert endpoint.tool_name is None
    assert not any(stage.stage == "tool_execution" for stage in endpoint.stage_durations)
    assert endpoint.retrieved_evidence_ids == (evidence,)
    assert endpoint.cited_evidence_ids == (evidence,)
    assert endpoint.actual_cost_usd == Decimal("0.0015")
    assert RAW not in harness.stream.getvalue()
    assert "Synthetic first proposition" not in harness.stream.getvalue()
    assert_count_matches_generation(harness.fake)


@pytest.mark.parametrize("mode", ["notice", "rent"])
def test_fact_native_roundtrip_exact_tool_and_all_evidence(
    corpus: Corpus, mode: Literal["notice", "rent"]
) -> None:
    request = request_for(mode)
    result = expected_tool(request, corpus)
    evidence = rule_ids(result, corpus)
    first = selection(request, narration=True)
    fake = RecordingMessages([first, final_message(sorted(evidence)[:2])])
    harness = Harness(request, corpus, fake)
    overlap = corpus.chunk(sorted(evidence)[0])
    harness.hits = (SearchHit(overlap, -2.0), SearchHit(corpus.chunks[0], -1.0))
    if overlap.id == corpus.chunks[0].id:
        harness.hits = (SearchHit(overlap, -2.0),)
    response = asyncio.run(harness.run())
    assert harness.queries == [NOTICE_QUERY if mode == "notice" else RENT_QUERY]
    assert len(fake.creates) == len(fake.counts) == 2
    assert "redact" not in fake.events
    assert fake.creates[0]["tools"] == fake.creates[1]["tools"] == provider_tool_definitions()
    assert fake.creates[0]["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert "output_config" not in fake.creates[0]
    assert fake.creates[1]["tool_choice"] == {"type": "none"}
    schema = fake.creates[1]["output_config"]["format"]["schema"]
    citation_schema = schema["$defs"]["Statement"]["properties"]["citation_ids"]
    assert citation_schema["items"]["enum"] == sorted(
        evidence | {hit.chunk.id for hit in harness.hits}
    )
    assert citation_schema["minItems"] == 1
    history = fake.creates[1]["messages"]
    assistant = next(item for item in history if item["role"] == "assistant")
    assert assistant["content"] == [block.model_dump(exclude_none=True) for block in first.content]
    user = history[-1]
    assert user["role"] == "user"
    tool_result = user["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == TOOL_ID
    assert json.loads(tool_result["content"]) == result.model_dump(mode="json")
    assert response.tool_result == result
    assert "SYNTHETIC SELECTION NARRATION" not in response.answer
    texts = strings(history)
    allowed = evidence | {hit.chunk.id for hit in harness.hits}
    for identifier in allowed:
        assert texts.count(corpus.chunk(identifier).text) == 1
    assert_count_matches_generation(fake)
    endpoint = harness.endpoint()
    assert endpoint.tool_name == result.tool
    assert [(check.id, check.status) for check in endpoint.check_statuses] == [
        (check.id, check.status) for check in result.checks
    ]
    assert endpoint.retrieved_evidence_ids == tuple(hit.chunk.id for hit in harness.hits)
    assert sum(stage.stage == "tool_execution" for stage in endpoint.stage_durations) == 1
    assert endpoint.actual_cost_usd == Decimal("0.003")
    assert endpoint.reserved_cost_usd == Decimal("0.038")
    assert "SYNTHETIC SELECTION NARRATION" not in harness.stream.getvalue()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong_name",
        "multiple",
        "empty_id",
        "no_tool_stop",
        "extra_field",
        "changed_cents",
        "string_cents",
        "bool_cents",
        "scope",
        "null",
        "date",
        "form",
        "unknown_block",
    ],
)
def test_invalid_native_calls_never_execute_or_request_final(corpus: Corpus, mutation: str) -> None:
    request = request_for()
    first = selection(request)
    block = cast(ToolUseBlock, first.content[0])
    data = deepcopy(block.input)
    if mutation == "missing":
        first = message([{"type": "text", "text": "No call"}], stop="tool_use")
    elif mutation == "wrong_name":
        block.name = "notice_deadline_check"
    elif mutation == "multiple":
        first.content.append(block.model_copy(update={"id": "toolu_second"}))
    elif mutation == "empty_id":
        block.id = ""
    elif mutation == "no_tool_stop":
        first.stop_reason = "end_turn"
    elif mutation == "unknown_block":
        first.content.append(cast(Any, {"type": "unknown", "text": RAW}))
    else:
        if mutation == "extra_field":
            data["question"] = RAW
        elif mutation == "changed_cents":
            data["proposed_cents"] = 204200
        elif mutation == "string_cents":
            data["current_cents"] = "200000"
        elif mutation == "bool_cents":
            data["current_cents"] = True
        elif mutation == "scope":
            data["scope"] = {"ordinary": "unknown", "period_start": "confirmed"}
        elif mutation == "null":
            data["served_on"] = None
        elif mutation == "date":
            data["served_on"] = "2026-02-30"
        elif mutation == "form":
            data["form"] = "n1"
        block.input = data
    harness = Harness(request, corpus, RecordingMessages([first]))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    assert caught.value.code == "tool_protocol_error"
    assert len(harness.fake.creates) == 1
    endpoint = harness.endpoint()
    assert endpoint.response_code == "tool_protocol_error"
    assert endpoint.tool_name is None
    assert not endpoint.check_statuses
    assert not any(stage.stage == "tool_execution" for stage in endpoint.stage_durations)
    assert endpoint.actual_cost_usd == Decimal("0.0015")
    assert RAW not in harness.stream.getvalue()


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "forged",
        "original_typo",
        "uppercase",
        "unretrieved",
        "extra_url",
        "sequence",
        "too_long",
        "invalid_json",
        "native_citation",
        "multiple_text",
        "tool_use",
        "refusal_stop",
        "truncated",
    ],
)
def test_final_output_and_citations_fail_closed(corpus: Corpus, invalid: str) -> None:
    request = request_for()
    result = expected_tool(request, corpus)
    allowed = rule_ids(result, corpus) | {corpus.chunks[0].id}
    ids = [sorted(allowed)[0]]
    if invalid == "missing":
        ids = []
    elif invalid == "forged":
        ids = ["f" * 64]
    elif invalid == "original_typo":
        ids = ["9dc1e76e7aa94913d4d506df9ae748a0ae9b8de10a0a9253069dec8ee258082bfa41e"]
    elif invalid == "uppercase":
        ids = [ids[0].upper()]
    elif invalid == "unretrieved":
        ids = [next(chunk.id for chunk in corpus.chunks if chunk.id not in allowed)]
    response = final_message(ids)
    text = cast(TextBlock, response.content[0])
    data = json.loads(text.text)
    if invalid == "extra_url":
        data["statements"][0]["url"] = "https://forged.invalid"
    elif invalid == "sequence":
        data["statements"][0]["id"] = "s2"
    elif invalid == "too_long":
        data["statements"][0]["text"] = "x" * 241
    elif invalid == "invalid_json":
        text.text = "SYNTHETIC MALFORMED JSON"
    elif invalid == "native_citation":
        text.citations = cast(Any, [{"type": "char_location", "document_title": RAW}])
    elif invalid == "multiple_text":
        response.content.append(TextBlock(type="text", text="extra"))
    elif invalid == "tool_use":
        response = selection(request)
    elif invalid == "refusal_stop":
        response.stop_reason = "refusal"
    elif invalid == "truncated":
        response.stop_reason = "max_tokens"
    if invalid in {"extra_url", "sequence", "too_long"}:
        text.text = json.dumps(data)
    harness = Harness(request, corpus, RecordingMessages([selection(request), response]))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    assert caught.value.code == "invalid_generated_output"
    assert len(harness.fake.creates) == 2
    endpoint = harness.endpoint()
    assert endpoint.response_code == "invalid_generated_output"
    assert endpoint.tool_name == result.tool
    assert len(endpoint.check_statuses) == 7
    assert endpoint.actual_cost_usd == Decimal("0.003")
    assert RAW not in harness.stream.getvalue()


@pytest.mark.parametrize("mode", ["question", "notice", "rent"])
@pytest.mark.parametrize("reason", list(REFUSALS))
def test_schema_refusal_has_fixed_text_and_preserves_actual_tool(
    corpus: Corpus, mode: Literal["question", "notice", "rent"], reason: str
) -> None:
    request = request_for(mode)
    responses = ([] if mode == "question" else [selection(request)]) + [
        final_message([], reason=reason)
    ]
    harness = Harness(request, corpus, RecordingMessages(responses))
    response = asyncio.run(harness.run())
    assert response.status == "refused"
    assert response.answer == REFUSALS[reason]
    assert response.statements == []
    assert response.citations == []
    assert response.tool_result == (None if mode == "question" else expected_tool(request, corpus))
    assert response.disclaimer == disclaimer_for(corpus.snapshot_date)
    assert harness.endpoint().response_code == "refused"


@pytest.mark.parametrize("mode", ["question", "notice", "rent"])
def test_baseline_has_no_retrieval_or_passages_and_allows_empty_citations(
    corpus: Corpus, mode: Literal["question", "notice", "rent"]
) -> None:
    request = request_for(mode)
    responses = ([] if mode == "question" else [selection(request)]) + [final_message([])]
    harness = Harness(request, corpus, RecordingMessages(responses), arm="baseline")
    response = asyncio.run(harness.run())
    assert not harness.queries
    assert response.citations == []
    assert response.status == "answered"
    assert len(harness.fake.creates) == (1 if mode == "question" else 2)
    all_text = strings(harness.fake.creates)
    assert all(chunk.text not in all_text for chunk in corpus.chunks)
    assert "evidence" not in json.dumps([call["messages"] for call in harness.fake.creates])
    if mode != "question":
        assert response.tool_result == expected_tool(request, corpus)
        assert (
            harness.fake.creates[0]["tools"]
            == harness.fake.creates[1]["tools"]
            == provider_tool_definitions()
        )
        assert harness.fake.creates[0]["tool_choice"] == {
            "type": "any",
            "disable_parallel_tool_use": True,
        }
        assert harness.fake.creates[1]["tool_choice"] == {"type": "none"}
    assert not harness.endpoint().retrieved_evidence_ids
    assert harness.fake.creates[-1]["output_config"]["format"]["schema"] == transform_schema(
        GeneratedResult.model_json_schema()
    )
    assert_count_matches_generation(harness.fake)


def test_final_schema_is_fresh_and_preserves_empty_and_refusal_branches(corpus: Corpus) -> None:
    original = transform_schema(GeneratedResult.model_json_schema())
    allowed = {corpus.chunks[0].id, corpus.chunks[1].id}
    production = agent_module._final_schema("production", allowed)
    assert production != original
    assert "minItems" not in production["properties"]["statements"]
    assert agent_module._final_schema("baseline", allowed) == original
    assert agent_module._final_schema("production", set()) == original
    production["$defs"]["Statement"]["properties"]["citation_ids"]["items"]["enum"].clear()
    assert agent_module._final_schema("production", allowed)["$defs"]["Statement"]["properties"][
        "citation_ids"
    ]["items"]["enum"] == sorted(allowed)
    request = request_for("question")
    harness = Harness(
        request, corpus, RecordingMessages([final_message([], reason="insufficient_evidence")])
    )
    harness.hits = ()
    assert asyncio.run(harness.run()).status == "refused"
    assert harness.fake.creates[0]["output_config"]["format"]["schema"] == original


def test_citation_policy_hash_is_static_and_binds_construction(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = agent_config_hash("rent")
    agent_module._final_schema("production", {corpus.chunks[0].id})
    agent_module._final_schema("production", {corpus.chunks[1].id})
    assert agent_config_hash("rent") == before
    policy = dict(agent_module._CITATION_SCHEMA_POLICY, version=2)
    monkeypatch.setattr(agent_module, "_CITATION_SCHEMA_POLICY", policy)
    assert agent_config_hash("rent") != before


@pytest.mark.parametrize("arm", ["production", "baseline"])
def test_failed_tool_explanation_instruction_shared_between_arms(
    arm: Literal["production", "baseline"],
) -> None:
    prompt = agent_module._system("rent", arm)
    assert "that outcome alone is not insufficient_evidence" in prompt
    assert "Refuse when the available information is truly insufficient" in prompt
    assert "R03" not in prompt
    if arm == "production":
        assert "ID in citation_ids" in prompt


@pytest.mark.parametrize("mode,valid", [("question", False), ("rent", True), ("rent", False)])
def test_baseline_optional_citations_only_resolve_executed_rules(
    corpus: Corpus, mode: Literal["question", "rent"], valid: bool
) -> None:
    request = request_for(mode)
    rules = rule_ids(expected_tool(request_for(), corpus), corpus)
    identifier = (
        sorted(rules)[0]
        if valid or mode == "question"
        else next(chunk.id for chunk in corpus.chunks if chunk.id not in rules)
    )
    responses = ([] if mode == "question" else [selection(request)]) + [final_message([identifier])]
    harness = Harness(request, corpus, RecordingMessages(responses), arm="baseline")
    if valid:
        response = asyncio.run(harness.run())
        assert response.citations == [corpus.citation(identifier)]
    else:
        with pytest.raises(ProviderFailure) as caught:
            asyncio.run(harness.run())
        assert caught.value.code == "invalid_generated_output"
    assert not harness.queries
    harness.endpoint()


@pytest.mark.parametrize("failure", ["provider", "missing_usage", "budget", "malformed"])
def test_second_call_failure_retains_first_tool_retrieval_and_accounting(
    corpus: Corpus, failure: str
) -> None:
    request = request_for()
    second: Message | BaseException = RuntimeError(RAW)
    if failure == "missing_usage":
        second = final_message([corpus.chunks[0].id]).model_copy(update={"usage": None})
    elif failure in {"budget", "malformed"}:
        second = message([{"type": "text", "text": "not json"}])
    fake = RecordingMessages([selection(request), second])
    if failure == "budget":
        fake.estimates = [1000, 15001]
    harness = Harness(request, corpus, fake)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    expected_code = {"budget": "budget_exhausted", "malformed": "invalid_generated_output"}.get(
        failure, "provider_error"
    )
    assert caught.value.code == expected_code
    endpoint = harness.endpoint()
    assert endpoint.response_code == expected_code
    assert endpoint.tool_name == "rent_increase_check"
    assert len(endpoint.check_statuses) == 7
    assert endpoint.retrieved_evidence_ids == (corpus.chunks[0].id,)
    assert not endpoint.cited_evidence_ids
    if failure in {"provider", "missing_usage"}:
        assert endpoint.actual_cost_usd is None
        assert not endpoint.usage_complete
        assert endpoint.reserved_cost_usd == Decimal("0.038")
        assert harness.ledger.stopped
    else:
        assert endpoint.actual_cost_usd == Decimal("0.0015" if failure == "budget" else "0.003")
    assert len(fake.creates) == (1 if failure == "budget" else 2)
    assert RAW not in harness.stream.getvalue()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("boundary", ["redaction", "retrieval", "first", "second"])
def test_one_shared_deadline_covers_preparation_and_both_calls(
    corpus: Corpus, boundary: str
) -> None:
    request = request_for("question" if boundary == "redaction" else "rent")
    clock = Clock()
    fake = RecordingMessages(
        ([] if boundary == "redaction" else [selection(request)])
        + [final_message([corpus.chunks[0].id])]
    )
    harness = Harness(request, corpus, fake)

    def redact(text: str) -> str:
        if boundary == "redaction":
            clock.now += 45
        return harness.redact(text)

    def retrieve(query: str) -> tuple[SearchHit, ...]:
        if boundary == "retrieval":
            clock.now += 45
        return harness.retrieve(query)

    async def advance(number: int) -> None:
        if boundary == "first" and number == 1:
            clock.now += 46
        if boundary == "second":
            clock.now += 30 if number == 1 else 16

    fake.before_create = advance
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(
            harness.run(deadline=Deadline.start(clock=clock), redact=redact, retrieve=retrieve)
        )
    assert caught.value.code == "deadline_exceeded"
    assert len(fake.creates) == {"redaction": 0, "retrieval": 0, "first": 1, "second": 2}[boundary]
    endpoint = harness.endpoint()
    assert endpoint.response_code == "deadline_exceeded"
    if boundary == "second":
        assert fake.creates[1]["timeout"] == 15
        assert endpoint.tool_name == "rent_increase_check"
        assert endpoint.actual_cost_usd == Decimal("0.003")


@pytest.mark.parametrize("call_number", [1, 2])
def test_cancellation_propagates_and_finishes_once_with_inflight_reservation(
    corpus: Corpus, call_number: int
) -> None:
    request = request_for()
    fake = RecordingMessages([selection(request), final_message([corpus.chunks[0].id])])
    harness = Harness(request, corpus, fake)

    async def cancel(number: int) -> None:
        if number == call_number:
            raise asyncio.CancelledError

    fake.before_create = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(harness.run())
    endpoint = harness.endpoint()
    assert endpoint.response_code == "deadline_exceeded"
    assert endpoint.actual_cost_usd is None
    assert endpoint.reserved_cost_usd == Decimal("0.019") * call_number
    assert endpoint.tool_name == ("rent_increase_check" if call_number == 2 else None)
    assert harness.ledger.stopped


@pytest.mark.parametrize(
    "failure",
    ["retrieve", "redactor", "context_phase", "context_attempt", "missing_chunk", "missing_rule"],
)
def test_local_failures_have_safe_error_and_exactly_one_endpoint(
    corpus: Corpus, failure: str
) -> None:
    request = request_for("question" if failure == "redactor" else "rent")
    harness = Harness(
        request,
        corpus,
        RecordingMessages([] if failure == "redactor" else [selection(request)]),
    )
    kwargs: dict[str, Any] = {}

    def explode(_value: str) -> Any:
        raise RuntimeError(RAW)

    if failure == "retrieve":
        kwargs["retrieve"] = explode
    elif failure == "redactor":
        kwargs["redact"] = explode
    elif failure == "context_phase":
        kwargs["context"] = harness.context.model_copy(update={"phase": "extraction"})
    elif failure == "context_attempt":
        kwargs["context"] = harness.context.model_copy(update={"attempt_id": uuid4()})
    elif failure == "missing_chunk":
        kwargs["corpus"] = replace(corpus, chunks=corpus.chunks[1:])
    elif failure == "missing_rule":
        kwargs["corpus"] = replace(corpus, rules=())
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(**kwargs))
    assert caught.value.code == "provider_error"
    assert RAW not in str(caught.value)
    assert RAW not in harness.stream.getvalue()
    assert harness.endpoint().response_code == "provider_error"


def test_config_identity_is_deterministic_and_separates_all_modes_and_arms() -> None:
    hashes = {
        agent_config_hash(mode, arm)
        for mode in ("question", "notice", "rent")
        for arm in ("production", "baseline")
    }
    assert len(hashes) == 6
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in hashes)
    assert agent_config_hash("rent", "production") == agent_config_hash("rent", "production")


def test_locked_sdk_serializes_native_roundtrip_and_matching_full_preflight(corpus: Corpus) -> None:
    request = request_for()
    result = expected_tool(request, corpus)
    replies = [selection(request), final_message([sorted(rule_ids(result, corpus))[0]])]
    requests: list[tuple[str, dict[str, Any]]] = []
    generated = 0

    async def handle(raw: httpx2.Request) -> httpx2.Response:
        nonlocal generated
        value = json.loads(raw.content)
        requests.append((raw.url.path, value))
        if raw.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        assert raw.url.path == "/v1/messages"
        response = replies[generated]
        generated += 1
        return httpx2.Response(200, json=response.model_dump(mode="json"))

    async def run() -> tuple[AskResponse, Harness]:
        harness = Harness(request, corpus, RecordingMessages([]))
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as transport:
            async with AsyncAnthropic(
                api_key="synthetic-test-only", http_client=transport, max_retries=0
            ) as client:
                response = await harness.run(
                    provider=ProviderAdapter(
                        cast(MessagesPort, client.messages), budget=harness.ledger
                    )
                )
        return response, harness

    response, harness = asyncio.run(run())
    assert response.tool_result == result
    assert [path for path, _ in requests] == [
        "/v1/messages/count_tokens",
        "/v1/messages",
        "/v1/messages/count_tokens",
        "/v1/messages",
    ]
    for offset in (0, 2):
        count, create = requests[offset][1], requests[offset + 1][1]
        for field in (
            "model",
            "system",
            "messages",
            "thinking",
            "tools",
            "tool_choice",
            "output_config",
        ):
            assert count.get(field) == create.get(field)
        assert create["temperature"] == 0
        assert create["thinking"] == {"type": "disabled"}
        assert create["max_tokens"] == 600
        assert create["stream"] is False
        assert "temperature" not in count
    second = requests[3][1]
    assert second["tool_choice"] == {"type": "none"}
    assert json.loads(second["messages"][-1]["content"][1]["text"]) == {
        "money_display_cad": {
            "current_rent_cad": "2000.00",
            "proposed_rent_cad": "2048.00",
            "exact_new_rent_ceiling_cad": "2042.00",
        }
    }
    assert agent_module._RENT_MONEY_SYSTEM in second["system"]
    assert second["messages"][-1]["content"][0]["tool_use_id"] == TOOL_ID
    assert second["messages"][-2]["content"][0]["type"] == "tool_use"
    assert json.loads(second["messages"][-1]["content"][0]["content"]) == result.model_dump(
        mode="json"
    )
    for identifier in rule_ids(result, corpus):
        assert corpus.chunk(identifier).text in strings(requests[2][1])
    assert harness.endpoint().actual_cost_usd == Decimal("0.003")


@pytest.mark.parametrize("attribute", ["id", "name", "input"])
def test_missing_native_tool_fields_are_protocol_errors(corpus: Corpus, attribute: str) -> None:
    request = request_for()
    first = selection(request)
    delattr(first.content[0], attribute)
    harness = Harness(request, corpus, RecordingMessages([first]))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    assert caught.value.code == "tool_protocol_error"
    assert harness.endpoint().tool_name is None
    assert len(harness.fake.creates) == 1


@pytest.mark.parametrize("block_kind", ["tool", "missing_text", "empty", "non_native"])
def test_question_cannot_execute_tools_or_accept_invalid_native_content(
    corpus: Corpus, block_kind: str
) -> None:
    response = final_message([corpus.chunks[0].id])
    if block_kind == "tool":
        response = selection(request_for())
    elif block_kind == "missing_text":
        delattr(response.content[0], "text")
    elif block_kind == "empty":
        response.content = []
    else:
        response.content = cast(Any, [{"type": "text", "text": "{}"}])
    harness = Harness(request_for("question"), corpus, RecordingMessages([response]))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    assert caught.value.code == "invalid_generated_output"
    endpoint = harness.endpoint()
    assert endpoint.tool_name is None
    assert not endpoint.check_statuses
    assert not any(stage.stage == "tool_execution" for stage in endpoint.stage_durations)
    assert len(harness.fake.creates) == 1


@pytest.mark.parametrize("state", ["unknown", "excluded", "null"])
def test_unknown_excluded_and_null_facts_are_preserved_and_may_be_explained(
    corpus: Corpus, state: str
) -> None:
    value = request_for().model_dump(mode="json")
    if state in {"unknown", "excluded"}:
        value["facts"]["scope"]["ordinary"] = state
    else:
        value["facts"].update(current_cents=None, served_on=None, service_method="unknown")
        value["facts"]["last_increase"] = {"state": "unknown", "date": None}
    request = ASK_REQUEST_ADAPTER.validate_json(json.dumps(value))
    expected = expected_tool(request, corpus)
    identifiers = sorted(rule_ids(expected, corpus))
    harness = Harness(
        request, corpus, RecordingMessages([selection(request), final_message(identifiers[:1])])
    )
    response = asyncio.run(harness.run())
    assert response.status == "answered"
    assert response.tool_result == expected
    assert expected.status == ("unsupported" if state == "excluded" else "cannot_determine")
    first_user = json.loads(harness.fake.creates[0]["messages"][0]["content"])
    assert first_user["facts"] == value["facts"]
    harness.endpoint()


@pytest.mark.parametrize("mode", ["notice", "rent"])
def test_actual_calculator_is_invoked_exactly_once_with_confirmed_facts(
    corpus: Corpus, mode: Literal["notice", "rent"], monkeypatch: pytest.MonkeyPatch
) -> None:
    request = request_for(mode)
    assert isinstance(request, (NoticeRequest, RentRequest))
    expected = expected_tool(request, corpus)
    name = expected.tool
    original = getattr(agent_module, name)
    executions: list[object] = []

    def execute(facts: Any, *, corpus: Corpus) -> ToolResult:
        executions.append(facts.model_dump(mode="json"))
        result = original(facts, corpus=corpus)
        assert isinstance(result, ToolResult)
        return result

    monkeypatch.setattr(agent_module, name, execute)
    harness = Harness(
        request,
        corpus,
        RecordingMessages([selection(request), final_message([corpus.chunks[0].id])]),
    )
    response = asyncio.run(harness.run())
    assert executions == [request.facts.model_dump(mode="json")]
    assert response.tool_result == expected
    harness.endpoint()


@pytest.mark.parametrize("bad_redactor", ["altered", "non_string", "invalid_json"])
def test_fact_redaction_callback_is_not_invoked(corpus: Corpus, bad_redactor: str) -> None:
    request = request_for()
    harness = Harness(
        request,
        corpus,
        RecordingMessages([selection(request), final_message([corpus.chunks[0].id])]),
    )
    calls: list[str] = []

    def redact(text: str) -> Any:
        calls.append(text)
        if bad_redactor == "non_string":
            return None
        if bad_redactor == "invalid_json":
            return "not JSON"
        return text.replace("204800", "204200")

    response = asyncio.run(harness.run(redact=redact))
    assert response.tool_result == expected_tool(request, corpus)
    assert not calls
    assert len(harness.fake.counts) == len(harness.fake.creates) == 2
    assert harness.endpoint().actual_cost_usd == Decimal("0.003")


@pytest.mark.parametrize("retrieval", ["duplicates", "too_many", "changed_chunk"])
def test_untrustworthy_retrieval_fails_without_generation(corpus: Corpus, retrieval: str) -> None:
    harness = Harness(request_for(), corpus, RecordingMessages([]))
    if retrieval == "duplicates":
        harness.hits *= 2
    elif retrieval == "too_many":
        harness.hits = tuple(SearchHit(chunk, -1.0) for chunk in corpus.chunks[:6])
    else:
        harness.hits = (SearchHit(corpus.chunks[0].model_copy(update={"text": RAW}), -1.0),)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run())
    assert caught.value.code == "provider_error"
    assert not harness.fake.counts
    assert not harness.fake.creates
    assert harness.endpoint().actual_cost_usd == Decimal(0)
    assert RAW not in harness.stream.getvalue()


def test_config_identity_changes_when_fixed_policy_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    original = agent_config_hash("rent", "production")
    monkeypatch.setitem(agent_module.REFUSAL_TEXT, "needs_confirmation", "Synthetic policy change.")
    assert agent_config_hash("rent", "production") != original


@pytest.mark.parametrize("arm", ["production", "baseline"])
def test_captured_money_failure_has_exact_cad_context_after_native_result(
    corpus: Corpus, arm: Literal["production", "baseline"], tmp_path: Path
) -> None:
    """Captured failure facts exercise request construction, not explanation quality."""
    request = request_for()
    result = expected_tool(request, corpus)
    first = selection(request)
    fake = RecordingMessages([first, final_message(sorted(rule_ids(result, corpus))[:1])])
    harness = Harness(request, corpus, fake, arm=arm)
    database = tmp_path / "captured-facts.sqlite"
    build_index(database, corpus)
    harness.hits = search(database, RENT_QUERY, expected_corpus_hash=corpus.corpus_hash)
    response = asyncio.run(harness.run())
    followup = fake.creates[1]["messages"][-1]["content"]
    assert followup[0] == {
        "type": "tool_result",
        "tool_use_id": TOOL_ID,
        "content": result.model_dump_json(),
    }
    assert followup[1] == {
        "type": "text",
        "text": json.dumps(
            {
                "money_display_cad": {
                    "current_rent_cad": "2000.00",
                    "proposed_rent_cad": "2048.00",
                    "exact_new_rent_ceiling_cad": "2042.00",
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    assert response.tool_result == result
    assert fake.creates[1]["messages"][-2]["content"] == [
        block.model_dump(mode="json", exclude_none=True) for block in first.content
    ]
    assert_count_matches_generation(fake)

    expected_ids = rule_ids(result, corpus) | {hit.chunk.id for hit in harness.hits}
    assert len(harness.hits) == 5
    assert len(expected_ids) == 20
    texts = strings(fake.creates[1]["messages"])
    if arm == "production":
        assert len(followup) == 3
        assert "evidence" in json.loads(followup[2]["text"])
        for identifier in expected_ids:
            assert texts.count(corpus.chunk(identifier).text) == 1
    else:
        assert len(followup) == 2
        assert not harness.queries
        assert all(chunk.text not in texts for chunk in corpus.chunks)
        assert "evidence" not in json.dumps(fake.creates[1]["messages"])
    assert agent_module._RENT_MONEY_SYSTEM in fake.creates[1]["system"]
    assert agent_module._RENT_MONEY_SYSTEM not in fake.creates[0]["system"]
    assert "money_display_cad" not in json.dumps(fake.creates[0]["messages"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (200000, "2000.00"),
        (1, "0.01"),
        ("204200", "2042.00"),
        ("102101.021", "1021.01021"),
        ("1.019", "0.01019"),
        ("0", "0.00"),
        ("-25", "-0.25"),
        ("-0.001", "-0.00001"),
        ("0.01", "0.0001"),
        ("100.01", "1.0001"),
        (1000000001, "10000000.01"),
        (None, None),
    ],
)
def test_cents_to_cad_is_an_exact_unit_shift(value: int | str | None, expected: str | None) -> None:
    assert agent_module._cents_to_cad(value) == expected


@pytest.mark.parametrize("value", [True, False, 0, -1, "-0", "1.0", "01", "1e2", "+1", "", 1.5])
def test_cents_to_cad_rejects_values_outside_source_contract(value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        agent_module._cents_to_cad(value)


def test_cents_to_cad_preserves_large_integers_without_changing_digit_limit() -> None:
    digit_limit = sys.get_int_max_str_digits()
    value = 10**4300 + 123
    expected = "1" + "0" * 4297 + "1.23"
    assert agent_module._cents_to_cad(value) == expected
    assert agent_module._cents_to_cad("1" + "0" * 4300 + ".001") == ("1" + "0" * 4298 + ".00001")
    assert sys.get_int_max_str_digits() == digit_limit


def test_cents_to_cad_does_not_depend_on_decimal_precision_or_traps() -> None:
    with localcontext() as context:
        context.prec = 1
        for signal in context.traps:
            context.traps[signal] = True
        assert agent_module._cents_to_cad("102101.021") == "1021.01021"
        assert agent_module._cents_to_cad(123456789) == "1234567.89"
        assert agent_module._cents_to_cad("-0.001") == "-0.00001"
        assert context.prec == 1
        assert not any(context.flags.values())


@pytest.mark.parametrize("arm", ["production", "baseline"])
@pytest.mark.parametrize(
    ("current", "proposed", "expected", "rounding"),
    [
        (100001, 102102, ("1000.01", "1021.02", "1021.01021"), True),
        (None, 204800, (None, "2048.00", None), False),
        (200000, None, ("2000.00", None, "2042.00"), False),
        (None, None, (None, None, None), False),
    ],
)
def test_final_money_context_preserves_fractional_ceiling_and_unknown_facts(
    corpus: Corpus,
    arm: Literal["production", "baseline"],
    current: int | None,
    proposed: int | None,
    expected: tuple[str | None, str | None, str | None],
    rounding: bool,
) -> None:
    value = request_for().model_dump(mode="json")
    value["facts"].update(current_cents=current, proposed_cents=proposed)
    request = ASK_REQUEST_ADAPTER.validate_json(json.dumps(value))
    result = expected_tool(request, corpus)
    if rounding:
        guideline = next(check for check in result.checks if check.id == "guideline")
        assert (guideline.status, guideline.reason) == ("unknown", "rounding_uncertain")
    fake = RecordingMessages(
        [selection(request), final_message(sorted(rule_ids(result, corpus))[:1])]
    )
    response = asyncio.run(Harness(request, corpus, fake, arm=arm).run())
    followup = fake.creates[1]["messages"][-1]["content"]
    assert followup[0]["content"] == result.model_dump_json()
    assert json.loads(followup[1]["text"]) == {
        "money_display_cad": dict(
            zip(
                ("current_rent_cad", "proposed_rent_cad", "exact_new_rent_ceiling_cad"),
                expected,
                strict=True,
            )
        )
    }
    assert response.tool_result == result
    assert_count_matches_generation(fake)


@pytest.mark.parametrize("arm", ["production", "baseline"])
@pytest.mark.parametrize("mode", ["question", "notice"])
def test_money_context_and_instruction_are_absent_from_other_modes(
    corpus: Corpus, arm: Literal["production", "baseline"], mode: Literal["question", "notice"]
) -> None:
    request = request_for(mode)
    replies = ([] if mode == "question" else [selection(request)]) + [
        final_message([corpus.chunks[0].id] if arm == "production" else [])
    ]
    fake = RecordingMessages(replies)
    asyncio.run(Harness(request, corpus, fake, arm=arm).run())
    for payload in (*fake.counts, *fake.creates):
        assert agent_module._RENT_MONEY_SYSTEM not in payload["system"]
        assert "money_display_cad" not in json.dumps(payload["messages"])
    assert_count_matches_generation(fake)


@pytest.mark.parametrize(
    "attribute",
    ["_MONEY_DISPLAY_KEY", "_MONEY_DISPLAY_FIELDS", "_MONEY_FORMAT_POLICY", "_RENT_MONEY_SYSTEM"],
)
def test_config_identity_covers_money_contract_and_formatter_policy(
    monkeypatch: pytest.MonkeyPatch, attribute: str
) -> None:
    original = agent_config_hash("rent", "production")
    changed: str | tuple[str, ...] = (
        ("synthetic_key",) if attribute == "_MONEY_DISPLAY_FIELDS" else "synthetic policy mutation"
    )
    monkeypatch.setattr(agent_module, attribute, changed)
    assert agent_config_hash("rent", "production") != original


def test_config_money_manifest_contains_policy_but_no_request_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    original = agent_module._json

    def capture(value: object) -> str:
        captured.append(deepcopy(value))
        return original(value)

    monkeypatch.setattr(agent_module, "_json", capture)
    assert len(agent_config_hash("rent", "production")) == 64
    assert len(captured) == 1
    values = strings(captured[0])
    assert agent_module._MONEY_DISPLAY_KEY in values
    assert all(key in values for key in agent_module._MONEY_DISPLAY_FIELDS)
    assert agent_module._MONEY_FORMAT_POLICY in values
    assert agent_module._RENT_MONEY_SYSTEM in values
    assert all(
        amount not in values for amount in ("200000", "204800", "2000.00", "2048.00", "2042.00")
    )


def test_money_instruction_preserves_units_total_rent_and_uncertainty() -> None:
    instruction = agent_module._RENT_MONEY_SYSTEM
    for clause in (
        "current_cents, proposed_cents, and cap_cents_exact are Canadian cents",
        "Use only the supplied money_display_cad strings for monetary amounts",
        "proposed_rent_cad is the proposed total new rent",
        "exact_new_rent_ceiling_cad is the exact mathematical ceiling on total new rent",
        "not the amount of an increase",
        "Do not invent or calculate other monetary figures",
        "Do not assume a monthly rental period",
        "Null amounts remain unknown",
        "rounding_uncertain conclusions without rounding",
    ):
        assert clause in instruction


@pytest.mark.parametrize("arm", ["production", "baseline"])
@pytest.mark.parametrize("mode", ["question", "notice", "rent"])
def test_section_citation_instruction_is_only_in_production_final_requests(
    corpus: Corpus,
    arm: Literal["production", "baseline"],
    mode: Literal["question", "notice", "rent"],
) -> None:
    clause = (
        "When a statement names a statutory section, cite the supplied statutory chunk for "
        "that section; otherwise omit the specific section reference."
    )
    request = request_for(mode)
    replies = ([] if mode == "question" else [selection(request)]) + [
        final_message([corpus.chunks[0].id] if arm == "production" else [])
    ]
    fake = RecordingMessages(replies)
    asyncio.run(Harness(request, corpus, fake, arm=arm).run())
    assert (clause in fake.creates[-1]["system"]) == (arm == "production")
    if mode != "question":
        assert clause not in fake.creates[0]["system"]
    assert_count_matches_generation(fake)


@pytest.mark.parametrize("arm", ["production", "baseline"])
def test_known_money_does_not_invent_a_ceiling_for_unsupported_scope(
    corpus: Corpus, arm: Literal["production", "baseline"]
) -> None:
    value = request_for().model_dump(mode="json")
    value["facts"]["scope"]["ordinary"] = "excluded"
    request = ASK_REQUEST_ADAPTER.validate_json(json.dumps(value))
    result = expected_tool(request, corpus)
    assert result.status == "unsupported"
    assert result.cap_cents_exact is None
    fake = RecordingMessages(
        [selection(request), final_message(sorted(rule_ids(result, corpus))[:1])]
    )
    response = asyncio.run(Harness(request, corpus, fake, arm=arm).run())
    assert json.loads(fake.creates[1]["messages"][-1]["content"][1]["text"]) == {
        "money_display_cad": {
            "current_rent_cad": "2000.00",
            "proposed_rent_cad": "2048.00",
            "exact_new_rent_ceiling_cad": None,
        }
    }
    assert response.tool_result == result
    assert_count_matches_generation(fake)
