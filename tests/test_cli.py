"""Restricted development-entry checks using synthetic clients and no network."""

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from anthropic.types import Message, MessageTokensCount, TextBlock
from pydantic import ValidationError

import rent_navigator.cli as cli
from rent_navigator.agent import TOOL_STATUS_TEXT, agent_config_hash
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.index import inspect_index
from rent_navigator.models import AskResponse
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import ACTOR_MODEL, PRICING_HASH, TraceRecord, provider_cost_totals

SOURCE_COMMIT = "a" * 40
_KEY_SENTINEL = "synthetic-test-key-never-persist"
_ERROR_SENTINEL = "synthetic-private-sdk-exception-never-persist"


def _reject_client(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Offline entry attempted to construct a real client")


def _records(directory: Path) -> list[TraceRecord]:
    return [
        TraceRecord.model_validate_json(line)
        for line in (directory / "metadata.jsonl").read_text().splitlines()
    ]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_offline_entry_records_one_native_roundtrip_and_matching_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "create_client", _reject_client)
    evidence = tmp_path / "offline"
    report = asyncio.run(cli.run_smoke(source_commit=SOURCE_COMMIT, evidence_dir=evidence))
    corpus = load_corpus()
    assert report["mode"] == "offline_synthetic"
    assert report["mechanical_acceptance"] == "PASS"
    assert report["explanation_review"] == "NOT APPLICABLE: synthetic response"
    assert report["real_generation_attempts"] == 0
    assert report["generation_attempts"] == report["count_attempts"] == 2
    assert report["preflight_estimates"] == [100, 100]
    assert report["source_commit"] == SOURCE_COMMIT
    assert report["config_hash"] == agent_config_hash("rent", "production")
    assert report["corpus_hash"] == corpus.corpus_hash
    assert report["pricing_hash"] == PRICING_HASH
    assert report["budget_reservation_limit_usd"] == "0.038"
    assert report["reforecast_required"] is False
    assert Decimal(report["batch_committed_usd"]) == Decimal("0.0004")
    response = AskResponse.model_validate_json(json.dumps(report["response"]))
    assert response.status == "answered"
    assert response.tool_result is not None
    assert response.tool_result.status == "fails_checked_rules"
    assert response.tool_result.notice_days == 60
    assert response.tool_result.cap_cents_exact == "204200"
    assert {check.id for check in response.tool_result.checks if check.status == "fail"} == {
        "notice",
        "guideline",
    }
    assert report["status_explanation"] == TOOL_STATUS_TEXT["fails_checked_rules"]
    assert all(citation == corpus.citation(citation.id) for citation in response.citations)
    assert json.loads((evidence / "report.json").read_text()) == report
    assert inspect_index(evidence / "index.sqlite3").corpus_hash == corpus.corpus_hash
    records = _records(evidence)
    assert len(records) == 5
    assert sum(record.record_kind == "endpoint" for record in records) == 1
    assert records[-1].response_code == "answered"
    assert records[-1].tool_name == "rent_increase_check"
    assert provider_cost_totals(records).model_dump(mode="json") == report["token_accounting"]
    assert report["token_accounting"]["input_tokens"] == 200
    assert report["token_accounting"]["output_tokens"] == 40
    assert report["token_accounting"]["actual_cost_usd"] == "0.0004"
    assert report["token_accounting"]["reserved_cost_usd"] == "0.038"
    assert all(record.source_commit == SOURCE_COMMIT for record in records)
    assert "current_cents" not in (evidence / "metadata.jsonl").read_text()
    requests = _jsonl(evidence / "synthetic_requests.jsonl")
    returns = _jsonl(evidence / "synthetic_returns.jsonl")
    assert len(requests) == len(returns) == 2
    assert [item["call"] for item in requests] == [1, 2]
    assert all(item["request"]["model"] == ACTOR_MODEL for item in requests)
    assert requests[0]["request"]["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }
    assert requests[1]["request"]["tool_choice"] == {"type": "none"}
    native = returns[0]["response"]["content"][0]
    history = requests[1]["request"]["messages"]
    assert history[1]["content"][0]["id"] == native["id"]
    assert history[2]["content"][0]["tool_use_id"] == native["id"]
    assert history[2]["content"][0]["type"] == "tool_result"
    assert json.loads(history[2]["content"][0]["content"]) == response.tool_result.model_dump(
        mode="json"
    )
    assert all("timeout" not in item["request"] for item in requests)
    preflight = _jsonl(evidence / "synthetic_preflight_requests.jsonl")
    assert len(preflight) == 2
    for counted, generated in zip(preflight, requests, strict=True):
        assert all(generated["request"][key] == value for key, value in counted["request"].items())
        assert "max_tokens" not in counted["request"]
        assert "extra_body" not in counted["request"]


def test_synthetic_request_validation_restricts_the_anonymous_context() -> None:
    first, second = cli.synthetic_request(), cli.synthetic_request()
    assert first.attempt_id != second.attempt_id
    assert first.facts == second.facts
    assert cli._validate_synthetic_request(first) == first
    changed = first.model_copy(
        update={"facts": first.facts.model_copy(update={"current_cents": 200001})}
    )
    with pytest.raises(ValueError, match="preset synthetic request"):
        cli._validate_synthetic_request(changed)


@pytest.mark.parametrize("mutation", ["changed_fact", "private_value", "question"])
def test_request_allowlist_prevents_capture_independently_of_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    request = cli.synthetic_request()
    sentinel = "SYNTHETIC PRIVATE REQUEST SENTINEL"
    invalid: object
    if mutation == "question":
        from rent_navigator.models import QuestionRequest

        invalid = QuestionRequest(mode="question", attempt_id=request.attempt_id, question=sentinel)
    else:
        value = 200001 if mutation == "changed_fact" else sentinel
        invalid = request.model_copy(
            update={"facts": request.facts.model_copy(update={"current_cents": value})}
        )
    monkeypatch.setattr(cli, "synthetic_request", lambda: invalid)
    monkeypatch.setattr(cli, "create_client", _reject_client)
    monkeypatch.setattr(cli, "redact_text", lambda text: text)
    evidence = tmp_path / mutation
    report = asyncio.run(cli.run_smoke(source_commit="a" * 40, evidence_dir=evidence))
    assert report["mechanical_acceptance"] == "INCOMPLETE"
    assert report["error"] == "provider_error"
    assert report["count_attempts"] == report["generation_attempts"] == 0
    assert "synthetic_request" not in report
    assert not (evidence / "metadata.jsonl").exists()
    assert not (evidence / "requests.jsonl").exists()
    assert not (evidence / "responses.jsonl").exists()
    assert sentinel not in (evidence / "report.json").read_text()


def test_entry_refuses_overwrite_without_modifying_prior_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "existing"
    evidence.mkdir()
    marker = evidence / "report.json"
    marker.write_text("preserved previous report\n")
    with pytest.raises(FileExistsError):
        asyncio.run(cli.run_smoke(source_commit=SOURCE_COMMIT, evidence_dir=evidence))
    assert marker.read_text() == "preserved previous report\n"
    assert list(evidence.iterdir()) == [marker]


@pytest.mark.parametrize("source", ["unknown", "a" * 39, "A" * 40, "a" * 41, ""])
def test_entry_requires_a_full_canonical_source_commit(tmp_path: Path, source: str) -> None:
    evidence = tmp_path / "invalid-source"
    with pytest.raises(ValidationError):
        asyncio.run(cli.run_smoke(source_commit=source, evidence_dir=evidence))
    assert not evidence.exists()


def test_entry_requires_explicit_billing_before_creating_artifacts_or_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "create_client", _reject_client)
    evidence = tmp_path / "live-without-billing"
    with pytest.raises(ProviderFailure) as error:
        asyncio.run(cli.run_smoke(source_commit=SOURCE_COMMIT, evidence_dir=evidence, live=True))
    assert error.value.code == "budget_exhausted"
    assert not evidence.exists()


