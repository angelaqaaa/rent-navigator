"""Packaged synthetic attacks through real boundaries; no semantic safety scores."""

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from anthropic import transform_schema
from anthropic.types import Message, TextBlock, ToolUseBlock
from test_agent import (
    REFUSALS,
    Harness,
    RecordingMessages,
    assert_count_matches_generation,
    final_message,
    request_for,
    rule_ids,
    selection,
    strings,
)
from test_extract import synthetic_message

import rent_navigator.agent as agent_module
import rent_navigator.extract as extract_module
from rent_navigator.agent import RetrievalContext, agent_config_hash
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.extract import extract_letter, extraction_config_hash
from rent_navigator.guards import REDACTION_POLICY_HASH, redact_text
from rent_navigator.index import SearchHit
from rent_navigator.models import (
    Extraction,
    ExtractRequest,
    QuestionRequest,
    RentRequest,
    disclaimer_for,
)
from rent_navigator.provider import (
    Deadline,
    ProviderAdapter,
    ProviderFailure,
    SpendLedger,
    configuration_hash,
)
from rent_navigator.rent import rent_increase_check
from rent_navigator.security_cases import SecurityCase, load_security_cases
from rent_navigator.trace import MetadataSink, TraceContext, TraceRecorder, provider_cost_totals


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


@pytest.fixture(scope="module")
def cases(corpus: Corpus) -> dict[str, SecurityCase]:
    return {case.id: case for case in load_security_cases(corpus=corpus)}


def harness_for(case: SecurityCase, corpus: Corpus, replies: list[Message]) -> Harness:
    assert not isinstance(case.request, ExtractRequest)
    harness = Harness(case.request, corpus, RecordingMessages(replies))
    harness.hits = tuple(
        SearchHit(corpus.chunk(identifier), -float(index + 1))
        for index, identifier in enumerate(case.canonical_evidence_ids[:5])
    )
    return harness


def envelope(harness: Harness, case: SecurityCase) -> RetrievalContext:
    return RetrievalContext(harness.hits, case.injected_retrieved_text)


def payload_of(parameters: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(parameters["messages"][0]["content"])
    assert isinstance(result, dict)
    return result


def assert_isolated(harness: Harness, case: SecurityCase) -> None:
    assert case.injected_retrieved_text is not None
    for parameters in (*harness.fake.counts, *harness.fake.creates):
        payload = payload_of(parameters)
        assert payload["untrusted_retrieved_text"] == case.injected_retrieved_text
        assert payload["evidence"] == [
            {"id": hit.chunk.id, "heading": hit.chunk.heading, "text": hit.chunk.text}
            for hit in harness.hits
        ]
        assert case.injected_retrieved_text not in json.dumps(parameters["system"])
    endpoint = harness.endpoint()
    assert endpoint.retrieved_evidence_ids == tuple(hit.chunk.id for hit in harness.hits)
    assert case.injected_retrieved_text not in harness.stream.getvalue()
    assert_count_matches_generation(harness.fake)


@pytest.mark.parametrize("case_id", ["S01", "S05", "S08"])
def test_question_attack_cannot_execute_native_tools(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cases[case_id]
    rent = Mock(side_effect=AssertionError("unexpected calculator execution"))
    notice = Mock(side_effect=AssertionError("unexpected calculator execution"))
    monkeypatch.setattr(agent_module, "rent_increase_check", rent)
    monkeypatch.setattr(agent_module, "notice_deadline_check", notice)
    harness = harness_for(case, corpus, [selection(request_for())])
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(retrieve=lambda _: envelope(harness, case), redact=redact_text))
    assert caught.value.code == "invalid_generated_output"
    rent.assert_not_called()
    notice.assert_not_called()
    assert len(harness.fake.creates) == 1
    assert harness.endpoint().tool_name is None
    assert not any(s.stage == "tool_execution" for s in harness.endpoint().stage_durations)
    if case.injected_retrieved_text:
        assert_isolated(harness, case)


@pytest.mark.parametrize("case_id", ["S02", "S05"])
@pytest.mark.parametrize("forged", [True, False])
def test_citations_require_supplied_canonical_evidence(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    case_id: str,
    forged: bool,
) -> None:
    case = cases[case_id]
    identifier = (
        "0" * 64
        if forged
        else next(
            chunk.id for chunk in corpus.chunks if chunk.id not in case.canonical_evidence_ids
        )
    )
    harness = harness_for(case, corpus, [final_message([identifier])])
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(retrieve=lambda _: envelope(harness, case), redact=redact_text))
    assert caught.value.code == "invalid_generated_output"
    assert harness.endpoint().cited_evidence_ids == ()
    if case.injected_retrieved_text:
        assert_isolated(harness, case)


