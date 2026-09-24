"""Synthetic source graphs and GitHub metadata exercise fail-closed reuse policy."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rent_navigator.eval.live_identity import (
    REPOSITORY,
    REQUIRED_CHECKS,
    WORKFLOW,
    ArtifactOrigin,
    BaselineReference,
    Commit,
    MergeProof,
    is_documentation,
    paid_execution_allowed,
    prove_source_relationship,
    select_evidence,
)

S, A, D, M, BASE = (letter * 40 for letter in "abcde")
BATCH = "00000000-0000-0000-0000-000000000019"
DIGEST = "f" * 64
NOW = datetime(2026, 9, 23, tzinfo=UTC)


def graph() -> dict[str, Commit]:
    source = {"src/app.py": "logic", "README.md": "before", "eval/activation.json": "pending"}
    attached = {**source, "eval/baseline.json": "baseline", "eval/activation.json": "active"}
    return {
        BASE: Commit(BASE, (), "old", {}),
        S: Commit(S, (BASE,), "source-tree", source),
        A: Commit(A, (S,), "attachment-tree", attached),
        D: Commit(D, (A,), "docs-tree", {**attached, "README.md": "after"}),
        M: Commit(M, (BASE, A), "attachment-tree", attached),
    }


def prove(nodes: dict[str, Commit], current: str, **kwargs: Any) -> Any:
    return prove_source_relationship(
        graph=nodes,
        current_sha=current,
        scored_sha=S,
        trusted_base_sha=A,
        batch_uuid=BATCH,
        manifest_sha256=DIGEST,
        baseline=kwargs.pop("baseline", BaselineReference(S, BATCH, DIGEST)),
        **kwargs,
    )


def test_exact_reuse_does_not_need_a_new_batch() -> None:
    assert prove(graph(), S).kind == "exact"


def test_attachment_is_exactly_two_files_and_one_parent() -> None:
    assert prove(graph(), A).kind == "bootstrap_attachment"
    for changed in (
        {**graph()[A].files, "README.md": "changed"},
        {**graph()[A].files, "src/app.py": "different"},
        {"src/app.py": "logic", "README.md": "before", "eval/baseline.json": "baseline"},
    ):
        nodes = graph()
        nodes[A] = replace(nodes[A], files=changed)
        with pytest.raises(ValueError):
            prove(nodes, A)
    nodes = graph()
    nodes[A] = replace(nodes[A], parents=(BASE, S))
    with pytest.raises(ValueError):
        prove(nodes, A)


@pytest.mark.parametrize("field", ["source_sha", "batch_uuid", "manifest_sha256"])
def test_attachment_must_name_the_actual_retained_evidence(field: str) -> None:
    fields = {"source_sha": S, "batch_uuid": BATCH, "manifest_sha256": DIGEST}
    fields[field] = "wrong"
    with pytest.raises(ValueError):
        prove(graph(), A, baseline=BaselineReference(**fields))


def test_docs_require_trusted_base_and_passing_ancestor() -> None:
    assert prove(graph(), D, passing_ancestor=A, ancestor_checks=REQUIRED_CHECKS).kind == "docs"
    with pytest.raises(ValueError):
        prove(graph(), D, passing_ancestor=A)
    with pytest.raises(ValueError):
        prove_source_relationship(
            graph=graph(),
            current_sha=D,
            scored_sha=S,
            trusted_base_sha=BASE,
            batch_uuid=BATCH,
            manifest_sha256=DIGEST,
            passing_ancestor=S,
            ancestor_checks=REQUIRED_CHECKS,
        )


def test_docs_cannot_launder_reverted_behavior_change() -> None:
    nodes = graph()
    bad = "f" * 40
    nodes[bad] = Commit(bad, (A,), "bad", {**nodes[A].files, "src/app.py": "untested"})
    nodes[D] = replace(nodes[D], parents=(bad,))
    with pytest.raises(ValueError, match="untested behavior"):
        prove(nodes, D, passing_ancestor=A, ancestor_checks=REQUIRED_CHECKS)


@pytest.mark.parametrize(
    "path",
    [
        "PROJECT-SPEC.md",
        "CONTRACTS.md",
        "uv.lock",
        "tests/a.py",
        "eval/baseline.json",
        "results.py",
        "results/../src/app.py",
        ".github/workflows/ci.yml",
    ],
)
def test_nonexempt_paths_cannot_be_called_docs(path: str) -> None:
    assert not is_documentation(path)
    nodes = graph()
    nodes[D] = replace(nodes[D], files={**nodes[D].files, path: "change"})
    with pytest.raises(ValueError):
        prove(nodes, D, passing_ancestor=A, ancestor_checks=REQUIRED_CHECKS)


def merge() -> MergeProof:
    return MergeProof(REPOSITORY, M, A, True, "main", REQUIRED_CHECKS)


def test_real_merge_preserves_checked_and_historical_scored_identity() -> None:
    proof = prove(graph(), M, merge=merge())
    assert (proof.kind, proof.checked_sha, proof.normalized_sha, proof.scored_sha) == (
        "main_merge",
        M,
        A,
        S,
    )
    assert proof.underlying == "bootstrap_attachment"


@pytest.mark.parametrize(
    "change",
    [
        dict(merged=False),
        dict(repository="fork/repo"),
        dict(base_branch="other"),
        dict(head_sha=BASE),
        dict(accepted_checks=frozenset({"checks"})),
        dict(merge_sha=D),
    ],
)
def test_merge_requires_github_association_and_all_accepted_checks(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        prove(graph(), M, merge=replace(merge(), **change))


@pytest.mark.parametrize("parents", [(A,), (BASE, S), (BASE, A, S)])
def test_squash_rebase_and_other_parentage_do_not_qualify(parents: tuple[str, ...]) -> None:
    nodes = graph()
    nodes[M] = replace(nodes[M], parents=parents)
    with pytest.raises(ValueError):
        prove(nodes, M, merge=merge())


def test_merge_resolution_bytes_must_not_differ() -> None:
    nodes = graph()
    nodes[M] = replace(nodes[M], files={**nodes[M].files, "src/app.py": "resolution"})
    with pytest.raises(ValueError):
        prove(nodes, M, merge=merge())


def test_docs_after_accepted_main_wrapper_can_reach_the_attachment() -> None:
    nodes = graph()
    nodes[D] = replace(nodes[D], parents=(M,))
    assert (
        prove_source_relationship(
            graph=nodes,
            current_sha=D,
            scored_sha=S,
            trusted_base_sha=M,
            batch_uuid=BATCH,
            manifest_sha256=DIGEST,
            baseline=BaselineReference(S, BATCH, DIGEST),
            passing_ancestor=A,
            ancestor_checks=REQUIRED_CHECKS,
            ancestral_merges={M: merge()},
            accepted_ancestors={M: REQUIRED_CHECKS},
        ).kind
        == "docs"
    )
    with pytest.raises(ValueError):
        prove_source_relationship(
            graph=nodes,
            current_sha=D,
            scored_sha=S,
            trusted_base_sha=M,
            batch_uuid=BATCH,
            manifest_sha256=DIGEST,
            baseline=BaselineReference(S, BATCH, DIGEST),
            passing_ancestor=A,
            ancestor_checks=REQUIRED_CHECKS,
        )


def artifact() -> ArtifactOrigin:
    return ArtifactOrigin(
        artifact_id=1,
        name=f"live-{S}-{BATCH}",
        repository=REPOSITORY,
        source_sha=S,
        batch_uuid=BATCH,
        workflow=WORKFLOW,
        event="pull_request",
        run_id="100",
        run_attempt=1,
        producer_conclusion="success",
        producer_manifest_sha256=DIGEST,
        archive_sha256="a" * 64,
        expired=False,
        expires_at=NOW + timedelta(days=90),
        trusted_branch=True,
    )


def test_single_complete_origin_is_selectable() -> None:
    assert select_evidence([artifact()], source_sha=S, batch_uuid=BATCH, now=NOW) == artifact()


@pytest.mark.parametrize(
    "change",
    [
        dict(repository="fork/repo"),
        dict(workflow="other.yml"),
        dict(event="push"),
        dict(run_attempt=2),
        dict(producer_conclusion="failure"),
        dict(expired=True),
        dict(expires_at=NOW),
        dict(trusted_branch=False),
        dict(name="misleading"),
        dict(producer_manifest_sha256=""),
        dict(archive_sha256=""),
    ],
)
def test_artifact_names_alone_never_establish_trust(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        select_evidence([replace(artifact(), **change)], source_sha=S, batch_uuid=BATCH, now=NOW)


def test_failure_history_cannot_be_filtered_for_later_lucky_success() -> None:
    failed = replace(artifact(), producer_conclusion="failure", artifact_id=2)
    with pytest.raises(ValueError, match="ambiguous"):
        select_evidence([failed, artifact()], source_sha=S, batch_uuid=BATCH, now=NOW)
    with pytest.raises(ValueError):
        select_evidence([], source_sha=S, batch_uuid=BATCH, now=NOW)
    with pytest.raises(ValueError):
        select_evidence([artifact()], source_sha=S, batch_uuid="wrong", now=NOW)


def test_only_first_trusted_pending_bootstrap_may_wait_for_a_permit() -> None:
    fields: dict[str, Any] = dict(
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        event="pull_request",
        run_attempt=1,
        has_history=False,
        baseline_phase="pending",
    )
    assert paid_execution_allowed(**fields)
    for change in (
        dict(head_repository="fork/repo"),
        dict(repository="fork/repo"),
        dict(event="push"),
        dict(run_attempt=2),
        dict(has_history=True),
        dict(baseline_phase="unknown"),
    ):
        assert not paid_execution_allowed(**{**fields, **change})


def test_historical_summary_is_required_but_current_producer_has_no_circular_dependency() -> None:
    waiting = replace(artifact(), summary_conclusion="incomplete")
    with pytest.raises(ValueError):
        select_evidence([waiting], source_sha=S, batch_uuid=BATCH, now=NOW)
    assert (
        select_evidence(
            [waiting],
            source_sha=S,
            batch_uuid=BATCH,
            now=NOW,
            current_run_id="100",
            current_run_attempt=1,
        )
        == waiting
    )
    with pytest.raises(ValueError):
        select_evidence(
            [waiting],
            source_sha=S,
            batch_uuid=BATCH,
            now=NOW,
            current_run_id="100",
            current_run_attempt=2,
        )


def test_docs_resolve_through_the_nearest_accepted_docs_ancestor() -> None:
    nodes = graph()
    nodes[D] = replace(nodes[D], parents=(M,))
    newer = "f" * 40
    nodes[newer] = Commit(newer, (D,), "new-docs", {**nodes[D].files, "NOTICE": "notice"})
    proof = prove_source_relationship(
        graph=nodes,
        current_sha=newer,
        scored_sha=S,
        trusted_base_sha=M,
        batch_uuid=BATCH,
        manifest_sha256=DIGEST,
        baseline=BaselineReference(S, BATCH, DIGEST),
        ancestral_merges={M: merge()},
        accepted_ancestors={M: REQUIRED_CHECKS, D: REQUIRED_CHECKS},
    )
    assert proof.kind == "docs" and proof.scored_sha == S
    nodes[D] = replace(nodes[D], files={**nodes[D].files, "src/app.py": "untested"})
    # A green checked ancestor is insufficient if its recursive behavior proof fails.
    nodes[newer] = replace(nodes[newer], files={**nodes[newer].files, "src/app.py": "untested"})
    with pytest.raises(ValueError):
        prove_source_relationship(
            graph=nodes,
            current_sha=newer,
            scored_sha=S,
            trusted_base_sha=D,
            batch_uuid=BATCH,
            manifest_sha256=DIGEST,
            baseline=BaselineReference(S, BATCH, DIGEST),
            ancestral_merges={M: merge()},
            accepted_ancestors={M: REQUIRED_CHECKS, D: REQUIRED_CHECKS},
        )
