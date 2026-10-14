"""Offline GitHub transport fixtures; no live status or credentials are changed."""

import importlib.util
import io
import json
import sys
import zipfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rent_navigator.eval.live_identity import REPOSITORY


def _script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_ci.py"
    spec = importlib.util.spec_from_file_location("live_ci_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _script()


def successful_needs() -> dict[str, object]:
    return {
        name: {"result": "success"}
        for name in (
            "checks",
            "docker",
            "offline-eval",
            "live-eval-plan",
            "live-eval-execute",
            "live-eval-reuse",
        )
    }


@pytest.mark.parametrize("mode", ["execute", "reuse"])
def test_only_the_selected_successful_branch_counts(mode: str) -> None:
    needs = successful_needs()
    other = "live-eval-reuse" if mode == "execute" else "live-eval-execute"
    needs[other] = {"result": "skipped"}
    ci.require_prerequisites(needs, mode)
    selected = "live-eval-" + mode
    for result in ("skipped", "cancelled", "failure", "pending", None):
        with pytest.raises(ValueError):
            ci.require_prerequisites({**needs, selected: {"result": result}}, mode)
    for missing in ("checks", "docker", "offline-eval", "live-eval-plan", selected):
        with pytest.raises(ValueError):
            ci.require_prerequisites(
                {key: value for key, value in needs.items() if key != missing}, mode
            )


def test_unknown_selection_is_not_an_exemption() -> None:
    with pytest.raises(ValueError):
        ci.require_prerequisites(successful_needs(), "skipped")


def test_paginated_inventory_consumes_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def command(*arguments: str) -> bytes:
        calls.append(arguments)
        return json.dumps([{"artifacts": [{"id": 1}]}, {"artifacts": [{"id": 2}]}]).encode()

    monkeypatch.setattr(ci, "command", command)
    assert ci.pages("inventory?per_page=100", "artifacts") == [{"id": 1}, {"id": 2}]
    assert calls == [("gh", "api", "--paginate", "--slurp", "inventory?per_page=100")]


def environment() -> dict[str, object]:
    return {
        "name": "live-eval",
        "can_admins_bypass": False,
        "deployment_branch_policy": None,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 197411709}}],
            }
        ],
    }


def test_environment_is_read_back_without_relaxing_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci, "api", lambda _path: environment())
    ci.protected_environment()
    changes: tuple[dict[str, object], ...] = (
        {"can_admins_bypass": True},
        {"name": "unprotected"},
        {"protection_rules": []},
        {"deployment_branch_policy": {"protected_branches": False}},
    )
    for change in changes:
        monkeypatch.setattr(ci, "api", lambda _path, change=change: {**environment(), **change})
        with pytest.raises(ValueError):
            ci.protected_environment()


def zip_payload(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return stream.getvalue()


def test_archive_and_manifest_digests_are_both_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = b'{"synthetic":true}'
    payload = zip_payload({"manifest.json": manifest, "results.jsonl": b""})
    plan = {
        "artifact_id": 1,
        "archive_sha256": sha256(payload).hexdigest(),
        "manifest_sha256": sha256(manifest).hexdigest(),
    }
    monkeypatch.setattr(ci, "command", lambda *_args: payload)
    destination = tmp_path / "valid"
    ci.download_artifact(plan, destination)
    assert (destination / "manifest.json").read_bytes() == manifest
    for key in ("archive_sha256", "manifest_sha256"):
        with pytest.raises(ValueError):
            ci.download_artifact({**plan, key: "0" * 64}, tmp_path / key)
    with pytest.raises(FileExistsError):
        ci.download_artifact(plan, destination)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "dir/../../outside"])
def test_archive_path_traversal_never_writes_outside_evidence(
    path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = zip_payload({path: b"bad"})
    plan = {
        "artifact_id": 1,
        "archive_sha256": sha256(payload).hexdigest(),
        "manifest_sha256": "0" * 64,
    }
    monkeypatch.setattr(ci, "command", lambda *_args: payload)
    with pytest.raises(ValueError):
        ci.download_artifact(plan, tmp_path / "evidence")
    assert not (tmp_path / "outside").exists()


def test_missing_permit_or_secret_fails_without_printing_its_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ci, "protected_environment", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["live_ci.py", "permit", "--permit-file", str(tmp_path / "grant")]
    )
    monkeypatch.setenv("LIVE_EVAL_PERMIT", '{"secret-sentinel":"not-a-permit"}')
    with pytest.raises(SystemExit) as raised:
        ci.main()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert "secret-sentinel" not in captured.err + captured.out
    assert "ValidationError" not in captured.err + captured.out
    assert not (tmp_path / "grant").exists()


