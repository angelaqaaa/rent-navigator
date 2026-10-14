"""Restricted smoke admission, fourteen fake votes, costs and R14 privacy."""

import asyncio
import importlib.util
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message, MessageTokensCount, TextBlock

SOURCE = "a" * 40
RUNNER = "b" * 40
MEASUREMENT_PACKETS = [*(f"J{number:02}" for number in range(5, 14)), "R04"]


def load_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/judge_smoke.py"
    spec = importlib.util.spec_from_file_location("judge_smoke_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "head_sha", lambda: SOURCE)
    return module


def test_selected_measurement_records_ten_votes_without_qualifying_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "head_sha", lambda: RUNNER)
    monkeypatch.setattr(
        smoke, "_measurement_provenance", lambda source, runner: RUNNER, raising=False
    )
    output = tmp_path / "synthetic-measurement"
    summary = asyncio.run(
        smoke.run_smoke(
            source_commit=SOURCE,
            runner_commit=RUNNER,
            evidence_dir=output,
            measurement=True,
            packets=MEASUREMENT_PACKETS,
        )
    )
    assert summary["purpose"] == "measurement after a failed gate"
    assert summary["measurement"] is True and summary["gate_passed"] is False
    assert summary["measurement_complete"] is True
    assert summary["requested_votes"] == summary["completed_votes"] == MEASUREMENT_PACKETS
    assert summary["unstarted_votes"] == []
    assert summary["planned_votes"] == summary["generation_attempts"] == 10
    assert summary["calibration_denominator"] == 9
    assert summary["reservation_forecast_usd"] == "2.13840"
    assert summary["source_commit"] == summary["judging_source_commit"] == SOURCE
    assert summary["runner_commit"] == RUNNER
    assert not smoke._passes_gate(summary, output, SOURCE)
    assert sorted(path.stem for path in output.iterdir()) == sorted(
        [*MEASUREMENT_PACKETS, "summary"]
    )


@pytest.mark.parametrize("failure", ["flip", "decode", "r04", "time", "output"])
def test_measurement_continues_and_keeps_each_failed_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "head_sha", lambda: RUNNER)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: RUNNER)
    created: list[str] = []
    now = 0.0
    selected = ["J06", "J07", "R04"]
    expected_reason = {
        "flip": "fail_to_pass",
        "decode": "call_failure",
        "r04": "r04_smoke",
        "time": "elapsed_bound",
        "output": "usage_bound",
    }[failure]

    def factory(value: Any, expected: Any) -> Any:
        identifier = selected[len(created)]
        good = smoke.OfflineMessages(value, expected)

        class Port:
            async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
                return MessageTokensCount(input_tokens=1000)

            async def create(self, **kwargs: Any) -> Message:
                nonlocal now
                created.append(identifier)
                response: Message = await good.create(**kwargs)
                if identifier == ("R04" if failure == "r04" else "J06"):
                    block = response.content[0]
                    assert isinstance(block, TextBlock)
                    output = json.loads(block.text)
                    if failure == "flip":
                        for leaf in output["required_claims"].values():
                            leaf["result"] = "met"
                        for leaf in output["statements"].values():
                            leaf["factual"] = "supported"
                        output["false_pass"] = False
                        output["policy_violations"] = {f"S{i:02}": False for i in range(1, 9)}
                    elif failure == "r04":
                        output["false_pass"] = False
                    elif failure == "decode":
                        return response.model_copy(
                            update={
                                "content": [
                                    TextBlock(type="text", text="synthetic-private-invalid-json")
                                ]
                            }
                        )
                    elif failure == "time":
                        now += 91
                    elif failure == "output":
                        response = response.model_copy(
                            update={
                                "usage": response.usage.model_copy(update={"output_tokens": 6001})
                            }
                        )
                    response = response.model_copy(
                        update={"content": [TextBlock(type="text", text=json.dumps(output))]}
                    )
                return response

        return Port()

    output = tmp_path / failure
    summary = asyncio.run(
        smoke.run_smoke(
            source_commit=SOURCE,
            runner_commit=RUNNER,
            evidence_dir=output,
            measurement=True,
            packets=selected,
            messages_factory=factory,
            clock=lambda: now,
        )
    )
    expected_created = selected[:1] if failure == "decode" else selected
    assert created == expected_created
    assert summary["measurement_complete"] is (failure != "decode")
    assert summary["complete"] is (failure != "decode")
    assert summary["gate_passed"] is False and not smoke._passes_gate(summary, output, SOURCE)
    assert summary["completed_votes"] == expected_created
    assert summary["unstarted_votes"] == (selected[1:] if failure == "decode" else [])
    failed = json.loads((output / (("R04" if failure == "r04" else "J06") + ".json")).read_text())
    assert failed["failed_criterion"] == expected_reason
    if failure == "flip":
        assert failed["fail_to_pass"] is True
    if failure == "decode":
        assert failed["decode_rejection_reason"] == "json_invalid"
        assert failed["actual_cost_usd"] == "0.003"
        assert summary["decoded_votes"] == 0
    assert "synthetic-private-invalid-json" not in "".join(
        path.read_text() for path in output.iterdir()
    )


