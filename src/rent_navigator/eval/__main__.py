"""Explicit, offline-only evaluation commands."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.offline import activation_at_revision, run_offline, verify_offline


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate and evaluate explicit offline datasets")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-gold", "offline", "plan", "verify-offline"):
        child = commands.add_parser(command)
        child.add_argument("--data-dir", type=Path, required=True)
        if command != "validate-gold":
            child.add_argument("--output-dir", type=Path, required=True)
        if command in {"offline", "verify-offline"}:
            child.add_argument("--source-sha", required=True)
            child.add_argument("--lockfile", type=Path, default=Path("uv.lock"))
            child.add_argument("--base-sha")
            child.add_argument("--repository", type=Path, default=Path.cwd())
        if command == "offline":
            child.add_argument("--security-report", type=Path, required=True)
        if command == "verify-offline":
            child.add_argument("--manifest-sha256", required=True)
            child.add_argument("--prerequisites", required=True)
    arguments = parser.parse_args(argv)
    try:
        corpus = load_corpus()
        if arguments.command in {"validate-gold", "plan"}:
            dataset = load_gold(arguments.data_dir, corpus, require_approved=False)
            if arguments.command == "validate-gold":
                print(
                    json.dumps(
                        {
                            "candidate_valid": True,
                            "case_count": len(dataset.cases),
                            "gold_hash": dataset.gold_hash,
                            "approval_status": dataset.approval.status,
                            "executed": False,
                        }
                    )
                )
                return
            from rent_navigator.eval.runner import build_plan

            arguments.output_dir.mkdir(parents=True, exist_ok=False)
            (arguments.output_dir / "plan.json").write_text(
                build_plan().model_dump_json(indent=2) + "\n"
            )
            print(json.dumps({"plan_written": True, "executed": False}))
            return
        previous = (
            activation_at_revision(arguments.repository, arguments.base_sha)
            if arguments.base_sha
            else None
        )
        if arguments.command == "offline":
            manifest = run_offline(
                arguments.data_dir,
                arguments.output_dir,
                corpus=corpus,
                source_sha=arguments.source_sha,
                security_report=arguments.security_report,
                lock_path=arguments.lockfile,
                previous_activation=previous,
            )
        else:
            prerequisites = json.loads(arguments.prerequisites)
            if not isinstance(prerequisites, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in prerequisites.items()
            ):
                raise ValueError("Prerequisites must map job names to conclusions")
            manifest = verify_offline(
                arguments.data_dir,
                arguments.output_dir,
                corpus=corpus,
                source_sha=arguments.source_sha,
                expected_manifest_hash=arguments.manifest_sha256,
                prerequisites=prerequisites,
                lock_path=arguments.lockfile,
                previous_activation=previous,
            )
        print(manifest.model_dump_json())
    except ValidationError:
        parser.exit(1, "Offline evaluation blocked: input or artifact schema is invalid.\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Offline evaluation blocked: {error}\n")


if __name__ == "__main__":
    main()
