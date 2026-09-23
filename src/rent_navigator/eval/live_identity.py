"""Pure provenance and Git source proofs, independent of GitHub transport."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

REPOSITORY = "angelaqaaa/rent-navigator"
WORKFLOW = ".github/workflows/ci.yml"
REQUIRED_CHECKS = frozenset({"checks", "docker", "offline-eval", "live-eval-gate"})
ATTACHMENT_PATHS = frozenset({"eval/baseline.json", "eval/activation.json"})


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    tree: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class BaselineReference:
    source_sha: str
    batch_uuid: str
    manifest_sha256: str


@dataclass(frozen=True)
class MergeProof:
    repository: str
    merge_sha: str
    head_sha: str
    merged: bool
    base_branch: str
    accepted_checks: frozenset[str]


@dataclass(frozen=True)
class SourceRelationship:
    checked_sha: str
    scored_sha: str
    kind: Literal["exact", "bootstrap_attachment", "docs", "main_merge"]
    normalized_sha: str
    underlying: str


def changed_paths(before: Commit, after: Commit) -> frozenset[str]:
    return frozenset(
        path
        for path in before.files.keys() | after.files.keys()
        if before.files.get(path) != after.files.get(path)
    )


def is_documentation(path: str) -> bool:
    return path in {"README.md", "LICENSE", "NOTICE"} or (
        path.startswith("results/") and not any(part in {".", ".."} for part in path.split("/"))
    )


def same_behavior(before: Commit, after: Commit) -> bool:
    """Only the explicit documentation whitelist is ignored; configuration hashes are irrelevant."""
    return all(is_documentation(path) for path in changed_paths(before, after))


def _merge_parent(graph: Mapping[str, Commit], source: str, proof: MergeProof) -> str:
    node = graph[source]
    if (
        proof.repository != REPOSITORY
        or not proof.merged
        or proof.base_branch != "main"
        or proof.merge_sha != source
        or len(node.parents) != 2
        or proof.head_sha != node.parents[1]
        or proof.head_sha not in graph
        or graph[proof.head_sha].tree != node.tree
        or dict(graph[proof.head_sha].files) != dict(node.files)
        or proof.accepted_checks != REQUIRED_CHECKS
    ):
        raise ValueError("main source is not an accepted tree-identical PR merge")
    return proof.head_sha


def _chain(
    graph: Mapping[str, Commit],
    head: str,
    ancestor: str,
    merges: Mapping[str, MergeProof],
) -> tuple[Commit, ...]:
    """Follow parents, normalizing only independently proved GitHub merge wrappers."""
    chain: list[Commit] = []
    seen: set[str] = set()
    while head != ancestor:
        if head in seen or head not in graph:
            raise ValueError("source proof has no complete ancestral chain")
        seen.add(head)
        commit = graph[head]
        chain.append(commit)
        if len(commit.parents) == 1:
            head = commit.parents[0]
        elif head in merges:
            head = _merge_parent(graph, head, merges[head])
        else:
            raise ValueError("source proof cannot cross an unverified merge")
    if ancestor not in graph:
        raise ValueError("source ancestor is missing")
    chain.append(graph[ancestor])
    return tuple(chain)


def _docs_chain(
    graph: Mapping[str, Commit],
    head: str,
    ancestor: str,
    merges: Mapping[str, MergeProof],
) -> None:
    chain = _chain(graph, head, ancestor, merges)
    for after, before in zip(chain, chain[1:], strict=False):
        if not all(is_documentation(path) for path in changed_paths(before, after)):
            raise ValueError("documentation exemption crosses an untested behavior change")
    if not all(is_documentation(path) for path in changed_paths(graph[ancestor], graph[head])):
        raise ValueError("documentation exemption changed nonexempt bytes")


def prove_source_relationship(
    *,
    graph: Mapping[str, Commit],
    current_sha: str,
    scored_sha: str,
    trusted_base_sha: str,
    batch_uuid: str,
    manifest_sha256: str,
    baseline: BaselineReference | None = None,
    passing_ancestor: str | None = None,
    ancestor_checks: frozenset[str] = frozenset(),
    merge: MergeProof | None = None,
    ancestral_merges: Mapping[str, MergeProof] | None = None,
    accepted_ancestors: Mapping[str, frozenset[str]] | None = None,
) -> SourceRelationship:
    """Accept only exact, strict attachment, byte-preserving docs or a proven merge."""
    checked = current_sha
    merges = dict(ancestral_merges or {})
    if merge is not None:
        merges[current_sha] = merge
    accepted = dict(accepted_ancestors or {})
    if passing_ancestor is not None:
        accepted[passing_ancestor] = ancestor_checks
    visited: set[str] = set()

    def resolve(sha: str, *, top: bool) -> tuple[str, str]:
        if sha in visited or sha not in graph or scored_sha not in graph:
            raise ValueError("source relationship is cyclic or missing")
        visited.add(sha)
        if sha == scored_sha:
            return "exact", sha
        node = graph[sha]
        if (
            node.parents == (scored_sha,)
            and ATTACHMENT_PATHS <= node.files.keys()
            and changed_paths(graph[scored_sha], node) == ATTACHMENT_PATHS
            and baseline == BaselineReference(scored_sha, batch_uuid, manifest_sha256)
        ):
            return "bootstrap_attachment", sha
        if sha in merges:
            parent = _merge_parent(graph, sha, merges[sha])
            underlying, _ = resolve(parent, top=top)
            return "main_merge:" + underlying, parent
        # Every intervening edge must be documentation-only. The first accepted
        # checked ancestor is selected before recursively proving its paid origin.
        cursor = sha
        while True:
            item = graph[cursor]
            if len(item.parents) != 1:
                raise ValueError("documentation has no nearest accepted checked ancestor")
            parent = item.parents[0]
            if parent not in graph or not all(
                is_documentation(path) for path in changed_paths(graph[parent], item)
            ):
                raise ValueError("documentation exemption crosses an untested behavior change")
            cursor = parent
            if accepted.get(cursor) == REQUIRED_CHECKS:
                break
        if top:
            _docs_chain(graph, sha, trusted_base_sha, merges)
        _docs_chain(graph, sha, cursor, merges)
        resolve(cursor, top=False)
        return "docs", sha

    kind, normalized = resolve(current_sha, top=True)
    underlying = kind.removeprefix("main_merge:")
    public_kind = "main_merge" if kind.startswith("main_merge:") else kind
    return SourceRelationship(
        checked_sha=checked,
        scored_sha=scored_sha,
        kind=public_kind,  # type: ignore[arg-type]
        normalized_sha=normalized,
        underlying=underlying,
    )


@dataclass(frozen=True)
class ArtifactOrigin:
    artifact_id: int
    name: str
    repository: str
    source_sha: str
    batch_uuid: str
    workflow: str
    event: str
    run_id: str
    run_attempt: int
    producer_conclusion: str
    producer_manifest_sha256: str
    archive_sha256: str
    expired: bool
    expires_at: datetime
    trusted_branch: bool
    summary_conclusion: str = "success"


def select_evidence(
    candidates: Sequence[ArtifactOrigin],
    *,
    source_sha: str,
    batch_uuid: str | None,
    now: datetime,
    current_run_id: str | None = None,
    current_run_attempt: int | None = None,
) -> ArtifactOrigin:
    """An unchanged source's attempt history cannot be filtered for a lucky pass."""
    history = [candidate for candidate in candidates if candidate.source_sha == source_sha]
    if len(history) != 1:
        raise ValueError("live evidence is missing or has ambiguous scored attempt history")
    artifact = history[0]
    current_producer = artifact.run_id == current_run_id and current_run_attempt == 1
    if (
        (batch_uuid is not None and artifact.batch_uuid != batch_uuid)
        or artifact.repository != REPOSITORY
        or artifact.workflow != WORKFLOW
        or artifact.event != "pull_request"
        or not artifact.trusted_branch
        or artifact.run_attempt != 1
        or artifact.producer_conclusion != "success"
        or artifact.name != f"live-{source_sha}-{artifact.batch_uuid}"
        or (not current_producer and artifact.summary_conclusion != "success")
        or artifact.expired
        or artifact.expires_at <= now
        or re.fullmatch(r"[0-9a-f]{64}", artifact.producer_manifest_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", artifact.archive_sha256) is None
    ):
        raise ValueError("live evidence origin, success or retention is invalid")
    return artifact


def paid_execution_allowed(
    *,
    repository: str,
    head_repository: str,
    event: str,
    run_attempt: int,
    has_history: bool,
    baseline_phase: str,
) -> bool:
    """A protected first attempt may wait for a permit; nothing authorizes a retry."""
    return (
        repository == REPOSITORY == head_repository
        and event == "pull_request"
        and run_attempt == 1
        and not has_history
        and baseline_phase in {"pending", "active"}
    )