def test_latest_trusted_app_check_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, Any]] = [
        {
            "id": index,
            "name": name,
            "head_sha": "a" * 40,
            "conclusion": "success",
            "app": {"id": 15368},
        }
        for index, name in enumerate(sorted(ci.REQUIRED_CHECKS), start=1)
    ]
    monkeypatch.setattr(ci, "pages", lambda *_args: rows)
    assert ci.accepted_checks("a" * 40) == ci.REQUIRED_CHECKS
    rows.append(
        {
            "id": 99,
            "name": "live-eval-gate",
            "head_sha": "a" * 40,
            "conclusion": "failure",
            "app": {"id": 15368},
        }
    )
    rows.append(
        {
            "id": 100,
            "name": "live-eval-gate",
            "head_sha": "a" * 40,
            "conclusion": "success",
            "app": {"id": 0},
        }
    )
    assert "live-eval-gate" not in ci.accepted_checks("a" * 40)


def test_planner_never_buys_a_rerun_or_exposes_forks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_live_identity import BASE, S, graph

    (tmp_path / "activation.json").write_text('{"schema_version":1,"baseline_phase":"pending"}')
    monkeypatch.setattr(ci, "pages", lambda *_args: [])
    monkeypatch.setattr(ci, "graph_from_git", graph)
    monkeypatch.setattr(ci, "command", lambda *_args: f"{S}\n{BASE}\n".encode())
    fields = dict(
        source=S,
        base=BASE,
        event="pull_request",
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        run_attempt=1,
        data_dir=tmp_path,
    )
    assert ci.make_plan(**fields)["mode"] == "execute"
    for changed in (dict(run_attempt=2), dict(head_repository="fork/repo")):
        with pytest.raises(ValueError):
            ci.make_plan(**{**fields, **changed})


def write_baseline(path: Path) -> None:
    from test_live_identity import BATCH, DIGEST, S

    value = {
        "schema_version": 1,
        "source_sha": S,
        "corpus_hash": "a" * 64,
        "gold_hash": "b" * 64,
        "config_hash": "c" * 64,
        "mrr_at_5": 0.7,
        "ndcg_at_5": 0.6,
        "B": 28,
        "bootstrap_run_id": BATCH,
        "bootstrap_manifest_sha256": DIGEST,
    }
    (path / "baseline.json").write_text(json.dumps(value))
    (path / "activation.json").write_text('{"schema_version":1,"baseline_phase":"active"}')


def test_planner_composes_docs_main_merge_attachment_without_rewriting_paid_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from test_live_identity import BASE, NOW, A, D, M, S, artifact, graph, merge

    class SyntheticClock:
        @staticmethod
        def now(_zone: object) -> datetime:
            return NOW

    monkeypatch.setattr(ci, "datetime", SyntheticClock)
    nodes = graph()
    nodes[D] = replace(nodes[D], parents=(M,))
    write_baseline(tmp_path)
    monkeypatch.setattr(ci, "pages", lambda *_args: [{"name": artifact().name}])
    monkeypatch.setattr(ci, "artifact_origin", lambda _row: artifact())
    monkeypatch.setattr(ci, "graph_from_git", lambda: nodes)
    monkeypatch.setattr(ci, "accepted_checks", lambda _sha: ci.REQUIRED_CHECKS)
    monkeypatch.setattr(ci, "merge_proof", lambda sha, event: merge() if sha == M else None)
    monkeypatch.setattr(ci, "command", lambda *_args: f"{D}\n{M}\n{A}\n{S}\n{BASE}\n".encode())
    result = ci.make_plan(
        source=D,
        base=M,
        event="pull_request",
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        run_attempt=1,
        data_dir=tmp_path,
    )
    assert result["mode"] == "reuse" and result["scored_sha"] == S
    assert result["relationship"]["kind"] == "docs"
    assert result["relationship"]["checked_sha"] == D
    # A second historical UUID is visible even if its producer failed: no lucky selection.
    monkeypatch.setattr(
        ci,
        "pages",
        lambda *_args: [
            {"name": artifact().name},
            {"name": f"live-{S}-00000000-0000-0000-0000-000000000020"},
        ],
    )
    with pytest.raises(ValueError, match="ambiguous"):
        ci.make_plan(
            source=D,
            base=M,
            event="pull_request",
            repository=REPOSITORY,
            head_repository=REPOSITORY,
            run_attempt=1,
            data_dir=tmp_path,
        )


