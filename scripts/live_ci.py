"""Read-only GitHub provenance and controlled live-job orchestration."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

from rent_navigator.eval.live_identity import (
    REPOSITORY,
    REQUIRED_CHECKS,
    WORKFLOW,
    ArtifactOrigin,
    BaselineReference,
    Commit,
    MergeProof,
    paid_execution_allowed,
    prove_source_relationship,
    same_behavior,
    select_evidence,
)
from rent_navigator.eval.models import Activation
from rent_navigator.eval.offline import Baseline
from rent_navigator.eval.permit import LivePermit

_ARTIFACT = re.compile(r"^live-([0-9a-f]{40})-([0-9a-f-]{36})$")


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return cast(list[object], value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a JSON string")
    return value


def integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected a JSON integer")
    return value


def command(*arguments: str) -> bytes:
    result = subprocess.run(arguments, check=False, capture_output=True)
    if result.returncode:
        raise ValueError("required read-only command failed")
    return result.stdout


def api(path: str) -> dict[str, object]:
    return mapping(json.loads(command("gh", "api", path)))


def pages(path: str, key: str) -> list[dict[str, object]]:
    values = array(json.loads(command("gh", "api", "--paginate", "--slurp", path)))
    return [mapping(row) for page in values for row in array(mapping(page)[key])]


def list_pages(path: str) -> list[dict[str, object]]:
    values = array(json.loads(command("gh", "api", "--paginate", "--slurp", path)))
    return [mapping(row) for page in values for row in array(page)]


def trusted_source_pr(source: str, repository_id: int) -> None:
    associations = list_pages(f"repos/{REPOSITORY}/commits/{source}/pulls?per_page=100")
    trusted: list[int] = []
    for row in associations:
        head, base = mapping(row["head"]), mapping(row["base"])
        if (
            mapping(head["repo"]).get("full_name") != REPOSITORY
            or mapping(base["repo"]).get("full_name") != REPOSITORY
            or mapping(head["repo"]).get("id") != repository_id
            or mapping(base["repo"]).get("id") != repository_id
            or base.get("ref") != "main"
        ):
            continue
        number = integer(row["number"])
        commits = list_pages(f"repos/{REPOSITORY}/pulls/{number}/commits?per_page=100")
        if sum(commit.get("sha") == source for commit in commits) == 1:
            trusted.append(number)
    if len(trusted) != 1:
        raise ValueError("paid source has no unique trusted pull-request association")


def graph_from_git() -> dict[str, Commit]:
    result: dict[str, Commit] = {}
    lines = command("git", "rev-list", "--parents", "--all").decode().splitlines()
    if len(lines) > 2000:
        raise ValueError("source history exceeds the bounded proof inventory")
    for line in lines:
        sha, *parents = line.split()
        files: dict[str, str] = {}
        for entry in command("git", "ls-tree", "-r", "-z", sha).split(b"\0"):
            if entry:
                identity, path = entry.decode().split("\t", 1)
                files[path] = identity
        tree = command("git", "rev-parse", f"{sha}^{{tree}}").decode().strip()
        result[sha] = Commit(sha, tuple(parents), tree, files)
    return result


def historical_commit(source: str) -> Commit:
    """Read an orphaned scored commit without trusting artifact names as behavior identity."""
    value = api(f"repos/{REPOSITORY}/git/commits/{source}")
    if value.get("sha") != source:
        raise ValueError("historical source identity is unavailable")
    tree_sha = string(mapping(value["tree"])["sha"])
    tree = api(f"repos/{REPOSITORY}/git/trees/{tree_sha}?recursive=1")
    if tree.get("sha") != tree_sha or tree.get("truncated") is not False:
        raise ValueError("historical behavior tree is incomplete")
    files: dict[str, str] = {}
    for entry in array(tree["tree"]):
        item = mapping(entry)
        if item.get("type") != "tree":
            path = string(item["path"])
            if path in files:
                raise ValueError("historical tree has duplicate entries")
            files[path] = f"{string(item['mode'])} {string(item['type'])} {string(item['sha'])}"
    parents = tuple(string(mapping(parent)["sha"]) for parent in array(value["parents"]))
    return Commit(source, parents, tree_sha, files)


def accepted_checks(source: str) -> frozenset[str]:
    checks = pages(f"repos/{REPOSITORY}/commits/{source}/check-runs?per_page=100", "check_runs")
    accepted: set[str] = set()
    for name in REQUIRED_CHECKS:
        matches = [
            row
            for row in checks
            if row.get("name") == name and mapping(row.get("app", {})).get("id") == 15368
        ]
        if matches:
            latest = max(matches, key=lambda row: integer(row["id"]))
            if latest.get("head_sha") == source and latest.get("conclusion") == "success":
                accepted.add(name)
    return frozenset(accepted)


def merge_proof(source: str, event: str) -> MergeProof | None:
    if event != "push":
        return None
    rows = array(json.loads(command("gh", "api", f"repos/{REPOSITORY}/commits/{source}/pulls")))
    matches = [mapping(row) for row in rows if mapping(row).get("merge_commit_sha") == source]
    if len(matches) != 1:
        raise ValueError("main wrapper lacks a unique associated merged PR")
    row = api(f"repos/{REPOSITORY}/pulls/{integer(matches[0]['number'])}")
    head, base = mapping(row["head"]), mapping(row["base"])
    return MergeProof(
        repository=string(mapping(head["repo"])["full_name"]),
        merge_sha=source,
        head_sha=string(head["sha"]),
        merged=row.get("merged") is True,
        base_branch=string(base["ref"]),
        accepted_checks=accepted_checks(string(head["sha"])),
    )


def artifact_origin(row: Mapping[str, object]) -> ArtifactOrigin:
    match = _ARTIFACT.fullmatch(string(row["name"]))
    if match is None:
        raise ValueError("invalid live artifact name")
    source, batch = match.groups()
    if str(UUID(batch)) != batch:
        raise ValueError("noncanonical live batch UUID")
    workflow_run = mapping(row["workflow_run"])
    run_id = str(integer(workflow_run["id"]))
    run = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/1")
    workflow = api(f"repos/{REPOSITORY}/actions/workflows/{integer(run['workflow_id'])}")
    jobs = pages(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/1/jobs?per_page=100", "jobs")
    producer = [job for job in jobs if job.get("name") == "live-eval-execute"]
    if len(producer) != 1:
        raise ValueError("artifact has no unique paid producer job")
    job = producer[0]
    repository_id = integer(mapping(run["repository"])["id"])
    if (
        run.get("id") != int(run_id)
        or job.get("run_id") != int(run_id)
        or job.get("run_attempt") != 1
        or job.get("head_sha") != source
        or workflow_run.get("repository_id") != repository_id
        or workflow_run.get("head_repository_id") != repository_id
        or mapping(run["head_repository"]).get("id") != repository_id
    ):
        raise ValueError("artifact and producer repository, source or run identities disagree")
    trusted_source_pr(source, repository_id)
    logs = command(
        "gh", "api", f"repos/{REPOSITORY}/actions/jobs/{integer(job['id'])}/logs"
    ).decode()
    digests = re.findall(r"LIVE_MANIFEST_SHA256=([0-9a-f]{64})(?:\r?\n|$)", logs)
    if len(digests) != 1:
        raise ValueError("producer manifest digest is missing or ambiguous")
    if workflow_run.get("head_sha") != source or run.get("head_sha") != source:
        raise ValueError("artifact source differs from its producing workflow run")
    archive_digest = string(row["digest"])
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", archive_digest):
        raise ValueError("artifact archive digest is unavailable")
    return ArtifactOrigin(
        artifact_id=integer(row["id"]),
        name=string(row["name"]),
        repository=string(mapping(run["repository"])["full_name"]),
        source_sha=source,
        batch_uuid=batch,
        workflow=string(workflow["path"]),
        event=string(run["event"]),
        run_id=run_id,
        run_attempt=integer(run["run_attempt"]),
        producer_conclusion=string(job.get("conclusion") or "incomplete"),
        producer_manifest_sha256=digests[0],
        archive_sha256=archive_digest.removeprefix("sha256:"),
        expired=row.get("expired") is not False,
        expires_at=datetime.fromisoformat(string(row["expires_at"])),
        trusted_branch=mapping(run["head_repository"]).get("full_name") == REPOSITORY,
        summary_conclusion=next(
            (
                string(job.get("conclusion") or "incomplete")
                for job in jobs
                if job.get("name") == "live-eval-gate"
            ),
            "missing",
        ),
    )


def _baseline(path: Path) -> BaselineReference | None:
    if not path.exists():
        return None
    value = Baseline.model_validate_json(path.read_bytes())
    return BaselineReference(
        value.source_sha, str(value.bootstrap_run_id), value.bootstrap_manifest_sha256
    )


def validate_baseline_phase(
    data_dir: Path, base: str, *, regression: bool
) -> tuple[str, str | None]:
    activation = Activation.model_validate_json((data_dir / "activation.json").read_bytes())
    baseline_path = data_dir / "baseline.json"
    if activation.baseline_phase == "pending":
        if baseline_path.exists() or regression:
            raise ValueError("pending bootstrap cannot have an active comparison baseline")
        return "pending", None
    baseline_bytes = baseline_path.read_bytes()
    Baseline.model_validate_json(baseline_bytes)
    if regression:
        if re.fullmatch(r"[0-9a-f]{40}", base) is None:
            raise ValueError("regression requires its trusted complete PR base")
        original = command("git", "show", f"{base}:eval/baseline.json")
        if original != baseline_bytes:
            raise ValueError("regression must preserve historical baseline bytes and B")
    return "active", sha256(baseline_bytes).hexdigest()


def make_plan(
    *,
    source: str,
    base: str,
    event: str,
    repository: str,
    head_repository: str,
    run_attempt: int,
    data_dir: Path,
) -> dict[str, object]:
    if repository != REPOSITORY or head_repository != REPOSITORY:
        raise ValueError("untrusted repositories cannot select paid or reusable evidence")
    all_artifacts = pages(f"repos/{REPOSITORY}/actions/artifacts?per_page=100", "artifacts")
    history = [row for row in all_artifacts if _ARTIFACT.fullmatch(string(row["name"]))]
    graph = graph_from_git()
    merge = merge_proof(source, event)
    normalized = merge.head_sha if merge else source
    baseline = _baseline(data_dir / "baseline.json")
    ancestors = command("git", "rev-list", "--topo-order", normalized).decode().splitlines()
    for anchor in ancestors:
        references = [row for row in history if string(row["name"]).startswith(f"live-{anchor}-")]
        scored = anchor
        if (
            not references
            and baseline
            and graph.get(anchor)
            and graph[anchor].parents == (baseline.source_sha,)
        ):
            scored = baseline.source_sha
            references = [
                row for row in history if string(row["name"]).startswith(f"live-{scored}-")
            ]
        if not references:
            continue
        if anchor != normalized and not same_behavior(graph[anchor], graph[normalized]):
            # A genuinely different behavior requires its own later funded permit.
            # Equal final bytes still enter the proof, which rejects reverted intervening changes.
            continue
        # Select from all history at this source before inspecting a success; failed attempts count.
        if len(references) != 1:
            raise ValueError("scored attempt history is ambiguous; no successful-run selection")
        evidence = select_evidence(
            [artifact_origin(row) for row in references],
            source_sha=scored,
            batch_uuid=baseline.batch_uuid if baseline and baseline.source_sha == scored else None,
            now=datetime.now(UTC),
            current_run_id=os.environ.get("GITHUB_RUN_ID"),
            current_run_attempt=run_attempt,
        )
        ancestral_merges = {
            sha: proof
            for sha in ancestors
            if len(graph[sha].parents) == 2 and (proof := merge_proof(sha, "push")) is not None
        }
        relationship = prove_source_relationship(
            graph=graph,
            current_sha=source,
            scored_sha=scored,
            trusted_base_sha=base,
            batch_uuid=evidence.batch_uuid,
            manifest_sha256=evidence.producer_manifest_sha256,
            baseline=baseline,
            passing_ancestor=anchor,
            ancestor_checks=accepted_checks(anchor),
            merge=merge,
            ancestral_merges=ancestral_merges,
            accepted_ancestors={sha: accepted_checks(sha) for sha in ancestors if sha != source},
        )
        return {
            "schema_version": 1,
            "mode": "reuse",
            "relationship": asdict(relationship),
            "artifact_id": evidence.artifact_id,
            "batch_uuid": evidence.batch_uuid,
            "scored_sha": evidence.source_sha,
            "manifest_sha256": evidence.producer_manifest_sha256,
            "archive_sha256": evidence.archive_sha256,
            "producer_run_id": evidence.run_id,
        }
    phase, baseline_digest = validate_baseline_phase(
        data_dir, base, regression=baseline is not None
    )
    if normalized != source or same_behavior(graph[base], graph[source]):
        raise ValueError(
            "missing historical evidence is not authority to repurchase unchanged behavior"
        )
    # Orphaned sources still count: force-pushing cannot hide a failed unchanged run.
    for row in history:
        historical = string(row["name"])[5:45]
        prior = graph[historical] if historical in graph else historical_commit(historical)
        if same_behavior(prior, graph[source]):
            raise ValueError(
                "unchanged behavior has prior scored history without eligible evidence"
            )
    has_history = any(string(row["name"]).startswith(f"live-{source}-") for row in history)
    if paid_execution_allowed(
        repository=repository,
        head_repository=head_repository,
        event=event,
        run_attempt=run_attempt,
        has_history=has_history,
        baseline_phase=phase,
    ):
        return {
            "schema_version": 1,
            "mode": "execute",
            "checked_sha": source,
            "purpose": "bootstrap" if phase == "pending" else "regression",
            "baseline_sha256": baseline_digest,
        }
    raise ValueError("no eligible evidence and no first-attempt bootstrap path")


def protected_environment() -> None:
    value = api(f"repos/{REPOSITORY}/environments/live-eval")
    rules = [mapping(rule) for rule in array(value["protection_rules"])]
    reviewers = [rule for rule in rules if rule.get("type") == "required_reviewers"]
    if (
        value.get("name") != "live-eval"
        or value.get("can_admins_bypass") is not False
        or value.get("deployment_branch_policy") is not None
        or len(reviewers) != 1
        or reviewers[0].get("prevent_self_review") is not False
    ):
        raise ValueError("protected environment policy differs from the approved configuration")
    allowed = array(reviewers[0]["reviewers"])
    if len(allowed) != 1:
        raise ValueError("protected environment reviewer differs")
    reviewer = mapping(allowed[0])
    identity = mapping(reviewer["reviewer"])
    if reviewer.get("type") != "User" or identity.get("id") != 197411709:
        raise ValueError("protected environment reviewer differs")


def download_artifact(plan: Mapping[str, object], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    payload = command(
        "gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{plan['artifact_id']}/zip"
    )
    if sha256(payload).hexdigest() != plan["archive_sha256"]:
        raise ValueError("downloaded archive digest differs from GitHub provenance")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names: set[str] = set()
        total = 0
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            total += item.file_size
            if (
                path.is_absolute()
                or ".." in path.parts
                or item.filename in names
                or total > 64 * 1024 * 1024
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("artifact archive contains unsafe or duplicate entries")
            names.add(item.filename)
            target = destination / path
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    digest = sha256((destination / "manifest.json").read_bytes()).hexdigest()
    if digest != plan["manifest_sha256"]:
        raise ValueError("downloaded manifest differs from the successful producer digest")


def output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a") as stream:
            stream.write(f"{name}={value}\n")


def require_prerequisites(value: Mapping[str, object], mode: str) -> None:
    required = {"checks", "docker", "offline-eval", "live-eval-plan"}
    selected = {"execute": "live-eval-execute", "reuse": "live-eval-reuse"}.get(mode)
    if selected is None:
        raise ValueError("live summary has no selected execution or reuse path")
    required.add(selected)
    if any(mapping(value.get(name, {})).get("result") != "success" for name in required):
        raise ValueError(
            "live summary requires current successful prerequisites and selected evidence"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "permit", "identity", "reuse", "summary"])
    parser.add_argument("--source-sha", default=os.environ.get("SOURCE_COMMIT"))
    parser.add_argument("--base-sha", default=os.environ.get("BASE_SHA"))
    parser.add_argument("--data-dir", type=Path, default=Path("eval"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--permit-file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "permit":
            protected_environment()
            permit = LivePermit.model_validate_json(os.environ.get("LIVE_EVAL_PERMIT", ""))
            permit.validate_context(
                repository=os.environ["GITHUB_REPOSITORY"],
                source_sha=args.source_sha,
                workflow=WORKFLOW,
                run_id=os.environ["GITHUB_RUN_ID"],
                run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
            )
            phase, baseline_digest = validate_baseline_phase(
                args.data_dir, args.base_sha, regression=permit.purpose == "regression"
            )
            permit.validate_phase(phase, baseline_digest)
            if not os.environ.get("ANTHROPIC_API_KEY") or args.permit_file is None:
                raise ValueError("required environment secret or permit output is missing")
            args.permit_file.write_text(permit.model_dump_json())
            args.permit_file.chmod(0o600)
            output("batch-uuid", str(permit.batch_uuid))
        elif args.command == "identity":
            if args.output_dir is None:
                raise ValueError("output directory is required")
            path = args.output_dir / "manifest.json"
            if path.exists():
                digest = sha256(path.read_bytes()).hexdigest()
                output("manifest-sha256", digest)
                print(f"LIVE_MANIFEST_SHA256={digest}")
        else:
            plan = make_plan(
                source=args.source_sha,
                base=args.base_sha,
                event=os.environ["GITHUB_EVENT_NAME"],
                repository=os.environ["GITHUB_REPOSITORY"],
                head_repository=os.environ.get("HEAD_REPOSITORY", os.environ["GITHUB_REPOSITORY"]),
                run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
                data_dir=args.data_dir,
            )
            if args.command == "plan":
                output("mode", string(plan["mode"]))
                output("plan", json.dumps(plan, sort_keys=True, separators=(",", ":")))
                print(json.dumps(plan, sort_keys=True))
            elif args.command == "reuse":
                if plan["mode"] != "reuse" or args.output_dir is None:
                    raise ValueError("reuse requires eligible retained evidence")
                download_artifact(plan, args.output_dir)
                verify_live(plan, args.output_dir, args.data_dir)
                output("plan", json.dumps(plan, sort_keys=True, separators=(",", ":")))
            else:
                needs = mapping(json.loads(os.environ["PREREQUISITES"]))
                original_plan = mapping(json.loads(os.environ["SELECTED_PLAN"]))
                require_prerequisites(needs, string(original_plan["mode"]))
                if original_plan["mode"] == "execute":
                    produced = mapping(mapping(needs["live-eval-execute"])["outputs"])
                    if (
                        original_plan.get("checked_sha") != args.source_sha
                        or plan.get("producer_run_id") != os.environ["GITHUB_RUN_ID"]
                        or plan.get("batch_uuid") != produced.get("batch-uuid")
                        or plan.get("manifest_sha256") != produced.get("manifest-sha256")
                    ):
                        raise ValueError("selected producer outputs do not match retained evidence")
                if plan["mode"] != "reuse" or args.output_dir is None:
                    raise ValueError("summary requires a retained completed producer artifact")
                if original_plan["mode"] == "reuse" and original_plan != plan:
                    raise ValueError("selected reuse provenance changed during this run")
                download_artifact(plan, args.output_dir)
                verify_live(plan, args.output_dir, args.data_dir)
                print(json.dumps(plan, sort_keys=True))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile):
        # Never print raw API, validation or secret-bearing exception payloads.
        parser.exit(1, "Live evidence or protected execution validation failed.\n")


def verify_live(plan: Mapping[str, object], directory: Path, data_dir: Path) -> None:
    command(
        sys.executable,
        "-m",
        "rent_navigator.eval",
        "verify-live",
        "--data-dir",
        str(data_dir),
        "--output-dir",
        str(directory),
        "--source-sha",
        string(plan["scored_sha"]),
        "--manifest-sha256",
        string(plan["manifest_sha256"]),
        "--producer-run-id",
        string(plan["producer_run_id"]),
        "--producer-run-attempt",
        "1",
        "--batch-uuid",
        string(plan["batch_uuid"]),
        "--lockfile",
        "uv.lock",
    )


if __name__ == "__main__":
    main()
