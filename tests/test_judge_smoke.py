"""Restricted smoke admission, fourteen fake votes, costs and R14 privacy."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message, MessageTokensCount

SOURCE = "a" * 40


def load_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/judge_smoke.py"
    spec = importlib.util.spec_from_file_location("judge_smoke_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "head_sha", lambda: SOURCE)
    return module


def test_offline_cli_completes_all_fourteen_votes_without_a_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Offline smoke must never create a client")

    monkeypatch.setattr(smoke, "create_client", forbidden)
    out = tmp_path / "evidence"
    assert smoke.main(["--offline", "--source-commit", SOURCE, "--evidence-dir", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["execution_mode"] == "synthetic"
    assert summary["real_generation_attempts"] == 0
    assert summary["generation_attempts"] == 14
    assert summary["calibration_matched"] == summary["calibration_denominator"] == 13
    assert summary["r04_smoke_passed"] is True
    assert summary["complete"] is True and summary["reportable"] is False
    assert summary["budget_limit_usd"] == "2.00"
    assert summary["reservation_forecast_usd"] == "1.84688"
    assert summary["cost"]["actual_cost_usd"] == "0.042"
    assert summary["cost"]["reserved_cost_usd"] == "1.84688"
    assert summary["batch_committed_usd"] == "0.042"
    assert sorted(p.name for p in out.iterdir()) == [
        *(f"J{n:02}.json" for n in range(1, 14)),
        "R04.json",
        "summary.json",
    ]
    for path in out.glob("*.json"):
        text = path.read_text()
        assert "system" not in json.loads(text)
        assert "No citation supplied" not in text
        assert summary["judge_config_hash"] == json.loads(text)["judge_config_hash"]
    row = json.loads((out / "J01.json").read_text())
    assert row["agreement"]["exact"] is True
    assert all(row["agreement"]["claims"].values())
    assert all(all(fields.values()) for fields in row["agreement"]["statements"].values())
    assert row["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    assert row["elapsed_seconds"] >= 0
    assert row["stop_reason"] == "end_turn"
    assert row["failure_diagnostic"] is None


@pytest.mark.parametrize("failure", ["budget", "http", "missing_usage", "invalid_output"])
def test_failure_records_preserve_accounting_and_privacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    smoke = load_smoke(monkeypatch)
    calls: list[str] = []
    sentinel = "synthetic-private-message-never-retained"
    sdk_error = anthropic.BadRequestError(
        sentinel,
        response=httpx2.Response(
            400,
            request=httpx2.Request("POST", "https://example.invalid"),
            headers={"request-id": "req_synthetic_failure", "private": sentinel},
        ),
        body={"error": {"type": "invalid_request_error", "message": sentinel}, "private": sentinel},
    )
    index = 0

    def factory(value: Any, expected: Any) -> Any:
        nonlocal index
        first = index == 0
        index += 1
        good = smoke.OfflineMessages(value, expected)

        class Port:
            async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
                calls.append("count")
                return MessageTokensCount(
                    input_tokens=24001 if failure == "budget" and first else 1000
                )

            async def create(self, **kwargs: Any) -> Message:
                calls.append("create")
                if first and failure == "http":
                    raise sdk_error
                response: Message = await good.create(**kwargs)
                if first and failure == "missing_usage":
                    return response.model_copy(update={"usage": None})
                if first and failure == "invalid_output":
                    return response.model_copy(
                        update={"content": [smoke.TextBlock(type="text", text=sentinel)]}
                    )
                return response

        return Port()

    out = tmp_path / failure
    summary = asyncio.run(
        smoke.run_smoke(source_commit=SOURCE, evidence_dir=out, messages_factory=factory)
    )
    row = json.loads((out / "J01.json").read_text())
    assert summary["complete"] is False and summary["real_generation_attempts"] == 0
    assert row["decoded_judgment"] is None
    for path in out.iterdir():
        text = path.read_text()
        assert sentinel not in text
        assert '"headers"' not in text and '"error_message"' not in text
        assert '"body"' not in text and '"packet"' not in text
    if failure == "budget":
        assert calls == ["count"]
        assert row["failure_code"] == "budget_exhausted"
        assert row["actual_cost_usd"] == row["reserved_cost_usd"] == "0"
        assert summary["recorded_votes"] == 1
    elif failure in {"http", "missing_usage"}:
        assert calls == ["count", "create"]
        assert row["actual_cost_usd"] is None and row["usage_complete"] is False
        assert row["reserved_cost_usd"] == "0.13192"
        assert summary["recorded_votes"] == 1
        if failure == "http":
            assert row["provider_request_id"] == "req_synthetic_failure"
            assert row["provider_error_type"] == "invalid_request_error"
            assert row["failure_diagnostic"] == {
                "schema_version": 2,
                "category": "http_error",
                "http_status": 400,
                "rejection_reason": "unclassified",
            }
    else:
        assert len(calls) == 28
        assert row["decode_error"] == "invalid_generated_output"
        assert row["actual_cost_usd"] == "0.003" and row["usage_complete"] is True
        assert row["failure_diagnostic"]["category"] == "local_guard"
        assert summary["recorded_votes"] == 14


@pytest.mark.parametrize("source,billing", [("b" * 40, True), (SOURCE, False)])
def test_live_admission_fails_before_artifacts_or_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, billing: bool
) -> None:
    smoke = load_smoke(monkeypatch)
    out = tmp_path / "absent"
    with pytest.raises((ValueError, smoke.ProviderFailure)):
        asyncio.run(
            smoke.run_smoke(
                source_commit=source, evidence_dir=out, live=True, billing_ready=billing
            )
        )
    assert not out.exists()


def test_evidence_directory_is_exclusive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    smoke = load_smoke(monkeypatch)
    out = tmp_path / "existing"
    out.mkdir()
    marker = out / "marker"
    marker.write_text("synthetic-preserved")
    with pytest.raises(FileExistsError):
        asyncio.run(smoke.run_smoke(source_commit=SOURCE, evidence_dir=out))
    assert marker.read_text() == "synthetic-preserved"


@pytest.mark.parametrize("argv", [[], ["--live", "--offline"], ["--offline"]])
def test_cli_requires_explicit_mode_source_and_destination(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        load_smoke(monkeypatch).main(argv)
    assert error.value.code == 2