def test_verifier_receives_the_external_producer_and_batch_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def command(*arguments: str) -> bytes:
        calls.append(arguments)
        return b""

    monkeypatch.setattr(ci, "command", command)
    plan = {
        "scored_sha": "a" * 40,
        "manifest_sha256": "b" * 64,
        "producer_run_id": "123",
        "batch_uuid": "00000000-0000-0000-0000-000000000019",
    }
    ci.verify_live(plan, tmp_path / "evidence", tmp_path / "eval")
    arguments = calls[0]
    assert arguments[arguments.index("--producer-run-id") + 1] == "123"
    assert arguments[arguments.index("--producer-run-attempt") + 1] == "1"
    assert arguments[arguments.index("--batch-uuid") + 1] == plan["batch_uuid"]


def test_trusted_pr_association_checks_repositories_and_actual_commit_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "a" * 40
    repository = {"full_name": REPOSITORY, "id": 123}
    association: dict[str, Any] = {
        "number": 9,
        "head": {"repo": repository},
        "base": {"repo": repository, "ref": "main"},
    }
    rows: list[dict[str, Any]] = [association]
    commits: list[dict[str, Any]] = [{"sha": source}]

    def list_pages(path: str) -> list[dict[str, Any]]:
        return commits if "/pulls/9/commits" in path else rows

    monkeypatch.setattr(ci, "list_pages", list_pages)
    ci.trusted_source_pr(source, 123)
    commits.clear()
    with pytest.raises(ValueError):
        ci.trusted_source_pr(source, 123)
    commits.append({"sha": source})
    rows.append(association)
    with pytest.raises(ValueError):
        ci.trusted_source_pr(source, 123)
    rows.pop()
    association["head"] = {"repo": {"full_name": "fork/repo", "id": 456}}
    with pytest.raises(ValueError):
        ci.trusted_source_pr(source, 123)


def origin_fixture() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    from test_live_identity import BATCH, S

    row = {
        "id": 10,
        "name": f"live-{S}-{BATCH}",
        "digest": "sha256:" + "a" * 64,
        "expired": False,
        "expires_at": "2026-12-20T00:00:00Z",
        "workflow_run": {"id": 100, "head_sha": S, "repository_id": 123, "head_repository_id": 123},
    }
    run = {
        "id": 100,
        "workflow_id": 20,
        "run_attempt": 1,
        "head_sha": S,
        "repository": {"full_name": REPOSITORY, "id": 123},
        "head_repository": {"full_name": REPOSITORY, "id": 123},
        "event": "pull_request",
    }
    jobs = [
        {
            "id": 30,
            "name": "live-eval-execute",
            "run_id": 100,
            "run_attempt": 1,
            "head_sha": S,
            "conclusion": "success",
        },
        {"id": 31, "name": "live-eval-gate", "conclusion": "success"},
    ]
    return row, run, jobs


def test_artifact_provenance_binds_repository_run_job_and_trusted_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_live_identity import DIGEST, S

    row, run, jobs = origin_fixture()
    checked_pr: list[tuple[str, int]] = []
    monkeypatch.setattr(
        ci,
        "api",
        lambda path: {"path": ".github/workflows/ci.yml"} if "/workflows/" in path else run,
    )
    monkeypatch.setattr(ci, "pages", lambda *_args: jobs)
    monkeypatch.setattr(ci, "command", lambda *_args: f"LIVE_MANIFEST_SHA256={DIGEST}\n".encode())
    monkeypatch.setattr(
        ci, "trusted_source_pr", lambda source, repo: checked_pr.append((source, repo))
    )
    artifact = ci.artifact_origin(row)
    assert artifact.run_id == "100" and artifact.producer_manifest_sha256 == DIGEST
    assert checked_pr == [(S, 123)]
    for target, field, value in (
        (run, "id", 101),
        (jobs[0], "run_id", 101),
        (jobs[0], "run_attempt", 2),
        (jobs[0], "head_sha", "b" * 40),
        (row["workflow_run"], "head_repository_id", 456),
        (run["head_repository"], "id", 456),
    ):
        old = target[field]
        target[field] = value
        with pytest.raises(ValueError):
            ci.artifact_origin(row)
        target[field] = old
    monkeypatch.setattr(ci, "command", lambda *_args: b"no producer digest\n")
    with pytest.raises(ValueError):
        ci.artifact_origin(row)