@pytest.mark.parametrize(
    "mode_args", [[], ["--offline", "--live"], ["--live"], ["--offline", "--question", "private"]]
)
def test_cli_requires_exclusive_explicit_modes_and_rejects_free_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode_args: list[str]
) -> None:
    evidence = tmp_path / "rejected"
    monkeypatch.setattr(
        sys,
        "argv",
        ["rent-cli", *mode_args, "--source-commit", SOURCE_COMMIT, "--evidence-dir", str(evidence)],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not evidence.exists()


def test_cli_offline_main_prints_report_with_no_live_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "create_client", _reject_client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rent-cli",
            "--offline",
            "--source-commit",
            SOURCE_COMMIT,
            "--evidence-dir",
            str(tmp_path / "main"),
        ],
    )
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["mechanical_acceptance"] == "PASS"
    assert report["real_generation_attempts"] == 0


@pytest.mark.parametrize(
    ("outcome", "generation_count", "count_count", "return_count", "error_code"),
    [
        ("success", 2, 2, 2, None),
        ("first_missing_usage", 1, 1, 1, "provider_error"),
        ("second_missing_usage", 2, 2, 2, "provider_error"),
        ("second_count_overflow", 1, 2, 1, "budget_exhausted"),
        ("second_create_error", 2, 2, 1, "provider_error"),
        ("first_count_error", 0, 1, 0, "provider_error"),
        ("mismatch", 2, 2, 2, "invalid_generated_output"),
        ("close_error", 2, 2, 2, "provider_error"),
    ],
)
def test_fake_live_flow_stops_preserves_accounting_and_never_queries_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    generation_count: int,
    count_count: int,
    return_count: int,
    error_code: str | None,
) -> None:
    class FakeMessages(cli._OfflineMessages):
        def __init__(self) -> None:
            super().__init__(load_corpus())
            self.count_calls = 0

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            self.count_calls += 1
            if outcome == "first_count_error":
                raise RuntimeError(_ERROR_SENTINEL)
            return MessageTokensCount(
                input_tokens=15001
                if outcome == "second_count_overflow" and self.count_calls == 2
                else 100
            )

        async def create(self, **kwargs: Any) -> Message:
            response = await super().create(**kwargs)
            if (outcome == "first_missing_usage" and self.calls == 1) or (
                outcome == "second_missing_usage" and self.calls == 2
            ):
                return response.model_copy(update={"usage": None})
            if outcome == "second_create_error" and self.calls == 2:
                raise RuntimeError(_ERROR_SENTINEL)
            if outcome == "mismatch" and self.calls == 2:
                return response.model_copy(
                    update={
                        "content": [
                            TextBlock(
                                type="text",
                                text=json.dumps(
                                    {
                                        "kind": "refusal",
                                        "refusal_reason": "insufficient_evidence",
                                        "statements": [],
                                    }
                                ),
                            )
                        ]
                    }
                )
            return response

    class FakeClient:
        def __init__(self) -> None:
            self.messages = FakeMessages()
            self.closed = False

        @property
        def models(self) -> NoReturn:
            raise AssertionError("This batch must not query Models")

        async def close(self) -> None:
            self.closed = True
            if outcome == "close_error":
                raise RuntimeError(_ERROR_SENTINEL)

    client = FakeClient()
    constructed: list[bool] = []

    def fake_client(*, api_key: str) -> FakeClient:
        assert api_key == _KEY_SENTINEL
        constructed.append(True)
        return client

    monkeypatch.setattr(cli, "os", SimpleNamespace(environ={"ANTHROPIC_API_KEY": _KEY_SENTINEL}))
    monkeypatch.setattr(cli, "create_client", fake_client)
    evidence = tmp_path / outcome
    report = asyncio.run(
        cli.run_smoke(
            source_commit=SOURCE_COMMIT, evidence_dir=evidence, live=True, billing_ready=True
        )
    )
    assert constructed == [True]
    assert client.closed
    assert client.messages.calls == generation_count <= 2
    assert client.messages.count_calls == count_count <= 2
    assert report["generation_attempts"] == report["real_generation_attempts"] == generation_count
    assert report["count_attempts"] == count_count
    assert report["mode"] == "live_development"
    assert report["explanation_review"] == "PENDING"
    assert report["mechanical_acceptance"] == ("PASS" if outcome == "success" else "INCOMPLETE")
    assert report.get("error") == error_code
    records = _records(evidence)
    assert len([record for record in records if record.record_kind == "endpoint"]) == 1
    assert provider_cost_totals(records).model_dump(mode="json") == report["token_accounting"]
    assert len(_jsonl(evidence / "synthetic_requests.jsonl")) == generation_count
    assert len(_jsonl(evidence / "synthetic_returns.jsonl")) == return_count
    counted_requests = _jsonl(evidence / "synthetic_preflight_requests.jsonl")
    assert len(counted_requests) == count_count
    assert json.loads((evidence / "report.json").read_text()) == report
    artifacts = "\n".join(
        path.read_text() for path in evidence.iterdir() if path.suffix in (".json", ".jsonl")
    )
    assert _KEY_SENTINEL not in artifacts
    assert _ERROR_SENTINEL not in artifacts
    assert "ANTHROPIC_API_KEY" not in artifacts
    if outcome in ("first_missing_usage", "second_missing_usage", "second_create_error"):
        assert report["token_accounting"]["actual_cost_usd"] is None
        assert report["token_accounting"]["usage_complete"] is False
        assert report["reforecast_required"] is True
        assert Decimal(report["batch_committed_usd"]) == (
            Decimal("0.019") if generation_count == 1 else Decimal("0.0192")
        )
    else:
        assert report["token_accounting"]["usage_complete"] is True
        assert report["reforecast_required"] is False
        assert Decimal(report["batch_committed_usd"]) == Decimal("0.0002") * generation_count
    if generation_count >= 2 or outcome == "second_count_overflow":
        assert records[-1].tool_name == "rent_increase_check"
        assert len(records[-1].check_statuses) == 7
    if outcome == "second_count_overflow":
        assert report["preflight_estimates"] == [100, 15001]
        assert report["token_accounting"]["reserved_cost_usd"] == "0.019"
        second_request = counted_requests[1]["request"]
        assert second_request["tool_choice"] == {"type": "none"}
        assert second_request["output_config"]["format"]["type"] == "json_schema"
        assert len(second_request["tools"]) == 2
        messages = second_request["messages"]
        initial_evidence = json.loads(messages[0]["content"])["evidence"]
        actual_result = json.loads(messages[2]["content"][0]["content"])
        assert messages[2]["content"][0]["type"] == "tool_result"
        assert json.loads(messages[2]["content"][1]["text"]) == {
            "money_display_cad": {
                "current_rent_cad": "2000.00",
                "proposed_rent_cad": "2048.00",
                "exact_new_rent_ceiling_cad": "2042.00",
            }
        }
        added_evidence = json.loads(messages[2]["content"][2]["text"])["evidence"]
        corpus = load_corpus()
        expected_ids = {item["id"] for item in initial_evidence} | {
            identifier
            for rule_id in actual_result["rule_ids"]
            for identifier in corpus.rule(rule_id).evidence_ids
        }
        passages = initial_evidence + added_evidence
        assert {item["id"] for item in passages} == expected_ids
        assert len(passages) == len(expected_ids)
        assert all(item["text"] == corpus.chunk(item["id"]).text for item in passages)
        assert messages[2]["content"][0]["tool_use_id"] == messages[1]["content"][0]["id"]
    if outcome == "mismatch":
        assert report["response"]["status"] == "refused"
        assert report["response"]["tool_result"]["status"] == "fails_checked_rules"


