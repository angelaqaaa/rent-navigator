"""Approved offline execution and exact artifact verification; no provider calls."""

import json
import subprocess
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, TypeAdapter, model_validator

from rent_navigator.corpus import Corpus
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.metrics import retrieval_scores
from rent_navigator.eval.models import CASE_IDS, Activation, CaseId
from rent_navigator.eval.security import SecurityInventory, parse_security_report
from rent_navigator.index import build_index, search
from rent_navigator.models import (
    CanonicalUUID,
    NoticeRequest,
    RentRequest,
    Sha256,
    SourceCommit,
    StrictModel,
    ToolResult,
)
from rent_navigator.notice import notice_deadline_check
from rent_navigator.rent import rent_increase_check
from rent_navigator.security_cases import security_cases_hash
from rent_navigator.trace import PRICING_HASH

Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_PREREQUISITES = {"checks", "docker", "offline-eval-run"}
_PAYLOADS = {"report.json", "security.xml"}


def _strict_integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("Integer metadata cannot be coerced")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_strict_integer)]


def _true(value: object) -> object:
    if value is not True:
        raise ValueError("Schema validation metadata must be true")
    return value


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def configuration_identity_hash(
    protocol_hash: str, configuration_map: object, judge_config: object | None
) -> str:
    return canonical_hash(
        {
            "protocol_hash": protocol_hash,
            "serving_config": configuration_map,
            "judge_config_hash": judge_config,
        }
    )


class ExactCase(StrictModel):
    id: CaseId
    kind: Literal["rent", "notice", "qa"]
    schema_valid: Annotated[Literal[True], BeforeValidator(_true)]
    actual_tool_result: ToolResult | None
    exact_match: bool


class Ranking(StrictModel):
    mrr_at_5: Score
    ndcg_at_5: Score


class RetrievalReport(Ranking):
    qa_count: Annotated[Literal[6], BeforeValidator(_strict_integer)]
    cases: dict[CaseId, Ranking]


class Baseline(StrictModel):
    """Deterministic comparison component; live bootstrap composition belongs to WP9."""

    schema_version: SchemaVersion
    source_sha: SourceCommit
    corpus_hash: Sha256
    gold_hash: Sha256
    config_hash: Sha256
    mrr_at_5: Score
    ndcg_at_5: Score
    B: Annotated[int, Field(ge=28, le=32)]
    bootstrap_run_id: CanonicalUUID
    bootstrap_manifest_sha256: Sha256


class OfflineReport(StrictModel):
    schema_version: SchemaVersion
    source_sha: SourceCommit
    gold_hash: Sha256
    approval_hash: Sha256
    corpus_hash: Sha256
    activation_hash: Sha256
    baseline_hash: Sha256 | None
    config_hash: Sha256
    serving_config: dict[str, Sha256]
    judge_config_hash: None
    protocol_hash: Sha256
    security_hash: Sha256
    pricing_hash: Sha256
    lock_hash: Sha256
    baseline_phase: Literal["pending", "active"]
    baseline_comparison: Literal["pending", "passed", "failed"]
    case_results: list[ExactCase]
    retrieved_ids: dict[CaseId, list[Sha256]]
    production_retrieval: RetrievalReport
    baseline_retrieval: RetrievalReport
    security: SecurityInventory
    offline_complete: bool

    @model_validator(mode="after")
    def complete_coverage(self) -> "OfflineReport":
        if self.config_hash != configuration_identity_hash(
            self.protocol_hash, self.serving_config, self.judge_config_hash
        ):
            raise ValueError("Offline configuration identity does not match its map")
        if tuple(result.id for result in self.case_results) != CASE_IDS:
            raise ValueError("Offline report must cover all sixteen cases once in order")
        qa_ids = set(CASE_IDS[10:])
        if any(
            set(mapping) != qa_ids
            for mapping in (
                self.retrieved_ids,
                self.production_retrieval.cases,
                self.baseline_retrieval.cases,
            )
        ):
            raise ValueError("Offline retrieval report must cover all six questions")
        if any(len(ids) > 5 or len(ids) != len(set(ids)) for ids in self.retrieved_ids.values()):
            raise ValueError("Offline ranked evidence must be unique and at most five")
        if self.baseline_phase == "pending":
            if self.baseline_comparison != "pending" or self.baseline_hash is not None:
                raise ValueError("Pending comparison cannot claim an active baseline")
        elif self.baseline_comparison == "pending" or self.baseline_hash is None:
            raise ValueError("Active comparison requires a baseline")
        complete = (
            all(case.exact_match for case in self.case_results)
            and self.baseline_comparison != "failed"
        )
        if self.offline_complete != complete:
            raise ValueError("Offline completeness must follow every exact result")
        return self