def test_regression_phase_requires_unchanged_historical_baseline_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_baseline(tmp_path)
    frozen = (tmp_path / "baseline.json").read_bytes()
    monkeypatch.setattr(ci, "command", lambda *_args: frozen)
    phase, digest = ci.validate_baseline_phase(tmp_path, "a" * 40, regression=True)
    assert phase == "active" and digest == sha256(frozen).hexdigest()
    value = json.loads(frozen)
    value["B"] = 29
    (tmp_path / "baseline.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="historical baseline"):
        ci.validate_baseline_phase(tmp_path, "a" * 40, regression=True)
    (tmp_path / "baseline.json").unlink()
    with pytest.raises(FileNotFoundError):
        ci.validate_baseline_phase(tmp_path, "a" * 40, regression=True)


def test_new_active_behavior_can_only_wait_for_a_fresh_matching_regression_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from test_live_identity import BASE, NOW, A, D, M, S, artifact, graph, merge

    class SyntheticClock:
        @staticmethod
        def now(_zone: object) -> datetime:
            return NOW

    monkeypatch.setattr(ci, "datetime", SyntheticClock)
    nodes = graph()
    nodes[D] = replace(nodes[D], parents=(M,), files={**nodes[D].files, "src/app.py": "new logic"})
    write_baseline(tmp_path)
    frozen = (tmp_path / "baseline.json").read_bytes()
    monkeypatch.setattr(ci, "pages", lambda *_args: [{"name": artifact().name}])
    monkeypatch.setattr(ci, "artifact_origin", lambda _row: artifact())
    monkeypatch.setattr(ci, "graph_from_git", lambda: nodes)
    monkeypatch.setattr(ci, "accepted_checks", lambda _sha: ci.REQUIRED_CHECKS)
    monkeypatch.setattr(ci, "merge_proof", lambda sha, event: merge() if sha == M else None)

    def command(*arguments: str) -> bytes:
        return (
            frozen if arguments[:2] == ("git", "show") else f"{D}\n{M}\n{A}\n{S}\n{BASE}\n".encode()
        )

    monkeypatch.setattr(ci, "command", command)
    fields: dict[str, Any] = dict(
        source=D,
        base=M,
        event="pull_request",
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        run_attempt=1,
        data_dir=tmp_path,
    )
    result = ci.make_plan(**fields)
    assert result["mode"] == "execute" and result["purpose"] == "regression"
    assert result["baseline_sha256"] == sha256(frozen).hexdigest()
    with pytest.raises(ValueError):
        ci.make_plan(**{**fields, "run_attempt": 2})
    # An unchanged failed source is not made eligible by changing only its README.
    failed = "f" * 40
    nodes[failed] = replace(nodes[D], sha=failed, parents=(M,))
    nodes[D] = replace(nodes[D], parents=(failed,), files={**nodes[D].files, "README.md": "docs"})
    monkeypatch.setattr(
        ci, "pages", lambda *_args: [{"name": f"live-{failed}-{artifact().batch_uuid}"}]
    )
    monkeypatch.setattr(
        ci,
        "artifact_origin",
        lambda _row: replace(
            artifact(),
            source_sha=failed,
            name=f"live-{failed}-{artifact().batch_uuid}",
            producer_conclusion="failure",
        ),
    )

    def failed_command(*arguments: str) -> bytes:
        return (
            frozen
            if arguments[:2] == ("git", "show")
            else f"{D}\n{failed}\n{M}\n{A}\n{S}\n{BASE}\n".encode()
        )

    monkeypatch.setattr(ci, "command", failed_command)
    with pytest.raises(ValueError):
        ci.make_plan(**fields)


def test_orphaned_scored_sources_still_supply_complete_behavior_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, tree_sha, blob = "a" * 40, "b" * 40, "c" * 40
    tree: dict[str, Any] = {
        "sha": tree_sha,
        "truncated": False,
        "tree": [{"path": "src/app.py", "mode": "100644", "type": "blob", "sha": blob}],
    }
    commit = {"sha": source, "tree": {"sha": tree_sha}, "parents": []}
    monkeypatch.setattr(ci, "api", lambda path: tree if "/trees/" in path else commit)
    restored = ci.historical_commit(source)
    assert restored.files == {"src/app.py": f"100644 blob {blob}"}
    tree["truncated"] = True
    with pytest.raises(ValueError, match="incomplete"):
        ci.historical_commit(source)
