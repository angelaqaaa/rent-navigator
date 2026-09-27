"""Paid entry fails before client construction unless every prerequisite is present."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_eval_live import SOURCE, activate_synthetic_baseline, permit

from rent_navigator.corpus import load_corpus
from rent_navigator.eval import live_cli
from rent_navigator.eval.__main__ import main
from rent_navigator.eval.offline import file_hash


def arguments(tmp_path: Path) -> argparse.Namespace:
    data_dir = tmp_path / "synthetic-active-data"
    shutil.copytree(Path("eval"), data_dir)
    activate_synthetic_baseline(data_dir, load_corpus())
    grant = permit(file_hash(data_dir / "baseline.json"))
    path = tmp_path / "permit.json"
    path.write_text(grant.model_dump_json())
    return argparse.Namespace(
        command="live-gate",
        data_dir=data_dir,
        output_dir=tmp_path / "output",
        source_sha=SOURCE,
        lockfile=Path("uv.lock"),
        offline_dir=tmp_path / "offline",
        permit_file=path,
        repository=grant.repository,
        workflow=grant.workflow,
        run_id=grant.run_id,
        run_attempt=grant.run_attempt,
    )


@pytest.mark.parametrize(
    "failure",
    [
        "source",
        "dirty",
        "outside_ci",
        "run",
        "attempt",
        "offline_missing",
        "offline_failure",
        "secret_missing",
    ],
)
def test_paid_entry_rejects_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    args = arguments(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", args.repository)
    monkeypatch.setenv("GITHUB_RUN_ID", args.run_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv(
        "OFFLINE_PREREQUISITES",
        json.dumps({name: "success" for name in ("checks", "docker", "offline-eval-run")}),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if failure == "outside_ci":
        monkeypatch.delenv("GITHUB_ACTIONS")
    if failure == "run":
        monkeypatch.setenv("GITHUB_RUN_ID", "999")
    if failure == "attempt":
        args.run_attempt = 2
    if failure == "offline_missing":
        monkeypatch.delenv("OFFLINE_PREREQUISITES")

    def git(command: list[str], *, text: bool) -> str:
        if command[1] == "rev-parse":
            return ("9" * 40 if failure == "source" else SOURCE) + "\n"
        return " M source.py\n" if failure == "dirty" else ""

    def offline(*args: Any, **kwargs: Any) -> None:
        if failure == "offline_failure":
            raise ValueError("synthetic rejected evidence")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr(live_cli, "verify_offline", offline)
    monkeypatch.setattr(live_cli, "create_client", forbidden)
    expected = {
        "source": "clean exact authorized CI source",
        "dirty": "clean exact authorized CI source",
        "outside_ci": "clean exact authorized CI source",
        "run": "clean exact authorized CI source",
        "attempt": "first run attempt",
        "offline_missing": "Offline prerequisite evidence is missing",
        "offline_failure": "synthetic rejected evidence",
        "secret_missing": "Protected provider credential is unavailable",
    }[failure]
    with pytest.raises(ValueError, match=expected):
        live_cli.live_command(args)
    assert not args.output_dir.exists()


def test_current_unfunded_bootstrap_stops_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = arguments(tmp_path)
    payload = json.loads(args.permit_file.read_text())
    payload.update(
        purpose="bootstrap",
        baseline_sha256=None,
        funded_slot="wp9_bootstrap_savings_repair",
        future_live_batches_remaining=2,
        still_required_usd="32.861",
    )
    args.permit_file.write_text(json.dumps(payload))
    args.data_dir = Path("eval")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unfunded bootstrap must fail before execution prerequisites")

    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(live_cli, "verify_offline", forbidden)
    monkeypatch.setattr(live_cli, "create_client", forbidden)
    with pytest.raises(ValueError, match="funded budget"):
        live_cli.live_command(args)
    assert not args.output_dir.exists()


def test_verify_cli_requires_external_producer_identity(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "verify-live",
                "--data-dir",
                "eval",
                "--output-dir",
                "/nonexistent",
                "--source-sha",
                SOURCE,
                "--manifest-sha256",
                "0" * 64,
            ]
        )
    assert failure.value.code == 2
    assert "--producer-run-id" in capsys.readouterr().err