def test_s03_generated_disclaimer_field_is_rejected(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    case = cases["S03"]
    reply = final_message(case.canonical_evidence_ids)
    block = cast(TextBlock, reply.content[0])
    data = json.loads(block.text)
    data["disclaimer"] = "SYNTHETIC FORGED AFFILIATION"
    block.text = json.dumps(data)
    harness = harness_for(case, corpus, [reply])
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(redact=redact_text))
    assert caught.value.code == "invalid_generated_output"
    assert "SYNTHETIC FORGED AFFILIATION" not in harness.stream.getvalue()


@pytest.mark.parametrize("case_id", ["S01", "S03"])
def test_valid_question_result_keeps_exact_server_disclaimer(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    case_id: str,
) -> None:
    case = cases[case_id]
    harness = harness_for(case, corpus, [final_message(case.canonical_evidence_ids)])
    response = asyncio.run(harness.run(redact=redact_text))
    assert response.disclaimer == disclaimer_for(corpus.snapshot_date)
    assert response.tool_result is None
    assert response.citations == [corpus.citation(i) for i in case.canonical_evidence_ids]


@pytest.mark.parametrize("mutation", ["cents", "null", "date", "scope"])
def test_s04_changed_confirmed_arguments_fail_before_calculation(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cases["S04"]
    assert isinstance(case.request, RentRequest)
    reply = selection(case.request)
    block = cast(ToolUseBlock, reply.content[0])
    data = case.request.facts.model_dump(mode="json")
    if mutation == "cents":
        data["proposed_cents"] = 204200
    elif mutation == "null":
        data["served_on"] = None
    elif mutation == "date":
        data["served_on"] = "2026-07-02"
    else:
        data["scope"]["ordinary"] = "unknown"
    block.input = data
    calculator = Mock(side_effect=AssertionError("unexpected calculator execution"))
    monkeypatch.setattr(agent_module, "rent_increase_check", calculator)
    harness = harness_for(case, corpus, [reply])
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(retrieve=lambda _: envelope(harness, case), redact=redact_text))
    assert caught.value.code == "tool_protocol_error"
    calculator.assert_not_called()
    assert len(harness.fake.creates) == 1
    assert payload_of(harness.fake.creates[0])["facts"] == case.request.facts.model_dump(
        mode="json"
    )
    assert harness.endpoint().tool_name is None
    assert_isolated(harness, case)


