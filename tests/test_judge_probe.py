"""Fixed request dump parity and terminal-only output using recording fakes."""

import asyncio
import importlib.util
import json
import logging
import sys
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, cast, get_args
from uuid import UUID

import anthropic
import httpx2
import pytest
from anthropic.types import Message, MessageTokensCount, TextBlock
from anthropic.types import Usage as SDKUsage
from judge_calibration import calibration_input

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.judge import (
    RealJudge,
    build_judge_packet,
    build_smoke_input,
    judge_config_hash,
    judge_schema,
)
from rent_navigator.eval.runner import JudgeFailure, JudgeInput
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.provider import ProviderFailure, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


def load_probe() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/judge_probe.py"
    spec = importlib.util.spec_from_file_location("judge_probe_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def retained_request(tmp_path: Path) -> dict[str, Any]:
    from test_eval_judge import harness, message

    from rent_navigator.eval.collection import (
        REAL_ARTIFACT_FILES,
        RealCollectionManifest,
        RunEnvironment,
        StartedAttempt,
    )
    from rent_navigator.eval.models import ResultRow
    from rent_navigator.eval.runner import JudgeAccounting, PlanEntry
    from rent_navigator.models import AskResponse, Citation, Statement, disclaimer_for

    corpus = load_corpus()
    dataset = load_gold(Path("eval"), corpus)
    case = next(case for case in dataset.cases if case.id == "Q04")
    chunk = corpus.chunk(case.evidence_ids[0])
    attempt = UUID("10000000-0000-4000-8000-000000000071")
    response = AskResponse(
        attempt_id=attempt,
        trace_id=UUID("10000000-0000-4000-8000-000000000072"),
        status="answered",
        answer="Synthetic retained answer sentinel.",
        statements=[
            Statement(id="s1", text="Synthetic retained answer sentinel.", citation_ids=[chunk.id]),
            Statement(id="s2", text="Synthetic uncited claim.", citation_ids=[]),
        ],
        tool_result=None,
        citations=[
            Citation(
                id=chunk.id,
                url=corpus.sources[0].canonical_url,
                heading=chunk.heading,
                snapshot_date=corpus.snapshot_date,
            )
        ],
        snapshot_date=corpus.snapshot_date,
        disclaimer=disclaimer_for(corpus.snapshot_date),
    )
    value = JudgeInput(
        required_claims=tuple(case.required_claims),
        evidence=tuple(corpus.chunk(key) for key in sorted(set(case.evidence_ids))),
        expected_tool_result=None,
        response=response,
        actual_tool_result=None,
        cited_evidence=(chunk,),
        approved_facts=None,
    )
    wire = {
        "required_claims": {claim.id: {"result": "met"} for claim in value.required_claims},
        "statements": {
            item.id: {
                "factual": "supported",
                "citation_support": "supported" if item.citation_ids else "not_applicable",
            }
            for item in response.statements
        },
        "false_pass": False,
        "policy_violations": {f"S{number:02}": False for number in range(1, 9)},
    }
    test = harness(value)
    test.context = test.context.model_copy(update={"attempt_id": attempt})
    test.port.response = message(value, content=[{"type": "text", "text": json.dumps(wire)}])
    evaluated = test.run()
    raw = [record.model_dump(mode="json") for record in test.records()]
    selected = next(
        row for row in raw if row["operation"] == "generation" and row["event"] == "request"
    )
    selected["value"]["timeout"] = 179.125
    row = ResultRow(
        run_id=UUID(raw[0]["run_id"]),
        case_id="Q04",
        arm="baseline",
        repeat=0,
        source_sha=test.context.source_commit,
        config_hash="a" * 64,
        corpus_hash=corpus.corpus_hash,
        gold_hash=dataset.gold_hash,
        pricing_hash=PRICING_HASH,
        attempt_id=attempt,
        trace_ids=[response.trace_id],
        retrieved_ids=[],
        foundation_evidence_ids=[],
        initial_context_evidence_ids=[],
        response=response,
        actual_extract=None,
        actual_tool_args=None,
        actual_tool_result=None,
        deterministic_assertions=[],
        judge=None,
        classification="error",
        latency_ms=0.0,
        input_tokens=1000,
        output_tokens=100,
        serving_cost_usd=Decimal("0.0015"),
        judge_cost_usd=evaluated.cost.actual_cost_usd,
        usage_complete=True,
    )
    contents = {name: b"" for name in REAL_ARTIFACT_FILES}
    contents["judge-raw-provider.jsonl"] = b"".join(
        (json.dumps(item) + "\n").encode() for item in raw
    )
    contents["results.jsonl"] = (row.model_dump_json() + "\n").encode()
    manifest = RealCollectionManifest(
        schema_version=1,
        collection_complete=False,
        execution_mode="live",
        reportable=False,
        evaluation_complete=False,
        run_id=row.run_id,
        source_sha=row.source_sha,
        corpus_hash=corpus.corpus_hash,
        gold_hash=dataset.gold_hash,
        approval_hash=dataset.approval_hash,
        security_hash="b" * 64,
        pricing_hash=PRICING_HASH,
        lock_hash="b" * 64,
        protocol_hash="b" * 64,
        config_map={},
        config_hash="b" * 64,
        judge_config_hash=judge_config_hash(),
        environment=RunEnvironment(
            os="synthetic",
            cpu="synthetic",
            ram_bytes=None,
            docker_version=None,
            image_digest=None,
            source_clean=True,
            provider_access_date=None,
            pricing_url="https://example.invalid",
        ),
        files={name: sha256(data).hexdigest() for name, data in contents.items()},
        started_attempts=[
            StartedAttempt(warmup=False, entry=PlanEntry(case_id="Q04", arm="baseline", repeat=0))
        ],
        judge_evaluations={},
        judge_accounting={
            str(attempt): JudgeAccounting.model_validate_json(
                evaluated.model_dump_json(exclude={"judgment"})
            )
        },
        reasons=["synthetic retained failure"],
    )
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "manifest.json").write_text(manifest.model_dump_json())
    selectors = {
        "raw_sha256": sha256(contents["judge-raw-provider.jsonl"]).hexdigest(),
        "manifest_sha256": sha256((tmp_path / "manifest.json").read_bytes()).hexdigest(),
        "attempt_id": str(attempt),
        "trace_id": selected["trace_id"],
        "operation_index": 2,
    }
    return {
        "directory": tmp_path,
        "selectors": selectors,
        "payload": deepcopy(selected["value"]),
        "wire": wire,
        "value": value,
    }