class OfflineManifest(StrictModel):
    schema_version: SchemaVersion
    source_sha: SourceCommit
    gold_hash: Sha256
    approval_hash: Sha256
    corpus_hash: Sha256
    config_hash: Sha256
    serving_config: dict[str, Sha256]
    judge_config_hash: None
    protocol_hash: Sha256
    security_hash: Sha256
    pricing_hash: Sha256
    activation_hash: Sha256
    baseline_hash: Sha256 | None
    lock_hash: Sha256
    offline_complete: bool
    payload_sha256: dict[str, Sha256]

    @model_validator(mode="after")
    def required_payloads(self) -> "OfflineManifest":
        if set(self.payload_sha256) != _PAYLOADS:
            raise ValueError("Offline manifest must hash exactly the required payloads")
        if self.config_hash != configuration_identity_hash(
            self.protocol_hash, self.serving_config, self.judge_config_hash
        ):
            raise ValueError("Offline manifest configuration identity does not match its map")
        return self


def _configuration_hash() -> str:
    from rent_navigator.eval.runner import config_map, protocol_hash

    return configuration_identity_hash(protocol_hash(), config_map(), None)


def check_activation(current: Activation, previous: Activation | None) -> None:
    if (
        previous is not None
        and previous.baseline_phase == "active"
        and current.baseline_phase != "active"
    ):
        raise ValueError("Active baseline cannot return to pending")


def activation_at_revision(repository: Path, revision: str) -> Activation | None:
    """Read parsed base state from an exact commit without interpreting artifact text."""
    revision = TypeAdapter(SourceCommit).validate_python(revision)
    subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", revision, "eval/activation.json"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    if not listing.stdout.strip():
        return None
    content = subprocess.run(
        ["git", "show", f"{revision}:eval/activation.json"],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    return Activation.model_validate_json(content)


def _report(
    dataset: GoldDataset,
    data_dir: Path,
    corpus: Corpus,
    source_sha: str,
    security: SecurityInventory,
    lock_path: Path,
) -> OfflineReport:
    from rent_navigator.eval.runner import config_map, protocol_hash

    case_results = []
    for case in dataset.cases:
        actual = None
        if isinstance(case.request, RentRequest):
            actual = rent_increase_check(case.request.facts, corpus=corpus)
        elif isinstance(case.request, NoticeRequest):
            actual = notice_deadline_check(case.request.facts, corpus=corpus)
        case_results.append(
            ExactCase(
                id=case.id,
                kind=case.kind,
                schema_valid=True,
                actual_tool_result=actual,
                exact_match=actual == case.expected_tool_result,
            )
        )
    ranked: dict[CaseId, list[str]] = {}
    with TemporaryDirectory(prefix="rent-navigator-offline-") as directory:
        database = Path(directory) / "index.sqlite3"
        build_index(database, corpus)
        for case in dataset.cases:
            if case.request.mode == "question":
                ranked[case.id] = [
                    hit.chunk.id
                    for hit in search(
                        database, case.request.question, expected_corpus_hash=corpus.corpus_hash
                    )
                ]
    evidence = {chunk.id for chunk in corpus.chunks}
    production = RetrievalReport.model_validate(
        asdict(
            retrieval_scores(
                dataset.cases,
                {str(key): value for key, value in ranked.items()},
                evidence_ids=evidence,
            )
        )
    )
    baseline = RetrievalReport.model_validate(
        asdict(retrieval_scores(dataset.cases, {}, evidence_ids=evidence))
    )
    config_hash = _configuration_hash()
    comparison: Literal["pending", "passed", "failed"] = "pending"
    baseline_hash = None
    if dataset.activation.baseline_phase == "active":
        baseline_path = data_dir / "baseline.json"
        frozen = Baseline.model_validate_json(baseline_path.read_bytes())
        if (frozen.corpus_hash, frozen.gold_hash) != (corpus.corpus_hash, dataset.gold_hash):
            raise ValueError("Frozen retrieval baseline hashes do not match current inputs")
        comparison = (
            "passed"
            if production.mrr_at_5 + 1e-12 >= frozen.mrr_at_5
            and production.ndcg_at_5 + 1e-12 >= frozen.ndcg_at_5
            else "failed"
        )
        baseline_hash = file_hash(baseline_path)
    return OfflineReport(
        schema_version=1,
        source_sha=source_sha,
        gold_hash=dataset.gold_hash,
        approval_hash=dataset.approval_hash,
        corpus_hash=corpus.corpus_hash,
        activation_hash=file_hash(data_dir / "activation.json"),
        baseline_hash=baseline_hash,
        config_hash=config_hash,
        serving_config=config_map(),
        judge_config_hash=None,
        protocol_hash=protocol_hash(),
        security_hash=security_cases_hash(),
        pricing_hash=PRICING_HASH,
        lock_hash=file_hash(lock_path),
        baseline_phase=dataset.activation.baseline_phase,
        baseline_comparison=comparison,
        case_results=case_results,
        retrieved_ids=ranked,
        production_retrieval=production,
        baseline_retrieval=baseline,
        security=security,
        offline_complete=all(case.exact_match for case in case_results) and comparison != "failed",
    )


def _manifest(report: OfflineReport, output_dir: Path) -> OfflineManifest:
    fields = {
        name: getattr(report, name)
        for name in OfflineManifest.model_fields
        if name != "payload_sha256"
    }
    fields["payload_sha256"] = {name: file_hash(output_dir / name) for name in sorted(_PAYLOADS)}
    return OfflineManifest.model_validate(fields)


def run_offline(
    data_dir: Path,
    output_dir: Path,
    *,
    corpus: Corpus,
    source_sha: str,
    security_report: Path,
    lock_path: Path,
    previous_activation: Activation | None = None,
) -> OfflineManifest:
    """Reserve a fresh artifact directory and fail approval before any execution."""
    TypeAdapter(SourceCommit).validate_python(source_sha)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        (output_dir / "security.xml").write_bytes(security_report.read_bytes())
        dataset = load_gold(data_dir, corpus, require_approved=True)
        check_activation(dataset.activation, previous_activation)
        security = parse_security_report(output_dir / "security.xml")
        report = _report(dataset, data_dir, corpus, source_sha, security, lock_path)
        (output_dir / "report.json").write_text(report.model_dump_json(indent=2) + "\n")
        manifest = _manifest(report, output_dir)
        (output_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
        if not manifest.offline_complete:
            raise ValueError("Offline exact checks or baseline comparison failed")
        return manifest
    except (ValueError, OSError):
        (output_dir / "failure.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": source_sha,
                    "offline_complete": False,
                    "reason": "approval_or_offline_acceptance_failed",
                }
            )
            + "\n"
        )
        raise