def test_recording_boundary_caps_dispatch_and_excludes_transport_secrets(tmp_path: Path) -> None:
    class CountingMessages(cli._OfflineMessages):
        def __init__(self) -> None:
            super().__init__(load_corpus())
            self.count_calls = 0

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            self.count_calls += 1
            return await super().count_tokens(**kwargs)

    delegate = CountingMessages()
    recording = cli._RecordingMessages(delegate, tmp_path)

    async def exercise() -> None:
        for _ in range(2):
            await recording.count_tokens(
                model=ACTOR_MODEL,
                system="synthetic system",
                messages=[],
                api_key=_KEY_SENTINEL,
                headers={"x-private": _KEY_SENTINEL},
                timeout=1.0,
            )
            await recording.create(
                model=ACTOR_MODEL,
                system="synthetic system",
                messages=[],
                api_key=_KEY_SENTINEL,
                headers={"x-private": _KEY_SENTINEL},
                timeout=1.0,
            )
        with pytest.raises(ProviderFailure) as count_error:
            await recording.count_tokens(model=ACTOR_MODEL)
        with pytest.raises(ProviderFailure) as create_error:
            await recording.create(model=ACTOR_MODEL)
        assert count_error.value.code == create_error.value.code == "budget_exhausted"

    asyncio.run(exercise())
    assert delegate.count_calls == delegate.calls == 2
    assert recording.count_attempts == recording.generation_attempts == 2
    for filename in ("synthetic_preflight_requests.jsonl", "synthetic_requests.jsonl"):
        records = _jsonl(tmp_path / filename)
        assert len(records) == 2
        assert all(set(row["request"]) == {"model", "system", "messages"} for row in records)
    assert _KEY_SENTINEL not in "\n".join(path.read_text() for path in tmp_path.iterdir())