@pytest.mark.parametrize("profile", ["current", "medium", "high-output"])
def test_retained_replay_profiles_preserve_exact_request_and_input(
    retained_request: dict[str, Any], profile: str
) -> None:
    probe = load_probe()
    before = {path.name: path.read_bytes() for path in retained_request["directory"].iterdir()}
    replay = probe.load_replay(retained_request["directory"], **retained_request["selectors"])
    assert build_judge_packet(replay.value) == build_judge_packet(retained_request["value"])
    assert replay.payload == retained_request["payload"]
    expected = deepcopy(replay.payload)
    if profile == "medium":
        expected["output_config"]["effort"] = "medium"
    elif profile == "high-output":
        expected["max_tokens"], expected["timeout"] = 16384, 300.0
    assert probe.diagnostic_payload(replay, profile) == expected
    assert replay.payload == retained_request["payload"]
    assert {
        path.name: path.read_bytes() for path in retained_request["directory"].iterdir()
    } == before


@pytest.mark.parametrize(
    "selector,value",
    [
        ("attempt_id", "bad"),
        ("trace_id", "bad"),
        ("operation_index", True),
        ("operation_index", 0),
        ("operation_index", 3),
        ("raw_sha256", "a" * 64),
        ("manifest_sha256", "a" * 64),
    ],
)
def test_replay_rejects_invalid_or_missing_selectors_without_client(
    retained_request: dict[str, Any], monkeypatch: pytest.MonkeyPatch, selector: str, value: Any
) -> None:
    probe = load_probe()

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid replay must stop before client setup")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", prohibited)
    selectors = {**retained_request["selectors"], selector: value}
    with pytest.raises(ValueError, match="^Invalid retained judge replay$"):
        probe.load_replay(retained_request["directory"], **selectors)


