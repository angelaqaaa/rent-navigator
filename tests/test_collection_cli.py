"""Recording-fake command integration; manifest flags are not genuine measurements."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import MessageTokensCount
from eval_fixtures import TOY_SOURCE_SHA, write_toy_data
from test_eval_real_collection import SyntheticRealCompositionPort

from rent_navigator.corpus import load_corpus
from rent_navigator.eval import __main__ as cli
from rent_navigator.eval import collection_cli
from rent_navigator.eval.collection import RealCollectionManifest
from rent_navigator.eval.dry_run import DryRunManifest
from rent_navigator.provider import ProviderFailure


def arguments(command: str, data: Path, output: Path) -> list[str]:
    return [
        command,
        "--cap-usd",
        "43" if command == "collect" else "1",
        "--results-dir",
        str(output),
        "--data-dir",
        str(data),
        "--image-digest",
        "sha256:" + "3" * 64,
        "--docker-version",
        "synthetic-docker",
        "--provider-access-date",
        "2026-10-13",
        "--source-clean",
        "--os",
        "synthetic",
        "--cpu",
        "synthetic",
        "--ram-bytes",
        "1024",
    ]


@pytest.mark.parametrize("command", ["collect", "collect-dry-run"])
def test_command_records_ledger_before_operations_and_preserves_read_only_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    corpus = load_corpus()
    data = write_toy_data(tmp_path / "data", corpus)
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    output = tmp_path / "output"
    events: list[str] = []

    class RecordingPort(SyntheticRealCompositionPort):
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            ledger = json.loads((output / "batch-ledger.json").read_text())
            assert len(ledger["batches"]) == 1
            assert Decimal(ledger["batches"][0]["forecast_usd"]) <= Decimal(
                ledger["batches"][0]["cap_usd"]
            )
            assert Decimal(ledger["batches"][0]["forecast_usd"]) == Decimal(
                "42.26040" if command == "collect" else "0.77652"
            )
            assert Decimal(ledger["batches"][0]["cap_usd"]) == Decimal(
                "43" if command == "collect" else "1"
            )
            events.append("count")
            return await super().count_tokens(**kwargs)

    port = RecordingPort()

    class Client:
        messages = port

        async def close(self) -> None:
            events.append("close")

    def fake_client(**kwargs: Any) -> Any:
        assert kwargs == {"api_key": ""}
        return Client()

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SOURCE_COMMIT", TOY_SOURCE_SHA)
    monkeypatch.setattr(collection_cli, "create_client", fake_client)
    cli.main(arguments(command, data, output))
    payload = json.loads(capsys.readouterr().out)
    assert events[-1] == "close"
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    if command == "collect":
        manifest = RealCollectionManifest.model_validate_json(json.dumps(payload))
        assert manifest.collection_complete and manifest.evaluation_complete and manifest.reportable
        assert manifest.source_sha == TOY_SOURCE_SHA
        assert len(manifest.started_attempts) == 166 and port.judge_calls == 160
        assert (output / "collection" / "manifest.json").is_file()
    else:
        dry_manifest = DryRunManifest.model_validate_json(json.dumps(payload))
        assert dry_manifest.dry_run_complete and dry_manifest.dry_run
        assert not dry_manifest.reportable and not dry_manifest.evaluation_complete
        assert [(row.case_id, row.arm, row.repeat) for row in dry_manifest.attempts] == [
            ("R01", "production", 0),
            ("N01", "production", 0),
            ("Q01", "production", 0),
        ]
        assert port.actor_calls == 5 and port.judge_calls == 3
        rows = [
            json.loads(line)
            for line in (output / "dry-run" / "attempts.jsonl").read_text().splitlines()
        ]
        assert len(rows) == 3
        assert all(
            row["judge_wall_seconds"] is not None and row["serving_duration_ms"] >= 0
            for row in rows
        )
        calls = [call for row in rows for call in row["provider_calls"]]
        assert len(calls) == 8 and all(call["usage_complete"] for call in calls)
        assert all(call["requested_model_id"] == call["returned_model_id"] for call in calls)
        assert [call["stop_reason"] for call in calls].count("end_turn") == 6
        assert [call["stop_reason"] for call in calls].count("tool_use") == 2
        assert not (output / "dry-run" / "plan.json").exists()


@pytest.mark.parametrize("forecast,cap", [("2", "1"), ("NaN", "1"), ("1", "44"), ("-1", "1")])
def test_invalid_cap_or_forecast_is_rejected_before_client_or_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forecast: str, cap: str
) -> None:
    def forbidden(**kwargs: Any) -> Any:
        raise AssertionError("Invalid amount must not construct a provider client")

    monkeypatch.setattr(collection_cli, "create_client", forbidden)
    output = tmp_path / "absent"
    args = arguments("collect", tmp_path / "data", output)
    args[args.index("--cap-usd") + 1] = cap
    with pytest.raises(SystemExit) as error:
        cli.main([*args, "--forecast-usd", forecast])
    assert error.value.code == 1 and not output.exists()


def test_file_ledger_appends_without_over_reserving_and_refuses_reuse(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = collection_cli.FileBatchLedger(path, cap_usd=Decimal("1"))
    ledger.reserve_batch(Decimal("0.25"))
    ledger.reserve_batch(Decimal("0.5"))
    entries = json.loads(path.read_text())["batches"]
    assert [entry["forecast_usd"] for entry in entries] == ["0.25", "0.5"]
    before = path.read_bytes()
    with pytest.raises(ProviderFailure):
        ledger.reserve_batch(Decimal("0.5"))
    assert path.read_bytes() == before
    with pytest.raises(FileExistsError):
        collection_cli.FileBatchLedger(path, cap_usd=Decimal("1"))


@pytest.mark.parametrize("failure", ["actor", "judge"])
def test_dry_run_keeps_incomplete_accounting_and_never_reports_a_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    data = write_toy_data(tmp_path / "data", load_corpus())
    output = tmp_path / "incomplete"
    port = SyntheticRealCompositionPort(
        fail_actor=failure == "actor", fail_judge=failure == "judge"
    )

    class Client:
        messages = port

        async def close(self) -> None:
            pass

    monkeypatch.setenv("SOURCE_COMMIT", TOY_SOURCE_SHA)
    monkeypatch.setattr(collection_cli, "create_client", lambda **kwargs: Client())
    with pytest.raises(SystemExit) as error:
        cli.main(arguments("collect-dry-run", data, output))
    assert error.value.code == 1
    manifest = DryRunManifest.model_validate_json(capsys.readouterr().out)
    assert not manifest.dry_run_complete and not manifest.reportable
    assert not manifest.evaluation_complete and len(manifest.attempts) == 1
    details = json.loads((output / "dry-run" / "attempts.jsonl").read_text())
    if failure == "actor":
        assert details["cost"]["actual_cost_usd"] is None
        assert details["cost"]["reserved_cost_usd"] == "0.027"
        assert port.judge_calls == 0
    else:
        assert manifest.attempts[0].judge_cost_usd == Decimal("0.003")
        assert port.judge_calls == 1


def test_missing_clean_source_assertion_blocks_before_client_or_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOURCE_COMMIT", TOY_SOURCE_SHA)
    output = tmp_path / "absent"
    args = arguments("collect", tmp_path / "data", output)
    args.remove("--source-clean")
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 1 and not output.exists()