def test_s05_sidecar_preserves_canonical_passages_and_citations(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    case = cases["S05"]
    harness = harness_for(case, corpus, [final_message(case.canonical_evidence_ids)])
    response = asyncio.run(
        harness.run(retrieve=lambda _: envelope(harness, case), redact=redact_text)
    )
    assert response.citations == [corpus.citation(i) for i in case.canonical_evidence_ids]
    assert_isolated(harness, case)


def test_s04_sidecar_does_not_replace_executed_rule_evidence(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    case = cases["S04"]
    assert isinstance(case.request, RentRequest)
    expected = rent_increase_check(case.request.facts, corpus=corpus)
    identifiers = rule_ids(expected, corpus)
    harness = harness_for(
        case, corpus, [selection(case.request), final_message(sorted(identifiers)[:2])]
    )
    response = asyncio.run(
        harness.run(retrieve=lambda _: envelope(harness, case), redact=redact_text)
    )
    assert response.tool_result == expected
    for identifier in identifiers:
        assert (
            strings(harness.fake.creates[1]["messages"]).count(corpus.chunk(identifier).text) == 1
        )
    assert_isolated(harness, case)


def test_s05_baseline_never_receives_retrieval_or_sidecar(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    case = cases["S05"]
    assert isinstance(case.request, QuestionRequest)
    assert case.injected_retrieved_text is not None
    harness = Harness(case.request, corpus, RecordingMessages([final_message([])]), arm="baseline")
    retrieve = Mock(return_value=envelope(harness, case))
    response = asyncio.run(harness.run(retrieve=retrieve, redact=redact_text))
    retrieve.assert_not_called()
    assert response.citations == []
    for parameters in (*harness.fake.counts, *harness.fake.creates):
        payload = payload_of(parameters)
        assert set(payload) == {"mode", "question"}
        assert case.injected_retrieved_text not in json.dumps(parameters)
    assert harness.endpoint().retrieved_evidence_ids == ()


def test_s06_contacts_redacted_before_extraction_and_question_requests(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = cases["S06"]
    assert isinstance(case.request, ExtractRequest)
    request = QuestionRequest(
        attempt_id=case.request.attempt_id, mode="question", question=case.request.letter
    )
    harness = Harness(request, corpus, RecordingMessages([final_message([corpus.chunks[0].id])]))
    expected_text = (
        "Contact [EMAIL] at [PHONE]; postal code [POSTAL]. The current rent is $1,234.50, "
        "the proposed rent is $1,259.80, effective 2027-04-01."
    )
    sentinels = ("unit@example.invalid", "+1 (416) 555-0123", "M5V 2T6")
    extraction_fake = RecordingMessages([synthetic_message()])
    stream = StringIO()
    context = TraceContext(
        **{
            **harness.context.model_dump(),
            "phase": "extraction",
            "trace_id": uuid4(),
            "config_hash": extraction_config_hash(),
        }
    )
    trace = TraceRecorder(context, MetadataSink(stream))

    def redact_letter(text: str) -> str:
        extraction_fake.events.append("redact")
        return redact_text(text)

    result = asyncio.run(
        extract_letter(
            case.request,
            provider=ProviderAdapter(extraction_fake, budget=SpendLedger(Decimal("0.1"))),
            trace=trace,
            redact=redact_letter,
            deadline=Deadline.start(),
        )
    )
    trace.finish("ok")
    assert result == Extraction(
        current_cents=123450, proposed_cents=125980, effective_on=date(2027, 4, 1)
    )
    assert extraction_fake.events == ["redact", "count", "create"]
    assert_count_matches_generation(extraction_fake)
    for parameters in (*extraction_fake.counts, *extraction_fake.creates):
        assert parameters["messages"] == [{"role": "user", "content": expected_text}]

    def redact_question(text: str) -> str:
        harness.fake.events.append("redact")
        return redact_text(text)

    asyncio.run(harness.run(redact=redact_question))
    assert harness.queries == [case.request.letter]
    assert harness.fake.events.index("redact") < harness.fake.events.index("count")
    for parameters in (*harness.fake.counts, *harness.fake.creates):
        assert payload_of(parameters)["question"] == expected_text
    assert_count_matches_generation(harness.fake)
    assert context.attempt_id == harness.context.attempt_id
    assert context.trace_id != harness.context.trace_id
    for sentinel in sentinels:
        assert sentinel not in json.dumps(extraction_fake.counts + extraction_fake.creates)
        assert sentinel not in json.dumps(harness.fake.counts + harness.fake.creates)
        assert sentinel not in stream.getvalue() + harness.stream.getvalue() + caplog.text
    assert expected_text not in stream.getvalue() + harness.stream.getvalue()
    assert provider_cost_totals(harness.records()).actual_cost_usd == Decimal("0.0015")


def test_s06_typed_phone_shaped_cents_never_enter_regex_redaction(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    original = cases["S04"].request
    assert isinstance(original, RentRequest)
    data = original.model_dump(mode="json")
    data["facts"]["current_cents"] = 4165550123
    request = RentRequest.model_validate_json(json.dumps(data))
    result = rent_increase_check(request.facts, corpus=corpus)
    harness = Harness(
        request,
        corpus,
        RecordingMessages(
            [
                selection(request),
                final_message(list(corpus.rule("notice.90_days").evidence_ids)),
            ]
        ),
    )
    redact = Mock(side_effect=AssertionError("typed facts must not enter regex redaction"))
    response = asyncio.run(harness.run(redact=redact))
    redact.assert_not_called()
    assert response.tool_result == result
    for parameters in (*harness.fake.counts, *harness.fake.creates):
        assert payload_of(parameters)["facts"]["current_cents"] == 4165550123
    assert_count_matches_generation(harness.fake)


@pytest.mark.parametrize("path", ["extraction", "question"])
@pytest.mark.parametrize(
    ("contact_text", "sentinels", "protected_amount"),
    [
        (
            "The fee is $1 416-555-0123 is the contact number.",
            ("416-555-0123",),
            "$1 [PHONE]",
        ),
        (
            "Rent is CAD 1 416-555-0123; call this number.",
            ("416-555-0123",),
            "CAD 1 [PHONE]",
        ),
        (
            "Contact unit@example.invalid/another@example.invalid.",
            ("unit@example.invalid", "another@example.invalid"),
            None,
        ),
        (
            "Contact unit@example.invalid+another@example.invalid.",
            ("unit@example.invalid", "another@example.invalid"),
            None,
        ),
    ],
    ids=["dollar-overlap", "cad-overlap", "slash-emails", "plus-emails"],
)
def test_contact_combinations_redacted_in_first_provider_payload(
    corpus: Corpus,
    caplog: pytest.LogCaptureFixture,
    path: str,
    contact_text: str,
    sentinels: tuple[str, ...],
    protected_amount: str | None,
) -> None:
    preserved_text = (
        "The current rent is $1,234.50, the proposed rent is $1,259.80, effective 2027-04-01. "
        "Explicit amounts remain $4165550123.00 and CAD 4165550123.00."
    )
    raw_text = f"{contact_text} {preserved_text}"
    request = QuestionRequest(attempt_id=uuid4(), mode="question", question=raw_text)
    fake = RecordingMessages(
        [synthetic_message() if path == "extraction" else final_message([corpus.chunks[0].id])]
    )
    harness = Harness(request, corpus, fake)
    stream = StringIO()
    redactor_inputs: list[str] = []

    def redact_once(text: str) -> str:
        redactor_inputs.append(text)
        fake.events.append("redact")
        return redact_text(text)

    if path == "extraction":
        context = TraceContext(
            **{
                **harness.context.model_dump(),
                "phase": "extraction",
                "trace_id": uuid4(),
                "config_hash": extraction_config_hash(),
            }
        )
        trace = TraceRecorder(context, MetadataSink(stream))
        result = asyncio.run(
            extract_letter(
                ExtractRequest(attempt_id=request.attempt_id, letter=raw_text),
                provider=ProviderAdapter(fake, budget=SpendLedger(Decimal("0.1"))),
                trace=trace,
                redact=redact_once,
                deadline=Deadline.start(),
            )
        )
        trace.finish("ok")
        assert result == Extraction(
            current_cents=123450, proposed_cents=125980, effective_on=date(2027, 4, 1)
        )
    else:
        asyncio.run(harness.run(redact=redact_once))

    assert redactor_inputs == [raw_text]
    assert fake.events.index("redact") < fake.events.index("count")
    assert len(fake.counts) == len(fake.creates) == 1
    assert_count_matches_generation(fake)
    for parameters in (*fake.counts, *fake.creates):
        sent_text = (
            parameters["messages"][0]["content"]
            if path == "extraction"
            else payload_of(parameters)["question"]
        )
        assert preserved_text in sent_text
        for sentinel in sentinels:
            assert sentinel not in json.dumps(parameters)
        if protected_amount is not None:
            assert protected_amount in sent_text
        else:
            assert sent_text.count("[EMAIL]") == 2
        assert redact_text(sent_text) == sent_text

    metadata_and_logs = stream.getvalue() + harness.stream.getvalue() + caplog.text
    for sentinel in (*sentinels, raw_text, preserved_text):
        assert sentinel not in metadata_and_logs


def test_s07_excluded_scope_keeps_actual_unsupported_tool_result(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
) -> None:
    case = cases["S07"]
    assert isinstance(case.request, RentRequest)
    expected = rent_increase_check(case.request.facts, corpus=corpus)
    harness = harness_for(
        case, corpus, [selection(case.request), final_message(case.canonical_evidence_ids)]
    )
    response = asyncio.run(harness.run(redact=redact_text))
    assert response.tool_result == expected
    assert expected.status == "unsupported"
    for name in (
        "notice_days",
        "deemed_served_on",
        "earliest_notice_on",
        "latest_deemed_service_on",
        "latest_dispatch_on",
        "earliest_spacing_on",
        "guideline_percent",
        "cap_cents_exact",
    ):
        assert getattr(expected, name) is None
    assert [(check.id, check.status) for check in expected.checks[3:]] == [
        (name, "not_applicable") for name in ("notice", "spacing", "guideline", "form")
    ]
    followup = harness.fake.creates[1]["messages"][-1]["content"][0]
    assert json.loads(followup["content"]) == expected.model_dump(mode="json")


def test_s07_coercing_excluded_scope_cannot_execute_calculator(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = cases["S07"]
    assert isinstance(case.request, RentRequest)
    reply = selection(case.request)
    block = cast(ToolUseBlock, reply.content[0])
    block.input = {
        **block.input,
        "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
    }
    calculator = Mock(side_effect=AssertionError("unexpected calculator execution"))
    monkeypatch.setattr(agent_module, "rent_increase_check", calculator)
    harness = harness_for(case, corpus, [reply])
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(redact=redact_text))
    assert caught.value.code == "tool_protocol_error"
    calculator.assert_not_called()
    assert len(harness.fake.creates) == 1


@pytest.mark.parametrize("extra", [None, "answer", "statements"])
def test_s08_refusal_text_is_owned_by_server_and_accepts_no_extra_prose(
    cases: dict[str, SecurityCase],
    corpus: Corpus,
    extra: str | None,
) -> None:
    case = cases["S08"]
    reply = final_message([], reason="out_of_scope")
    if extra:
        block = cast(TextBlock, reply.content[0])
        data = json.loads(block.text)
        data[extra] = (
            "SYNTHETIC ADVICE SENTINEL"
            if extra == "answer"
            else [
                {
                    "id": "s1",
                    "text": "SYNTHETIC ADVICE SENTINEL",
                    "citation_ids": case.canonical_evidence_ids,
                }
            ]
        )
        block.text = json.dumps(data)
    harness = harness_for(case, corpus, [reply])
    if extra:
        with pytest.raises(ProviderFailure) as caught:
            asyncio.run(harness.run(redact=redact_text))
        assert caught.value.code == "invalid_generated_output"
    else:
        response = asyncio.run(harness.run(redact=redact_text))
        assert response.answer == REFUSALS["out_of_scope"]
        assert response.status == "refused"
        assert response.statements == []
        assert response.disclaimer == disclaimer_for(corpus.snapshot_date)
    assert "SYNTHETIC ADVICE SENTINEL" not in harness.stream.getvalue()


@pytest.mark.parametrize("sidecar", [None, "x", "x" * 4000])
def test_retrieval_envelope_accepts_valid_boundaries(corpus: Corpus, sidecar: str | None) -> None:
    harness = Harness(
        request_for("question"), corpus, RecordingMessages([final_message([corpus.chunks[0].id])])
    )
    result = RetrievalContext(harness.hits, sidecar)
    with pytest.raises(FrozenInstanceError):
        cast(Any, result).untrusted_text = "mutation"
    asyncio.run(harness.run(retrieve=lambda _: result, redact=redact_text))
    payload = payload_of(harness.fake.creates[0])
    if sidecar is None:
        assert "untrusted_retrieved_text" not in payload
    else:
        assert payload["untrusted_retrieved_text"] == sidecar


@pytest.mark.parametrize(
    "invalid",
    [
        "empty",
        "long",
        "number",
        "list_hits",
        "duplicate",
        "six",
        "replaced",
        "invalid_chunk",
        "non_hit",
    ],
)
def test_invalid_envelopes_fail_safely_before_provider(corpus: Corpus, invalid: str) -> None:
    harness = Harness(request_for("question"), corpus, RecordingMessages([]))
    result = RetrievalContext(harness.hits, "SYNTHETIC RETRIEVED SENTINEL")
    if invalid in {"empty", "long", "number"}:
        value = {"empty": "", "long": "x" * 4001, "number": 123}[invalid]
        result = replace(result, untrusted_text=cast(Any, value))
    elif invalid == "list_hits":
        result = replace(result, hits=cast(Any, list(harness.hits)))
    elif invalid == "duplicate":
        result = replace(result, hits=harness.hits * 2)
    elif invalid == "six":
        result = replace(result, hits=tuple(SearchHit(chunk, -1.0) for chunk in corpus.chunks[:6]))
    elif invalid in {"replaced", "invalid_chunk"}:
        chunk = corpus.chunks[0].model_copy(
            update={"text": "SYNTHETIC CHUNK SENTINEL" if invalid == "replaced" else 123}
        )
        result = replace(result, hits=(SearchHit(chunk, -1.0),))
    else:
        result = replace(result, hits=cast(Any, ("SYNTHETIC NON-HIT SENTINEL",)))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(harness.run(retrieve=lambda _: result, redact=redact_text))
    assert caught.value.code == "provider_error"
    assert not harness.fake.counts and not harness.fake.creates
    assert harness.endpoint().retrieved_evidence_ids == ()
    assert "SENTINEL" not in str(caught.value) + harness.stream.getvalue()


def test_configuration_hashes_include_fixed_redaction_and_sidecar_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_extraction = extraction_config_hash()
    original_agent = agent_config_hash("question")
    provider_hash = configuration_hash(
        system=extract_module.EXTRACTION_SYSTEM,
        output_schema=transform_schema(Extraction.model_json_schema()),
    )
    material = {
        "version": 1,
        "provider_hash": provider_hash,
        "redaction_hash": REDACTION_POLICY_HASH,
    }
    assert (
        original_extraction
        == sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    redact_text("unit@example.invalid arbitrary runtime input")
    assert extraction_config_hash() == original_extraction
    assert agent_config_hash("question") == original_agent
    with monkeypatch.context() as changed:
        changed.setattr(extract_module, "REDACTION_POLICY_HASH", "f" * 64)
        changed.setattr(agent_module, "REDACTION_POLICY_HASH", "f" * 64)
        assert extraction_config_hash() != original_extraction
        assert agent_config_hash("question") != original_agent
    monkeypatch.setattr(agent_module, "_RETRIEVAL_SIDECAR_POLICY", {"version": "changed policy"})
    assert agent_config_hash("question") != original_agent
    assert extraction_config_hash() == original_extraction
