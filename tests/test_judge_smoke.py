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
from anthropic.types import Message, MessageTokensCount, TextBlock

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
    assert summary["fail_to_pass_flips"] == 0
    assert summary["field_agreement"] == {"matched": 132, "denominator": 132}
    assert summary["unstarted_votes"] == []


def test_cli_accepts_complete_nonperfect_proposed_label_agreement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = load_smoke(monkeypatch)
    original = smoke.OfflineMessages
    counts: list[dict[str, Any]] = []
    generations: list[dict[str, Any]] = []

    class RecordingMessages:
        def __init__(self, value: Any, expected: Any) -> None:
            self.good = original(value, expected)
            self.expected = expected

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            counts.append(kwargs)
            response: MessageTokensCount = await self.good.count_tokens(**kwargs)
            return response

        async def create(self, **kwargs: Any) -> Message:
            generations.append(kwargs)
            response: Message = await self.good.create(**kwargs)
            if self.expected is not None and len(generations) <= 5:
                block = response.content[0]
                assert isinstance(block, TextBlock)
                output = json.loads(block.text)
                output["false_pass"] = not output["false_pass"]
                # A measurement mismatch must not turn a proposed failure into a pass.
                if not output["false_pass"]:
                    output["statements"][0]["factual"] = "contradicted"
                response = response.model_copy(
                    update={"content": [TextBlock(type="text", text=json.dumps(output))]}
                )
            return response

    monkeypatch.setattr(smoke, "OfflineMessages", RecordingMessages)
    out = tmp_path / "synthetic-nonperfect"
    assert smoke.main(["--offline", "--source-commit", SOURCE, "--evidence-dir", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert len(counts) == len(generations) == 14
    assert summary["complete"] is True
    assert summary["r04_smoke_passed"] is True
    assert summary["calibration_matched"] == 8
    assert summary["calibration_denominator"] == 13
    assert summary["execution_mode"] == "synthetic"
    assert summary["real_generation_attempts"] == 0
    assert summary["expectations"] == "proposed_not_independently_reviewed"
    rows = [json.loads((out / f"J{number:02}.json").read_text()) for number in range(1, 14)]
    assert [row["agreement"]["exact"] for row in rows] == [False] * 5 + [True] * 8


@pytest.mark.parametrize(
    "output_tokens,elapsed_seconds,exit_code",
    [(6000, 90.0, 0), (6001, 0.0, 1), (100, 90.001, 1), (100, 120.001, 1)],
)
def test_cli_enforces_recorded_output_and_elapsed_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    output_tokens: int,
    elapsed_seconds: float,
    exit_code: int,
) -> None:
    smoke = load_smoke(monkeypatch)
    original_messages, original_run = smoke.OfflineMessages, smoke.run_smoke
    now = 0.0
    calls: list[str] = []

    class RecordingMessages:
        def __init__(self, value: Any, expected: Any) -> None:
            self.good = original_messages(value, expected)

        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            calls.append("count")
            response: MessageTokensCount = await self.good.count_tokens(**kwargs)
            return response

        async def create(self, **kwargs: Any) -> Message:
            nonlocal now
            calls.append("create")
            response: Message = await self.good.create(**kwargs)
            if len(calls) == 2:
                now += elapsed_seconds
            return response.model_copy(
                update={"usage": response.usage.model_copy(update={"output_tokens": output_tokens})}
            )

    async def recorded_run(**kwargs: Any) -> dict[str, Any]:
        summary: dict[str, Any] = await original_run(**kwargs, clock=lambda: now)
        return summary

    monkeypatch.setattr(smoke, "OfflineMessages", RecordingMessages)
    monkeypatch.setattr(smoke, "run_smoke", recorded_run)
    out = tmp_path / "synthetic-limits"
    assert (
        smoke.main(["--offline", "--source-commit", SOURCE, "--evidence-dir", str(out)])
        == exit_code
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["complete"] is (exit_code == 0)
    assert summary["calibration_matched"] == (13 if exit_code == 0 else 1)
    assert summary["r04_smoke_passed"] is (True if exit_code == 0 else None)
    assert summary["real_generation_attempts"] == 0
    assert calls == ["count", "create"] * (14 if exit_code == 0 else 1)
    assert len(summary["unstarted_votes"]) == (0 if exit_code == 0 else 13)
    for path in out.glob("J*.json"):
        row = json.loads(path.read_text())
        assert row["usage"]["output_tokens"] == output_tokens
        assert row["elapsed_seconds"] == pytest.approx(
            elapsed_seconds if path.stem == "J01" else 0.0
        )


@pytest.mark.parametrize(
    "location,changes",
    [
        ("missing_vote", {}),
        ("malformed_json", {}),
        ("J01", {"decoded_judgment": None}),
        ("J01", {"decoded_judgment": {}}),
        ("J01", {"stop_reason": "max_tokens"}),
        ("J01", {"requested_model_id": "synthetic-wrong-model"}),
        ("J01", {"returned_model_id": "synthetic-wrong-model"}),
        ("J01", {"usage_complete": False}),
        ("J01", {"usage": {"input_tokens": None, "output_tokens": 100}}),
        ("J01", {"source_commit": "b" * 40}),
        ("J01", {"judge_config_hash": "b" * 64}),
        ("R04", {"r04_smoke_passed": False}),
        ("summary", {"source_commit": "b" * 40}),
        ("summary", {"judge_config_hash": "b" * 64}),
        ("summary", {"complete": False}),
        ("summary", {"stop_reason": "provider_error"}),
        ("summary", {"calibration_matched": 12}),
    ],
)
def test_cli_rejects_missing_or_failed_recorded_vote_criteria(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    location: str,
    changes: dict[str, Any],
) -> None:
    smoke = load_smoke(monkeypatch)
    original = smoke.run_smoke

    async def recorded_run(**kwargs: Any) -> dict[str, Any]:
        summary: dict[str, Any] = await original(**kwargs)
        out = kwargs["evidence_dir"]
        if location == "summary":
            summary.update(changes)
        elif location == "missing_vote":
            (out / "J13.json").unlink()
        elif location == "malformed_json":
            (out / "J01.json").write_text("{")
        else:
            path = out / (location + ".json")
            row = json.loads(path.read_text())
            row.update(changes)
            path.write_text(json.dumps(row))
        return summary

    monkeypatch.setattr(smoke, "run_smoke", recorded_run)
    out = tmp_path / "synthetic-failed-criterion"
    assert smoke.main(["--offline", "--source-commit", SOURCE, "--evidence-dir", str(out)]) == 1
    assert json.loads(capsys.readouterr().out)["real_generation_attempts"] == 0


@pytest.mark.parametrize("criterion", ["false_pass", "s3_contradiction"])
def test_cli_requires_actual_r04_validator_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    criterion: str,
) -> None:
    smoke = load_smoke(monkeypatch)
    original = smoke.OfflineMessages.create

    async def create(port: Any, **kwargs: Any) -> Message:
        response: Message = await original(port, **kwargs)
        if port.expected is None:
            block = response.content[0]
            assert isinstance(block, TextBlock)
            output = json.loads(block.text)
            if criterion == "false_pass":
                output["false_pass"] = False
            else:
                output["statements"]["s3"]["factual"] = "supported"
            response = response.model_copy(
                update={"content": [TextBlock(type="text", text=json.dumps(output))]}
            )
        return response

    monkeypatch.setattr(smoke.OfflineMessages, "create", create)
    out = tmp_path / "synthetic-r04-failure"
    assert smoke.main(["--offline", "--source-commit", SOURCE, "--evidence-dir", str(out)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["complete"] is False
    assert summary["r04_smoke_passed"] is False
    assert summary["real_generation_attempts"] == 0
    row = json.loads((out / "R04.json").read_text())
    assert row["failed_criterion"] == "r04_smoke"


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
    assert smoke._passes_gate(summary, out, SOURCE) is False
    assert row["decoded_judgment"] is None
    for path in out.iterdir():
        text = path.read_text()
        assert sentinel not in text
        assert '"headers"' not in text and '"error_message"' not in text
        assert '"body"' not in text and '"packet"' not in text
    if failure == "budget":
        assert row["thinking_metrics"] is None
        assert calls == ["count"]
        assert row["failure_code"] == "budget_exhausted"
        assert row["actual_cost_usd"] == row["reserved_cost_usd"] == "0"
        assert summary["recorded_votes"] == 1
    elif failure in {"http", "missing_usage"}:
        if failure == "http":
            assert row["thinking_metrics"] is None
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
        assert len(calls) == 2
        assert row["decode_error"] == "invalid_generated_output"
        assert row["actual_cost_usd"] == "0.003" and row["usage_complete"] is True
        assert row["failure_diagnostic"]["category"] == "local_guard"
        assert summary["recorded_votes"] == 1


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


def test_fail_to_pass_stops_at_first_vote_and_preserves_unstarted_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    smoke = load_smoke(monkeypatch)
    original = smoke.OfflineMessages.create
    calls: list[str] = []

    async def create(port: Any, **kwargs: Any) -> Message:
        calls.append("generation")
        response: Message = await original(port, **kwargs)
        block = response.content[0]
        assert isinstance(block, TextBlock)
        grades = json.loads(block.text)
        for claim in grades["required_claims"].values():
            claim["result"] = "met"
        for statement in grades["statements"].values():
            statement["factual"] = "supported"
        grades["false_pass"] = False
        grades["policy_violations"] = {f"S{i:02}": False for i in range(1, 9)}
        return response.model_copy(
            update={"content": [TextBlock(type="text", text=json.dumps(grades))]}
        )

    monkeypatch.setattr(smoke.OfflineMessages, "create", create)
    out = tmp_path / "synthetic-dangerous-flip"
    summary = asyncio.run(smoke.run_smoke(source_commit=SOURCE, evidence_dir=out))
    row = json.loads((out / "J01.json").read_text())
    assert row["expected_task_success"] is False and row["observed_task_success"] is True
    assert row["fail_to_pass"] is True and row["failed_criterion"] == "fail_to_pass"
    assert summary["fail_to_pass_flips"] == 1 and not summary["complete"]
    assert summary["completed_votes"] == ["J01"]
    assert summary["unstarted_votes"] == [*(f"J{n:02}" for n in range(2, 14)), "R04"]
    assert calls == ["generation"] and not (out / "J02.json").exists()
    assert smoke._passes_gate(summary, out, SOURCE) is False
    assert row["actual_cost_usd"] == "0.003"


def test_smoke_retains_counts_only_for_observed_reasoning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from anthropic.types import ThinkingBlock
    from anthropic.types.output_tokens_details import OutputTokensDetails

    smoke = load_smoke(monkeypatch)
    original = smoke.OfflineMessages.create
    sentinel = "synthetic-reasoning-payload-never-retained"

    async def create(port: Any, **kwargs: Any) -> Message:
        response: Message = await original(port, **kwargs)
        return response.model_copy(
            update={
                "content": [
                    ThinkingBlock(type="thinking", thinking=sentinel, signature=sentinel),
                    *response.content,
                ],
                "usage": response.usage.model_copy(
                    update={
                        "output_tokens_details": OutputTokensDetails(thinking_tokens=40),
                    }
                ),
            }
        )

    monkeypatch.setattr(smoke.OfflineMessages, "create", create)
    out = tmp_path / "synthetic-thinking-metrics"
    summary = asyncio.run(smoke.run_smoke(source_commit=SOURCE, evidence_dir=out))
    assert smoke._passes_gate(summary, out, SOURCE)
    row = json.loads((out / "J01.json").read_text())
    assert row["thinking_metrics"] == {
        "thinking_blocks": 1,
        "redacted_thinking_blocks": 0,
        "thinking_present": True,
        "output_tokens": 100,
        "thinking_tokens": 40,
        "thinking_output_share": 0.4,
    }
    assert summary["cost"]["actual_cost_usd"] == "0.042"
    assert all(sentinel not in path.read_text() for path in out.iterdir())


@pytest.mark.parametrize("argv", [[], ["--live", "--offline"], ["--offline"]])
def test_cli_requires_explicit_mode_source_and_destination(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        load_smoke(monkeypatch).main(argv)
    assert error.value.code == 2
