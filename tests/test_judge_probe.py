"""Fixed request dump parity and terminal-only output using recording fakes."""

import asyncio
import importlib.util
import json
import logging
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, get_args
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
from rent_navigator.eval.runner import JudgeFailure
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.provider import ProviderFailure, SpendLedger
from rent_navigator.trace import PRICING_HASH, TraceContext


def load_probe() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/judge_probe.py"
    spec = importlib.util.spec_from_file_location("judge_probe_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
        budget=SpendLedger(Decimal("0.15")),
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
                content=[TextBlock(type="text", text="x" * 240)],
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
        assert set(output) == {"http_status", "stop_reason", "usage", "content", "request_id"}
        assert output["http_status"] == 200
        assert output["stop_reason"] == "end_turn"
        assert output["usage"]["input_tokens"] == 100
        assert output["usage"]["output_tokens"] == 10
        assert output["content"] == "x" * 200
        assert output["request_id"] == (
            "req_synthetic_success" if outcome == "success_with_id" else None
        )
