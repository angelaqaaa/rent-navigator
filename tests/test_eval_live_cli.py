"""Paid entry fails before client construction unless every prerequisite is present."""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_eval_live import SOURCE, permit

from rent_navigator.eval import live_cli
from rent_navigator.eval.__main__ import main


def arguments(tmp_path: Path) -> argparse.Namespace:
    grant = permit()
    path = tmp_path / "permit.json"
    path.write_text(grant.model_dump_json())
    return argparse.Namespace(
        command="live-gate",
        data_dir=Path("eval"),
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
    with pytest.raises(ValueError):
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