def test_offline_recorded_12289_second_count_preserves_complete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay only the retained token estimates through a synthetic provider."""
    corpus = load_corpus()

    class RecordedEstimateMessages(cli._OfflineMessages):
        def __init__(self, corpus: Corpus) -> None:
            super().__init__(corpus)
            self.count_number = 0

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            estimate = (3414, 12289)[self.count_number]
            self.count_number += 1
            return MessageTokensCount(input_tokens=estimate)

    monkeypatch.setattr(cli, "create_client", _reject_client)
    monkeypatch.setattr(cli, "_OfflineMessages", RecordedEstimateMessages)
    evidence = tmp_path / "recorded-estimate-offline"
    report = asyncio.run(cli.run_smoke(source_commit=SOURCE_COMMIT, evidence_dir=evidence))
    assert report["mode"] == "offline_synthetic"
    assert report["mechanical_acceptance"] == "PASS"
    assert report["preflight_estimates"] == [3414, 12289]
    assert report["generation_attempts"] == report["count_attempts"] == 2
    assert report["real_generation_attempts"] == 0
    assert report["budget_reservation_limit_usd"] == "0.038"
    assert report["token_accounting"]["reserved_cost_usd"] == "0.038"
    # Usage is still the fake's billed usage, not the historical request's measurement.
    assert report["token_accounting"]["actual_cost_usd"] == "0.0004"
    assert not report["reforecast_required"]
    counted = _jsonl(evidence / "synthetic_preflight_requests.jsonl")[1]["request"]
    generated = _jsonl(evidence / "synthetic_requests.jsonl")[1]["request"]
    assert counted["messages"] == generated["messages"]
    assert counted["tools"] == generated["tools"]
    assert counted["output_config"] == generated["output_config"]
    messages = counted["messages"]
    top_five = json.loads(messages[0]["content"])["evidence"]
    result = json.loads(messages[2]["content"][0]["content"])
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert json.loads(messages[2]["content"][1]["text"]) == {
        "money_display_cad": {
            "current_rent_cad": "2000.00",
            "proposed_rent_cad": "2048.00",
            "exact_new_rent_ceiling_cad": "2042.00",
        }
    }
    added = json.loads(messages[2]["content"][2]["text"])["evidence"]
    required_ids = {item["id"] for item in top_five} | {
        identifier for rule in result["rule_ids"] for identifier in corpus.rule(rule).evidence_ids
    }
    passages = top_five + added
    assert len(top_five) == 5
    assert len(passages) == len(required_ids) == 20
    assert {item["id"] for item in passages} == required_ids
    assert all(item["text"] == corpus.chunk(item["id"]).text for item in passages)
    endpoint = _records(evidence)[-1]
    assert endpoint.tool_name == "rent_increase_check"
    assert endpoint.retrieved_evidence_ids == tuple(item["id"] for item in top_five)