def verify_offline(
    data_dir: Path,
    output_dir: Path,
    *,
    corpus: Corpus,
    source_sha: str,
    expected_manifest_hash: str,
    prerequisites: dict[str, str],
    lock_path: Path,
    previous_activation: Activation | None = None,
) -> OfflineManifest:
    """Require current prerequisites, exact payload hashes, and reproducible results."""
    if set(prerequisites) != _PREREQUISITES or any(
        value != "success" for value in prerequisites.values()
    ):
        raise ValueError("All exact prerequisite jobs must succeed")
    TypeAdapter(SourceCommit).validate_python(source_sha)
    TypeAdapter(Sha256).validate_python(expected_manifest_hash)
    if file_hash(output_dir / "manifest.json") != expected_manifest_hash:
        raise ValueError("Producer manifest digest mismatch")
    manifest = OfflineManifest.model_validate_json((output_dir / "manifest.json").read_bytes())
    if manifest.source_sha != source_sha or not manifest.offline_complete:
        raise ValueError("Offline artifact is stale or incomplete")
    if set(path.name for path in output_dir.iterdir()) != _PAYLOADS | {"manifest.json"}:
        raise ValueError("Offline artifact file inventory is incomplete or unexpected")
    for name, expected_digest in manifest.payload_sha256.items():
        path = output_dir / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != expected_digest:
            raise ValueError("Offline payload digest mismatch")
    dataset = load_gold(data_dir, corpus, require_approved=True)
    check_activation(dataset.activation, previous_activation)
    security = parse_security_report(output_dir / "security.xml")
    actual = OfflineReport.model_validate_json((output_dir / "report.json").read_bytes())
    expected = _report(dataset, data_dir, corpus, source_sha, security, lock_path)
    if (
        actual != expected
        or manifest != _manifest(expected, output_dir)
        or not expected.offline_complete
    ):
        raise ValueError("Offline report does not match current exact results and identities")
    return manifest