def test_complete_fourteen_vote_measurement_cannot_pass_gate_or_cli_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: SOURCE)
    output = tmp_path / "synthetic-all-fourteen"
    assert (
        smoke.main(
            [
                "--offline",
                "--measurement",
                "--packets",
                *smoke.PACKET_IDS,
                "--source-commit",
                SOURCE,
                "--runner-commit",
                SOURCE,
                "--evidence-dir",
                str(output),
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["measurement_complete"] is True and summary["calibration_matched"] == 13
    assert summary["r04_smoke_passed"] is True and summary["fail_to_pass_flips"] == 0
    assert summary["gate_passed"] is False
    assert not smoke._passes_gate(summary, output, SOURCE)
    assert summary["source_commit"] == summary["judging_source_commit"] == SOURCE
    assert summary["runner_commit"] == SOURCE
    assert summary["judge_config_hash"] == smoke.MEASUREMENT_JUDGE_HASH
    assert summary["budget_limit_usd"] == "3.00"
    assert summary["reservation_forecast_usd"] == "2.99376"
    assert summary["completed_votes"] == list(smoke.PACKET_IDS)
    assert summary["reportable"] is False and summary["official_n16_evaluation"] is False


@pytest.mark.parametrize(
    "cap,forecast",
    [
        ("2.99", "2.99376"),
        ("3.01", "2.99376"),
        ("NaN", "2.99376"),
        ("-1", "2.99376"),
        ("3.00", "1.84688"),
        ("3.00", "NaN"),
        ("3.00", "2.99375"),
    ],
)
def test_k2_forecast_and_cap_reject_before_evidence_or_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cap: str, forecast: str
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: SOURCE)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid funding bounds must stop before vote or client setup")

    monkeypatch.setattr(smoke, "fixed_votes", forbidden)
    monkeypatch.setattr(smoke, "create_client", forbidden)
    output = tmp_path / "absent"
    with pytest.raises(ValueError, match="forecast and cap"):
        asyncio.run(
            smoke.run_smoke(
                source_commit=SOURCE,
                runner_commit=SOURCE,
                evidence_dir=output,
                measurement=True,
                packets=list(smoke.PACKET_IDS),
                cap_usd=Decimal(cap),
                forecast_usd=Decimal(forecast),
            )
        )
    assert not output.exists()


@pytest.mark.parametrize("packets", [None, [], ["J05", "J05"], ["unknown"], ["J06", "J05"]])
def test_measurement_rejects_bad_selection_before_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, packets: list[str] | None
) -> None:
    smoke = load_smoke(monkeypatch)
    output = tmp_path / "absent"
    with pytest.raises(ValueError):
        asyncio.run(
            smoke.run_smoke(
                source_commit=SOURCE,
                runner_commit=RUNNER,
                evidence_dir=output,
                measurement=True,
                packets=packets,
            )
        )
    assert not output.exists()


@pytest.mark.parametrize("arguments", [{"packets": ["J05"]}, {"runner_commit": RUNNER}])
def test_ordinary_gate_rejects_measurement_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any]
) -> None:
    smoke = load_smoke(monkeypatch)
    output = tmp_path / "absent"
    with pytest.raises(ValueError):
        asyncio.run(smoke.run_smoke(source_commit=SOURCE, evidence_dir=output, **arguments))
    assert not output.exists()