def replay_arguments(fixture: dict[str, Any], profile: str = "current") -> list[str]:
    args = ["--diagnose", "--replay-dir", str(fixture["directory"])]
    for name, value in fixture["selectors"].items():
        args.extend(["--" + name.replace("_", "-"), str(value)])
    return [*args, "--diagnostic-profile", profile]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_request",
        "duplicate_row",
        "wrong_case",
        "wrong_arm",
        "wrong_repeat",
        "run_id",
        "trace_accounting",
        "manifest_config",
        "missing_count",
        "invalid_count",
        "model",
        "thinking",
        "schema",
        "system",
        "extra_field",
        "timeout",
        "packet",
    ],
)
def test_replay_rejects_rehashed_mismatches_before_client_and_sanitizes_failure(
    retained_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutation: str,
) -> None:
    directory = retained_request["directory"]
    raw_path, rows_path = directory / "judge-raw-provider.jsonl", directory / "results.jsonl"
    raw = [json.loads(line) for line in raw_path.read_text().splitlines()]
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()]
    manifest = json.loads((directory / "manifest.json").read_text())
    request = next(
        row for row in raw if row["operation"] == "generation" and row["event"] == "request"
    )
    secret = "synthetic-private-malformed-request"
    payload = request["value"]
    if mutation == "duplicate_request":
        raw.append(deepcopy(request))
    elif mutation == "duplicate_row":
        rows.append(deepcopy(rows[0]))
    elif mutation in {"wrong_case", "wrong_arm", "wrong_repeat"}:
        name, value = {
            "wrong_case": ("case_id", "Q03"),
            "wrong_arm": ("arm", "production"),
            "wrong_repeat": ("repeat", 1),
        }[mutation]
        rows[0][name] = value
    elif mutation == "run_id":
        request["run_id"] = "10000000-0000-4000-8000-000000000099"
    elif mutation == "trace_accounting":
        accounting = manifest["judge_accounting"][retained_request["selectors"]["attempt_id"]]
        for record in accounting["records"]:
            record["trace_id"] = "10000000-0000-4000-8000-000000000099"
    elif mutation == "manifest_config":
        manifest["judge_config_hash"] = "b" * 64
    elif mutation == "missing_count":
        raw = [
            row
            for row in raw
            if not (row["operation"] == "count_tokens" and row["event"] == "response")
        ]
    elif mutation == "invalid_count":
        next(
            row for row in raw if row["operation"] == "count_tokens" and row["event"] == "response"
        )["value"]["input_tokens"] = True
    elif mutation == "model":
        payload["model"] = secret
    elif mutation == "thinking":
        payload["thinking"]["display"] = secret
    elif mutation == "schema":
        payload["output_config"]["format"]["schema"]["properties"].pop("false_pass")
    elif mutation == "system":
        payload["system"] += secret
    elif mutation == "extra_field":
        payload["private"] = secret
    elif mutation == "timeout":
        payload["timeout"] = 301.0
    else:
        packet = json.loads(payload["messages"][0]["content"])
        packet["statements"][0]["text"] = secret
        payload["messages"][0]["content"] = json.dumps(packet)
    raw_path.write_text("".join(json.dumps(row) + "\n" for row in raw))
    rows_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest["files"] = {
        name: sha256((directory / name).read_bytes()).hexdigest() for name in manifest["files"]
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    retained_request["selectors"]["raw_sha256"] = manifest["files"]["judge-raw-provider.jsonl"]
    retained_request["selectors"]["manifest_sha256"] = sha256(
        (directory / "manifest.json").read_bytes()
    ).hexdigest()

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid replay cannot instantiate a client")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", prohibited)
    with pytest.raises(SystemExit) as error:
        load_probe().main(replay_arguments(retained_request))
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and secret not in captured.err
    assert "Invalid retained judge diagnostic" in captured.err


