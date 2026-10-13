"""Local image collection entry with explicit caps and immutable result directories."""

import argparse
import asyncio
import json
import os
import platform
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from pydantic import TypeAdapter

from rent_navigator.corpus import load_corpus
from rent_navigator.eval.collection import RealCollectionManifest, RunEnvironment, collect_real
from rent_navigator.eval.data import load_gold
from rent_navigator.eval.dry_run import DryRunManifest, collect_dry_run, dry_run_forecast
from rent_navigator.eval.offline import file_hash
from rent_navigator.index import SearchHit, build_index, search
from rent_navigator.models import ISODate, SourceCommit
from rent_navigator.provider import MessagesPort, ProviderFailure, SpendLedger, create_client

COLLECTION_FORECAST = Decimal("29.15320")
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"


class FileBatchLedger:
    """Append forecast records before operations; this is not an account balance."""

    def __init__(self, path: Path, *, cap_usd: Decimal) -> None:
        if not cap_usd.is_finite() or cap_usd < 0:
            raise ValueError("Batch cap must be finite and nonnegative")
        if path.exists():
            raise FileExistsError("Batch ledger already exists")
        self.path, self.cap = path, cap_usd
        self._forecasts: list[Decimal] = []

    def reserve_batch(self, forecast_usd: Decimal) -> None:
        if (
            not forecast_usd.is_finite()
            or forecast_usd < 0
            or sum(self._forecasts, Decimal(0)) + forecast_usd > self.cap
        ):
            raise ProviderFailure("budget_exhausted")
        records = [*self._forecasts, forecast_usd]
        value = {
            "schema_version": 1,
            "batches": [
                {"cap_usd": format(self.cap, "f"), "forecast_usd": format(amount, "f")}
                for amount in records
            ],
        }
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self._forecasts = records


def _ram_bytes() -> int | None:
    try:
        value = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _amount(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Amount must be a decimal") from None


def add_collection_commands(commands: Any) -> None:
    for name in ("collect", "collect-dry-run"):
        child = commands.add_parser(
            name,
            description=(
                "Run the fixed collection or three judged production attempts locally. "
                "The image runs as uid 65532, contains the packaged corpus but no eval/ data, "
                "and requires network access and a results volume writable by uid 65532. "
                'Mount -v "$PWD/eval:/data/eval:ro" and use --data-dir /data/eval. '
                "The results root is new and exclusive: batch-ledger.json plus collection/ "
                "or dry-run/ artifacts. This command grants no paid execution authority."
            ),
        )
        child.add_argument(
            "--cap-usd",
            type=_amount,
            required=True,
            help="Flat cap, at most 32 USD for collection or 1 USD for dry run",
        )
        child.add_argument(
            "--forecast-usd",
            type=_amount,
            default=COLLECTION_FORECAST if name == "collect" else dry_run_forecast(),
            help=(
                "Reservation forecast, finite/nonnegative and no greater than cap "
                f"(default: {COLLECTION_FORECAST if name == 'collect' else dry_run_forecast()})"
            ),
        )
        child.add_argument("--results-dir", type=Path, required=True)
        child.add_argument(
            "--data-dir",
            type=Path,
            required=True,
            help=(
                "Read-only eval directory containing gold.jsonl, gold-approval.json "
                "and activation.json"
            ),
        )
        child.add_argument(
            "--source-sha", help="Defaults to the image SOURCE_COMMIT environment variable"
        )
        child.add_argument(
            "--lockfile",
            type=Path,
            default=Path("uv.lock"),
            help="Defaults to uv.lock in the image WORKDIR /app",
        )
        child.add_argument("--image-digest", required=True)
        child.add_argument("--docker-version", required=True)
        child.add_argument("--provider-access-date", required=True)
        child.add_argument("--pricing-url", default=PRICING_URL)
        child.add_argument(
            "--source-clean",
            action="store_true",
            help="Required caller assertion after checking the host Git tree before image build",
        )
        child.add_argument("--os", default=platform.platform())
        child.add_argument("--cpu", default=platform.processor() or platform.machine())
        child.add_argument("--ram-bytes", type=int, default=_ram_bytes())


def collection_command(arguments: argparse.Namespace) -> None:
    cap, forecast = arguments.cap_usd, arguments.forecast_usd
    maximum = Decimal("32.00") if arguments.command == "collect" else Decimal("1.00")
    if (
        not cap.is_finite()
        or not 0 <= cap <= maximum
        or not forecast.is_finite()
        or not 0 <= forecast <= cap
    ):
        raise ValueError("Forecast and cap violate the command's bounds")
    source_sha = TypeAdapter(SourceCommit).validate_python(
        arguments.source_sha or os.environ.get("SOURCE_COMMIT"), strict=True
    )
    TypeAdapter(ISODate).validate_python(arguments.provider_access_date)
    if not arguments.source_clean:
        raise ValueError("Collection requires the caller's clean-source assertion")
    if any(
        not value.strip()
        for value in (
            arguments.os,
            arguments.cpu,
            arguments.docker_version,
            arguments.image_digest,
            arguments.pricing_url,
        )
    ):
        raise ValueError("Run environment fields must be nonempty")
    if arguments.ram_bytes is None or arguments.ram_bytes <= 0:
        raise ValueError("Supply a positive RAM size when automatic detection is unavailable")
    environment = RunEnvironment(
        os=arguments.os,
        cpu=arguments.cpu,
        ram_bytes=arguments.ram_bytes,
        docker_version=arguments.docker_version,
        image_digest=arguments.image_digest,
        source_clean=arguments.source_clean,
        provider_access_date=arguments.provider_access_date,
        pricing_url=arguments.pricing_url,
    )
    corpus = load_corpus()
    dataset = load_gold(arguments.data_dir, corpus)
    lock_hash = file_hash(arguments.lockfile)
    arguments.results_dir.mkdir(parents=True, exist_ok=False)
    ledger = FileBatchLedger(arguments.results_dir / "batch-ledger.json", cap_usd=cap)
    budget = SpendLedger(cap)

    async def execute() -> RealCollectionManifest | DryRunManifest:
        client = create_client(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        try:
            with TemporaryDirectory(prefix="rent-navigator-collection-") as temporary:
                index = Path(temporary) / "index.sqlite3"
                build_index(index, corpus)

                def retrieve(query: str) -> tuple[SearchHit, ...]:
                    return search(index, query, expected_corpus_hash=corpus.corpus_hash)

                if arguments.command == "collect":
                    return await collect_real(
                        dataset,
                        arguments.results_dir / "collection",
                        corpus=corpus,
                        source_sha=source_sha,
                        lock_hash=lock_hash,
                        environment=environment,
                        messages=cast(MessagesPort, client.messages),
                        budget=budget,
                        batch_ledger=ledger,
                        forecast_usd=forecast,
                        retrieve=retrieve,
                    )
                return await collect_dry_run(
                    dataset,
                    arguments.results_dir / "dry-run",
                    corpus=corpus,
                    source_sha=source_sha,
                    lock_hash=lock_hash,
                    environment=environment,
                    messages=cast(MessagesPort, client.messages),
                    budget=budget,
                    batch_ledger=ledger,
                    forecast_usd=forecast,
                    retrieve=retrieve,
                )
        finally:
            await client.close()

    manifest = asyncio.run(execute())
    print(manifest.model_dump_json())
    complete = (
        manifest.evaluation_complete
        if isinstance(manifest, RealCollectionManifest)
        else manifest.dry_run_complete
    )
    if not complete:
        raise ValueError("Execution is incomplete; retained artifacts must be reviewed")