@pytest.mark.parametrize(
    "drift",
    [
        "none",
        "missing_runner",
        "runner",
        "unresolved",
        "not_ancestor",
        "source",
        "unrelated",
        "profile",
        "dirty",
        "script",
    ],
)
def test_measurement_provenance_checks_ancestor_content_and_live_cleanliness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "head_sha", lambda: RUNNER)
    assert smoke.MEASUREMENT_JUDGE_HASH == smoke.judge_config_hash()
    calls: list[list[str]] = []

    def git(command: list[str], **kwargs: Any) -> Any:
        calls.append(command)
        if command[1:3] == ["rev-parse", "--verify"]:
            if drift == "unresolved":
                raise subprocess.CalledProcessError(128, command)
            return (SOURCE if drift == "source" else RUNNER) + "\n"
        if command[1] == "merge-base":
            if drift == "not_ancestor":
                raise subprocess.CalledProcessError(1, command)
            return b""
        if command[1] == "diff":
            return (
                "src/rent_navigator/eval/judge.py\0"
                if drift == "unrelated"
                else "scripts/judge_smoke.py\0tests/test_judge_smoke.py\0"
            )
        if command[1] == "status":
            return " M scripts/judge_smoke.py\n" if drift == "dirty" else ""
        if command[1] == "show":
            assert smoke.__file__ is not None
            source = Path(smoke.__file__).read_text()
            if drift == "script" and len([call for call in calls if call[1] == "show"]) == 2:
                source = source.replace("def fixed_votes()", "def changed_votes()", 1)
            return source
        raise AssertionError("Unexpected provenance command")

    monkeypatch.setattr(smoke.subprocess, "check_output", git)
    if drift == "profile":
        from rent_navigator.eval import judge

        monkeypatch.setattr(
            judge,
            "judge_request_settings",
            lambda: {
                "thinking": {"type": "adaptive", "display": "omitted"},
                "output_config": {"effort": "low"},
            },
        )
    output = tmp_path / "provenance"
    clients: list[str] = []

    def forbidden_client(**kwargs: Any) -> Any:
        clients.append("client")
        raise AssertionError("No real client is permitted")

    monkeypatch.setattr(smoke, "create_client", forbidden_client)
    runner = None if drift == "missing_runner" else SOURCE if drift == "runner" else RUNNER
    if drift == "none":
        assert smoke._measurement_provenance(RUNNER, runner) == RUNNER
        assert any(command[1] == "merge-base" for command in calls)
        assert any(command[1] == "diff" and "--no-renames" in command for command in calls)
    else:
        with pytest.raises(ValueError):
            asyncio.run(
                smoke.run_smoke(
                    source_commit=RUNNER,
                    runner_commit=runner,
                    evidence_dir=output,
                    live=True,
                    billing_ready=True,
                    measurement=True,
                    packets=["J05"],
                )
            )
    assert not output.exists() and clients == []