@pytest.mark.parametrize(
    "args",
    [
        ["--diagnose"],
        ["--diagnose", "--dump"],
        ["--diagnose", "--send"],
        ["--dump", "--out", "x", "--diagnostic-profile", "medium"],
        ["--send", "--in", "x", "--replay-dir", "y"],
        ["--diagnose", "--diagnostic-profile", "unbounded"],
        ["--diagnose", "--max-tokens", "32768"],
    ],
)
def test_diagnostic_cli_rejects_missing_mixed_or_unbounded_options(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        load_probe().main(args)
    assert error.value.code == 2


def test_diagnostic_deadline_is_probe_local_and_cancels_one_fake_call(
    retained_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rent_navigator.model_policy import MODEL_POLICIES

    probe = load_probe()
    replay = probe.load_replay(retained_request["directory"], **retained_request["selectors"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    limits: list[float] = []
    calls: list[dict[str, Any]] = []
    cancelled: list[bool] = []
    original_timeout = asyncio.timeout

    def short_timeout(seconds: float) -> asyncio.Timeout:
        limits.append(seconds)
        return original_timeout(0.01)

    monkeypatch.setattr(probe.asyncio, "timeout", short_timeout)

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = self

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def create(self, **kwargs: Any) -> Message:
            calls.append(kwargs)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            raise AssertionError("Blocked diagnostic must time out")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    previous = logging.root.manager.disable
    try:
        result = asyncio.run(probe.diagnose_replay(replay, "high-output"))
    finally:
        logging.disable(previous)
    assert result.decode_result == "deadline_exceeded"
    assert limits == [300.0] and len(calls) == 1 and calls[0]["timeout"] == 300.0
    assert cancelled == [True]
    assert result.cost.actual_cost_usd is None and not result.cost.usage_complete
    assert result.cost.reserved_cost_usd == Decimal("0.21384")
    assert MODEL_POLICIES[JUDGE_MODEL].max_output_tokens == 16384
    assert MODEL_POLICIES[JUDGE_MODEL].reservation_usd == Decimal("0.21384")


def test_diagnostic_cli_prints_only_five_fields_with_no_artifact_write(
    retained_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = load_probe()
    from rent_navigator.trace import cost_for_usage

    before = {path.name: path.read_bytes() for path in retained_request["directory"].iterdir()}

    async def fake_diagnostic(replay: Any, profile: str) -> Any:
        assert replay.payload == retained_request["payload"] and profile == "medium"
        return probe.DiagnosticResult(
            "stop_reason", "max_tokens", 8192, None, 2.0, cost_for_usage(JUDGE_MODEL, None)
        )

    monkeypatch.setattr(probe, "diagnose_replay", fake_diagnostic)
    assert probe.main(replay_arguments(retained_request, "medium")) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "decode_result": "stop_reason",
        "stop_reason": "max_tokens",
        "output_tokens": 8192,
        "thinking_tokens": None,
        "elapsed_seconds": 2.0,
    }
    assert {
        path.name: path.read_bytes() for path in retained_request["directory"].iterdir()
    } == before


@pytest.mark.parametrize("profile", ["current", "medium", "high-output"])
@pytest.mark.parametrize(
    "outcome",
    [
        "decoded",
        "json_invalid",
        "stop_reason",
        "missing_usage",
        "sdk_error",
        "missing_breakdown",
        "malformed_input",
        "malformed_output",
        "usage_error",
        "coverage",
        "citation_applicability",
        "policy_ids",
        "model_mismatch",
        "model_invalid",
        "layout_invalid",
    ],
)
def test_diagnostic_one_fake_call_safe_output_full_decode_and_cost(
    retained_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    outcome: str,
) -> None:
    from anthropic.types import ThinkingBlock
    from anthropic.types.output_tokens_details import OutputTokensDetails

    from rent_navigator.eval import judge

    probe = load_probe()
    replay = probe.load_replay(retained_request["directory"], **retained_request["selectors"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-private-key")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    sentinel = "synthetic-private-response-and-reasoning"
    calls: list[dict[str, Any]] = []
    settings: list[dict[str, Any]] = []
    decodes: list[JudgeInput] = []
    original_parse = judge.parse_judgment

    def observed_parse(response: Message, value: JudgeInput) -> Any:
        decodes.append(value)
        return original_parse(response, value)

    monkeypatch.setattr(judge, "parse_judgment", observed_parse)
    if outcome == "usage_error":
        from rent_navigator import provider

        def invalid_usage(response: Message) -> Any:
            raise RuntimeError(sentinel)

        monkeypatch.setattr(provider, "_usage", invalid_usage)

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            settings.append(kwargs)
            self.messages = self

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def create(self, **kwargs: Any) -> Message:
            calls.append(deepcopy(kwargs))
            if outcome == "sdk_error":
                raise RuntimeError(sentinel)
            wire = deepcopy(retained_request["wire"])
            if outcome == "coverage":
                wire["statements"].pop("s2")
            elif outcome == "citation_applicability":
                wire["statements"]["s2"]["citation_support"] = "supported"
            elif outcome == "policy_ids":
                wire["policy_violations"].pop("S08")
            usage: Any = SDKUsage(
                input_tokens=1000,
                output_tokens=100,
                output_tokens_details=OutputTokensDetails(thinking_tokens=40),
            )
            if outcome == "missing_usage":
                usage = None
            elif outcome == "missing_breakdown":
                usage = SDKUsage(input_tokens=1000, output_tokens=100)
            elif outcome == "malformed_input":
                usage = usage.model_copy(update={"input_tokens": True})
            elif outcome == "malformed_output":
                usage = usage.model_copy(update={"output_tokens": sentinel})
            content = [
                ThinkingBlock(type="thinking", thinking=sentinel, signature=sentinel),
                TextBlock(
                    type="text", text=sentinel if outcome == "json_invalid" else json.dumps(wire)
                ),
            ]
            if outcome == "layout_invalid":
                content.pop()
            return Message.model_construct(
                id=sentinel,
                type="message",
                role=cast(Any, "user" if outcome == "model_invalid" else "assistant"),
                model=sentinel if outcome == "model_mismatch" else JUDGE_MODEL,
                content=cast(Any, content),
                stop_reason="max_tokens" if outcome == "stop_reason" else "end_turn",
                stop_sequence=None,
                usage=usage,
            )

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    times = iter([0.0, 1.25])
    previous_logging = logging.root.manager.disable
    try:
        result = asyncio.run(probe.diagnose_replay(replay, profile, clock=lambda: next(times)))
    finally:
        logging.disable(previous_logging)
    unknown_usage = outcome in {
        "missing_usage",
        "sdk_error",
        "malformed_input",
        "malformed_output",
        "usage_error",
    }
    expected = (
        "provider_error"
        if unknown_usage
        else "decoded"
        if outcome == "missing_breakdown"
        else outcome
    )
    assert result.decode_result == expected
    assert len(calls) == len(settings) == 1
    assert calls[0] == probe.diagnostic_payload(replay, profile)
    assert settings[0] == {
        "api_key": "synthetic-private-key",
        "base_url": "https://api.anthropic.com",
        "max_retries": 0,
        "timeout": calls[0]["timeout"],
    }
    assert decodes == ([] if outcome == "sdk_error" else [replay.value])
    assert result.elapsed_seconds == 1.25
    assert result.cost.actual_cost_usd == (None if unknown_usage else Decimal("0.003"))
    assert result.cost.reserved_cost_usd == Decimal(
        "0.21384" if profile == "high-output" else "0.21384"
    )
    assert result.cost.usage_complete is (not unknown_usage)
    output = json.dumps(result.terminal_fields())
    assert set(result.terminal_fields()) == {
        "decode_result",
        "stop_reason",
        "output_tokens",
        "thinking_tokens",
        "elapsed_seconds",
    }
    assert result.output_tokens == (
        None if outcome in {"missing_usage", "sdk_error", "malformed_output"} else 100
    )
    assert result.thinking_tokens == (
        None
        if outcome in {"missing_usage", "sdk_error", "missing_breakdown", "malformed_output"}
        else 40
    )
    assert sentinel not in output and "synthetic-private-key" not in output
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("packet_id", [*(f"J{n:02}" for n in range(1, 14)), "R04"])
def test_dump_matches_real_judge_recording_fake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    packet_id: str,
) -> None:
    probe = load_probe()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def forbidden_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Offline dump must never construct a client")

    monkeypatch.setattr(anthropic, "Anthropic", forbidden_client)
    out = tmp_path / "request.json"
    assert probe.main(["--dump", "--out", str(out), "--packet-id", packet_id]) == 0
    assert capsys.readouterr() == ("", "")
    dumped = json.loads(out.read_bytes())
    corpus = load_corpus()
    value = (
        build_smoke_input(
            next(case for case in load_gold(Path("eval"), corpus).cases if case.id == "R04"), corpus
        )
        if packet_id == "R04"
        else calibration_input(probe.fixed_packet(packet_id), corpus)
    )
    packet = build_judge_packet(value)
    assert fingerprint(dumped["output_config"]["format"]["schema"]) == fingerprint(
        judge_schema(packet)
    )
    captured: list[dict[str, Any]] = []

    class RecordingMessages:
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            return MessageTokensCount(input_tokens=100)

        async def create(self, **kwargs: Any) -> Message:
            captured.append(kwargs)
            raise ProviderFailure("provider_error")

    context = TraceContext(
        attempt_id=UUID("10000000-0000-4000-8000-000000000001"),
        trace_id=UUID("10000000-0000-4000-8000-000000000002"),
        phase="judge",
        source_commit="a" * 40,
        config_hash=judge_config_hash(),
        corpus_hash=load_corpus().corpus_hash,
        pricing_hash=PRICING_HASH,
    )
    judge = RealJudge(
        RecordingMessages(),
        budget=SpendLedger(Decimal("0.30")),
        metadata=StringIO(),
        raw_provider=StringIO(),
        run_id=UUID("10000000-0000-4000-8000-000000000003"),
    )
    with pytest.raises(JudgeFailure):
        asyncio.run(judge.evaluate(value, context=context))
    assert len(captured) == 1
    actual = {key: val for key, val in captured[0].items() if key != "timeout"}
    assert fingerprint(actual) == fingerprint(dumped)
    assert actual["model"] == JUDGE_MODEL


def test_dump_preserves_existing_file(tmp_path: Path) -> None:
    probe = load_probe()
    out = tmp_path / "existing.json"
    out.write_bytes(b"synthetic-existing-content")
    with pytest.raises(FileExistsError):
        probe.dump_payload(out)
    assert out.read_bytes() == b"synthetic-existing-content"


@pytest.mark.parametrize(
    "args", [[], ["--dump"], ["--send"], ["--dump", "--send"], ["--dump", "--in", "x"]]
)
def test_cli_rejects_incomplete_or_mixed_modes(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        load_probe().main(args)
    assert error.value.code == 2


def test_send_rejects_modified_payload_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = load_probe()
    path = tmp_path / "modified.json"
    payload = probe.build_payload()
    payload["max_tokens"] += 1
    path.write_text(json.dumps(payload))

    def forbidden_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Modified payload must never construct a client")

    monkeypatch.setattr(anthropic, "Anthropic", forbidden_client)
    with pytest.raises(ValueError, match="does not match"):
        probe.send_payload(path)


@pytest.mark.parametrize("outcome", ["error", "success_with_id", "success_without_id"])
@pytest.mark.parametrize("packet_id", ["J01", "J13", "R04"])
def test_send_uses_one_recording_call_and_only_permitted_terminal_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    packet_id: str,
) -> None:
    fails = outcome == "error"
    probe = load_probe()
    path = tmp_path / "request.json"
    probe.dump_payload(path, packet_id)
    original = path.read_bytes()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-probe-key")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    calls: list[dict[str, Any]] = []
    settings: list[dict[str, Any]] = []
    message = "Synthetic unsupported schema feature.\nFull second line."
    body = {
        "error": {"type": "invalid_request_error", "message": message},
        "private": "synthetic-body-must-not-be-printed",
    }
    error = anthropic.BadRequestError(
        "synthetic-exception-string-must-not-be-printed",
        response=httpx2.Response(
            400,
            request=httpx2.Request("POST", "https://example.invalid"),
            headers={"request-id": "req_synthetic", "private": "synthetic-header-secret"},
        ),
        body=body,
    )

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            settings.append(kwargs)
            self.messages = self

        def __enter__(self) -> "RecordingClient":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def create(self, **kwargs: Any) -> Message:
            calls.append(kwargs)
            if fails:
                raise error
            response = Message.model_construct(
                id="msg_synthetic",
                type="message",
                role=get_args(Message.model_fields["role"].annotation)[0],
                model=JUDGE_MODEL,
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps(
                            {
                                "required_claims": {
                                    row["id"]: {"result": "met"}
                                    for row in probe.fixed_packet(packet_id)["required_claims"]
                                },
                                "statements": {
                                    row["id"]: {
                                        "factual": "supported",
                                        "citation_support": "supported"
                                        if row["citation_ids"]
                                        else "not_applicable",
                                    }
                                    for row in probe.fixed_packet(packet_id)["statements"]
                                },
                                "false_pass": False,
                                "policy_violations": {f"S{i:02}": False for i in range(1, 9)},
                            }
                        ),
                    )
                ],
                stop_reason="end_turn",
                stop_sequence=None,
                usage=SDKUsage(input_tokens=100, output_tokens=10),
            )
            if outcome == "success_with_id":
                response._request_id = "req_synthetic_success"
            return response

    monkeypatch.setattr(anthropic, "Anthropic", RecordingClient)
    previous_logging = logging.root.manager.disable
    try:
        assert probe.main(["--send", "--in", str(path), "--packet-id", packet_id]) == int(fails)
    finally:
        logging.disable(previous_logging)
    captured = capsys.readouterr()
    assert captured.err == ""
    output = json.loads(captured.out)
    assert len(calls) == len(settings) == 1
    assert settings[0]["max_retries"] == 0
    assert settings[0]["timeout"] == 180.0
    assert fingerprint(calls[0]) == fingerprint(json.loads(original))
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["request.json"]
    for sentinel in (
        "synthetic-probe-key",
        "synthetic-body-must-not-be-printed",
        "synthetic-header-secret",
        "synthetic-exception-string-must-not-be-printed",
    ):
        assert sentinel not in captured.out
    if fails:
        assert output == {
            "http_status": 400,
            "sdk_exception": "BadRequestError",
            "error_type": "invalid_request_error",
            "error_message": message,
            "request_id": "req_synthetic",
        }
    else:
        assert set(output) == {
            "http_status",
            "stop_reason",
            "usage",
            "decode_result",
            "request_id",
        }
        assert output["http_status"] == 200
        assert output["stop_reason"] == "end_turn"
        assert output["usage"]["input_tokens"] == 100
        assert output["usage"]["output_tokens"] == 10
        assert output["decode_result"] == "decoded"
        assert output["request_id"] == (
            "req_synthetic_success" if outcome == "success_with_id" else None
        )
