"""Offline CLI boundaries with isolated toy datasets; no provider composition."""

import json
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from eval_fixtures import TOY_SOURCE_SHA, write_toy_data
from test_eval_offline import toy_corpus as _toy_corpus
from test_eval_security import synthetic_security_xml

from rent_navigator.corpus import Corpus
from rent_navigator.eval import __main__ as cli
from rent_navigator.eval import offline

toy_corpus = _toy_corpus


def test_draft_candidate_validation_is_read_only_and_nonexecuting(
    tmp_path: Path,
    toy_corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = write_toy_data(tmp_path / "data", toy_corpus, approved=False)
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    monkeypatch.setattr(cli, "load_corpus", lambda: toy_corpus)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("candidate validation must not execute")

    monkeypatch.setattr(offline, "_report", forbidden)
    cli.main(["validate-gold", "--data-dir", str(data)])
    result = json.loads(capsys.readouterr().out)
    assert result["approval_status"] == "draft"
    assert result["executed"] is False
    assert result["case_count"] == 16
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before


def test_plan_only_writes_schedule_without_requiring_gold_approval(
    tmp_path: Path, toy_corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = write_toy_data(tmp_path / "data", toy_corpus, approved=False)
    monkeypatch.setattr(cli, "load_corpus", lambda: toy_corpus)
    output = tmp_path / "plan"
    args = ["plan", "--data-dir", str(data), "--output-dir", str(output)]
    cli.main(args)
    plan = json.loads((output / "plan.json").read_text())
    assert len(plan["warmups"]) == 6
    assert len(plan["measured"]) == 160
    assert sorted(path.name for path in output.iterdir()) == ["plan.json"]
    before = (output / "plan.json").read_bytes()
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 1
    assert (output / "plan.json").read_bytes() == before


def test_offline_draft_is_nonzero_with_truthful_approval_reason(
    tmp_path: Path,
    toy_corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = write_toy_data(tmp_path / "data", toy_corpus, approved=False)
    xml = synthetic_security_xml(tmp_path / "security.xml")
    monkeypatch.setattr(cli, "load_corpus", lambda: toy_corpus)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "offline",
                "--data-dir",
                str(data),
                "--output-dir",
                str(tmp_path / "output"),
                "--security-report",
                str(xml),
                "--source-sha",
                TOY_SOURCE_SHA,
            ]
        )
    assert error.value.code == 1
    assert "explicit approval" in capsys.readouterr().err
    assert (tmp_path / "output" / "failure.json").is_file()


@pytest.mark.parametrize(
    "args",
    [
        ["validate-gold"],
        ["plan", "--data-dir", "unused"],
        ["offline", "--data-dir", "unused"],
        ["validate-gold", "--data-dir", "unused", "--live"],
    ],
)
def test_cli_requires_explicit_paths_and_has_no_live_flag(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_ci_summary_runs_always_and_binds_exact_artifacts() -> None:
    workflow = yaml.load(Path(".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "push"}
    summary = workflow["jobs"]["offline-eval"]
    assert summary["if"] == "always()"
    assert set(summary["needs"]) == {"checks", "docker", "offline-eval-run"}
    producer = workflow["jobs"]["offline-eval-run"]
    upload = next(
        step for step in producer["steps"] if "actions/upload-artifact@" in step.get("uses", "")
    )
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["overwrite"] == "false"
    download = next(
        step for step in summary["steps"] if "actions/download-artifact@" in step.get("uses", "")
    )
    assert upload["with"]["name"] == download["with"]["name"]
    assert download["with"]["digest-mismatch"] == "error"
    assert "SOURCE_COMMIT" in download["with"]["name"]
    assert workflow["jobs"]["live-eval-gate"]["if"] == "always()"