@pytest.mark.parametrize("drift", ["historical_hash", "run1_hash", "rubric"])
def test_measurement_identity_rejects_wrong_profile_before_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "head_sha", lambda: RUNNER)
    assert (
        smoke.MEASUREMENT_JUDGE_HASH
        == smoke.judge_config_hash()
        == ("23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8")
    )
    if drift in {"historical_hash", "run1_hash"}:
        monkeypatch.setattr(
            smoke,
            "judge_config_hash",
            lambda: (
                "258529b186b909caf4a86e6b8eda05d495f887e2ffb778891816457db1afcb04"
                if drift == "run1_hash"
                else "89df22d995b0b0c89cd91ebfa6c587bf5cd589384801e28738e0604cb26e1384"
            ),
        )
    else:
        from rent_navigator.eval import judge

        monkeypatch.setattr(judge, "JUDGE_SYSTEM", judge.JUDGE_SYSTEM + "Synthetic rubric drift.")
        assert smoke.judge_config_hash() != smoke.MEASUREMENT_JUDGE_HASH

    def prohibited(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Identity drift must stop before provenance reads or provider setup")

    monkeypatch.setattr(smoke.subprocess, "check_output", prohibited)
    monkeypatch.setattr(smoke, "create_client", prohibited)
    output = tmp_path / "wrong-profile-measurement"
    with pytest.raises(ValueError, match="Measurement runner or judge identity differs"):
        asyncio.run(
            smoke.run_smoke(
                source_commit=SOURCE,
                runner_commit=RUNNER,
                evidence_dir=output,
                live=True,
                billing_ready=True,
                measurement=True,
                packets=["J05"],
            )
        )
    assert not output.exists()


@pytest.mark.parametrize("failure", ["missing_usage", "preflight", "cancelled", "model"])
def test_measurement_hard_guards_keep_reservation_and_unstarted_votes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: RUNNER)
    calls: list[str] = []
    sentinel = "synthetic-private-hard-guard"

    def factory(value: Any, expected: Any) -> Any:
        good = smoke.OfflineMessages(value, expected)

        class Port:
            async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
                calls.append("count")
                return MessageTokensCount(input_tokens=24001 if failure == "preflight" else 1000)

            async def create(self, **kwargs: Any) -> Message:
                calls.append("generation")
                if failure == "cancelled":
                    raise asyncio.CancelledError(sentinel)
                response: Message = await good.create(**kwargs)
                return response.model_copy(
                    update={"usage": None}
                    if failure == "missing_usage"
                    else {"model": "claude-haiku-4-5-20251001"}
                )

        return Port()

    output = tmp_path / failure
    summary = asyncio.run(
        smoke.run_smoke(
            source_commit=SOURCE,
            runner_commit=RUNNER,
            evidence_dir=output,
            measurement=True,
            packets=MEASUREMENT_PACKETS,
            messages_factory=factory,
        )
    )
    assert summary["measurement_complete"] is False and summary["complete"] is False
    assert summary["gate_passed"] is False
    assert summary["attempted_votes"] == summary["completed_votes"] == ["J05"]
    assert summary["unstarted_votes"] == MEASUREMENT_PACKETS[1:]
    assert calls == (["count"] if failure == "preflight" else ["count", "generation"])
    row = json.loads((output / "J05.json").read_text())
    assert row["failed_criterion"] == "call_failure"
    if failure == "preflight":
        assert row["actual_cost_usd"] == row["reserved_cost_usd"] == "0"
        assert row["failure_code"] == "budget_exhausted"
    else:
        assert row["actual_cost_usd"] is None and row["usage_complete"] is False
        assert row["reserved_cost_usd"] == "0.21384"
        assert summary["cost"]["actual_cost_usd"] is None
        assert summary["cost"]["reserved_cost_usd"] == "0.21384"
    assert all(sentinel not in path.read_text() for path in output.iterdir())


@pytest.mark.parametrize("close_failure", [False, True])
def test_measurement_closes_recording_client_and_reports_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, close_failure: bool
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: RUNNER)
    monkeypatch.setattr(smoke.subprocess, "check_output", lambda *args, **kwargs: "")
    _, value, expected = next(vote for vote in smoke.fixed_votes() if vote[0] == "J05")
    closed: list[str] = []

    class Client:
        messages = smoke.OfflineMessages(value, expected)

        async def close(self) -> None:
            closed.append("closed")
            if close_failure:
                raise ValueError("synthetic-private-close-failure")

    monkeypatch.setattr(smoke, "create_client", lambda **kwargs: Client())
    output = tmp_path / "synthetic-client-cleanup"
    summary = asyncio.run(
        smoke.run_smoke(
            source_commit=SOURCE,
            runner_commit=RUNNER,
            evidence_dir=output,
            live=True,
            billing_ready=True,
            measurement=True,
            packets=["J05"],
        )
    )
    assert closed == ["closed"]
    assert summary["measurement_complete"] is not close_failure
    assert summary["gate_passed"] is False
    assert summary["stop_reason"] == ("provider_error" if close_failure else None)
    assert "synthetic-private-close-failure" not in (output / "summary.json").read_text()


def test_measurement_preserves_exclusive_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    smoke = load_smoke(monkeypatch)
    monkeypatch.setattr(smoke, "_measurement_provenance", lambda source, runner: RUNNER)
    output = tmp_path / "existing-synthetic"
    output.mkdir()
    marker = output / "marker"
    marker.write_text("synthetic-preserved")
    with pytest.raises(FileExistsError):
        asyncio.run(
            smoke.run_smoke(
                source_commit=SOURCE,
                runner_commit=RUNNER,
                evidence_dir=output,
                measurement=True,
                packets=["J05"],
            )
        )
    assert [path.name for path in output.iterdir()] == ["marker"]
    assert marker.read_text() == "synthetic-preserved"


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
    assert summary["budget_limit_usd"] == "3.00"
    assert summary["reservation_forecast_usd"] == "2.99376"
    assert summary["cost"]["actual_cost_usd"] == "0.042"
    assert summary["cost"]["reserved_cost_usd"] == "2.99376"
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
        assert row["reserved_cost_usd"] == "0.21384"
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
