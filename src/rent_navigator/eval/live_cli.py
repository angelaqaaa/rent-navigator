"""Explicit protected execution entry; validate all authority before client creation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.live import run_live_gate, verify_live_gate
from rent_navigator.eval.offline import file_hash, verify_offline
from rent_navigator.eval.permit import LivePermit
from rent_navigator.index import build_index, search
from rent_navigator.provider import MessagesPort, create_client


def add_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for name in ("live-gate", "verify-live"):
        child = commands.add_parser(name)
        child.add_argument("--data-dir", type=Path, required=True)
        child.add_argument("--output-dir", type=Path, required=True)
        child.add_argument("--source-sha", required=True)
        child.add_argument("--lockfile", type=Path, default=Path("uv.lock"))
        if name == "verify-live":
            child.add_argument("--manifest-sha256", required=True)
            child.add_argument("--producer-run-id", required=True)
            child.add_argument("--producer-run-attempt", type=int, required=True)
            child.add_argument("--batch-uuid", type=UUID, required=True)
        else:
            child.add_argument("--offline-dir", type=Path, required=True)
            child.add_argument("--permit-file", type=Path, required=True)
            child.add_argument("--repository", required=True)
            child.add_argument("--workflow", required=True)
            child.add_argument("--run-id", required=True)
            child.add_argument("--run-attempt", type=int, required=True)


def live_command(arguments: argparse.Namespace) -> None:
    corpus = load_corpus()
    dataset = load_gold(arguments.data_dir, corpus)
    if arguments.command == "verify-live":
        manifest = verify_live_gate(
            arguments.output_dir,
            data_dir=arguments.data_dir,
            dataset=dataset,
            corpus=corpus,
            source_sha=arguments.source_sha,
            lock_path=arguments.lockfile,
            manifest_sha256=arguments.manifest_sha256,
            expected_run_id=arguments.producer_run_id,
            expected_run_attempt=arguments.producer_run_attempt,
            expected_batch_uuid=arguments.batch_uuid,
        )
        print(manifest.model_dump_json())
        return
    permit = LivePermit.model_validate_json(arguments.permit_file.read_bytes())
    permit.validate_context(
        repository=arguments.repository,
        source_sha=arguments.source_sha,
        workflow=arguments.workflow,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
    )
    permit.validate_phase(
        baseline_phase=dataset.activation.baseline_phase,
        baseline_sha256=file_hash(arguments.data_dir / "baseline.json")
        if dataset.activation.baseline_phase == "active"
        else None,
    )
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if (
        actual_head != arguments.source_sha
        or subprocess.check_output(["git", "status", "--porcelain"], text=True)
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY") != arguments.repository
        or os.environ.get("GITHUB_RUN_ID") != arguments.run_id
        or os.environ.get("GITHUB_RUN_ATTEMPT") != str(arguments.run_attempt)
    ):
        raise ValueError("Protected execution requires the clean exact authorized CI source")
    prerequisites = json.loads(os.environ.get("OFFLINE_PREREQUISITES", "null"))
    if not isinstance(prerequisites, dict):
        raise ValueError("Offline prerequisite evidence is missing")
    verify_offline(
        arguments.data_dir,
        arguments.offline_dir,
        corpus=corpus,
        source_sha=arguments.source_sha,
        expected_manifest_hash=os.environ.get("OFFLINE_MANIFEST_SHA256", ""),
        prerequisites=prerequisites,
        lock_path=arguments.lockfile,
    )
    if arguments.output_dir.exists():
        raise ValueError("Paid execution cannot overwrite an earlier attempt")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("Protected provider credential is unavailable")

    async def execute() -> None:
        with TemporaryDirectory(prefix="rent-live-index-") as temporary:
            index = Path(temporary) / "corpus.sqlite"
            build_index(index, corpus=corpus)
            async with create_client(api_key=key) as client:
                result = await run_live_gate(
                    arguments.data_dir,
                    arguments.output_dir,
                    dataset=dataset,
                    corpus=corpus,
                    source_sha=arguments.source_sha,
                    lock_path=arguments.lockfile,
                    offline_dir=arguments.offline_dir,
                    permit=permit,
                    messages=cast(MessagesPort, client.messages),
                    retrieve=lambda query: search(
                        index, query, expected_corpus_hash=corpus.corpus_hash
                    ),
                )
            print(
                json.dumps(
                    {
                        "source_sha": result.source_sha,
                        "batch_uuid": str(result.batch_uuid),
                        "evaluation_complete": result.evaluation_complete,
                        "passed": result.passed,
                    }
                )
            )
            verify_live_gate(
                arguments.output_dir,
                data_dir=arguments.data_dir,
                dataset=dataset,
                corpus=corpus,
                source_sha=arguments.source_sha,
                lock_path=arguments.lockfile,
                manifest_sha256=file_hash(arguments.output_dir / "manifest.json"),
            )

    asyncio.run(execute())
