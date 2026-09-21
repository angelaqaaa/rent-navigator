# Rent Navigator

WP1 provides an installable Python package, strict public data models, metadata tracing, and a single `GET /healthz` endpoint. The service currently performs no legal calculations and answers no questions. The corpus and its snapshot have not been created. There is no deployment, evaluation baseline, or performance measurement yet.

> Independent project; not affiliated with the Government of Ontario or the Landlord and Tenant Board. General legal information, not legal advice. Rules as of {snapshot_date}; results depend on confirmed facts. For advice, consult a licensed Ontario lawyer or paralegal.

`{snapshot_date}` is an unresolved placeholder until the six official sources are snapshotted. It is not the design date or a claim about current law. The approved scope and future acceptance criteria are in [PROJECT-SPEC.md](PROJECT-SPEC.md) and [CONTRACTS.md](CONTRACTS.md).

## Run locally

Use uv **0.11.21**. The project uses an isolated **Python 3.12.13** environment; system Python is not changed. No API key is required for any WP1 command.

```sh
uv python install 3.12.13
uv sync --locked
```

Start from a clean committed checkout so the reported revision identifies the running source:

```sh
test -z "$(git status --porcelain)" && \
SOURCE_COMMIT="$(git rev-parse HEAD)" uv run --locked uvicorn \
  rent_navigator.app:create_app --factory --host 127.0.0.1 --port 8000 \
  --workers 1 --no-access-log
```

In another terminal:

```sh
curl --fail http://127.0.0.1:8000/healthz
```

The response contains `status: "ok"` and the full commit SHA. Startup fails if `SOURCE_COMMIT` is missing or malformed. There are no `/api/ask`, `/api/extract`, page, or documentation routes yet.

## Verify

```sh
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

`uv.lock` records exact dependency versions and distribution hashes; direct dependencies and the build backend are also pinned in `pyproject.toml`. Strict mypy covers source and tests. JSON contract tests exercise the actual wire boundary, including explicit nulls, canonical dates/UUIDs, enums, and result ordering. Trace tests use synthetic usage and injected clocks; their values are not production latency or cost measurements.

The unfiltered pull-request workflow runs these basic checks plus Docker build and health smoke tests. Offline evaluation, live evaluation, and branch protection are future work; the current workflow does not provide evaluation regression blocking.

## Docker

The Dockerfile pins Python **3.12.13-slim-bookworm** to multi-platform digest `sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2` and uv **0.11.21** to `sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946`. Both include Linux ARM64 and AMD64.

Build from the committed archive, so uncommitted files cannot enter an image labeled with a clean revision:

```sh
git archive --format=tar HEAD | docker build \
  --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" \
  --tag rent-navigator:wp1 -
docker run --rm --publish 127.0.0.1:8000:8000 rent-navigator:wp1
```

The image runs as a non-root user, with one worker and access logging disabled. Its health check verifies both status and source revision. The build context allows only packaging files, README, and application source; local references, secrets, and Git history are excluded.

## Stable foundation

- `models.py`: input/output schemas and provider tool definitions derived from `NoticeFacts` and `RentFacts`. Runtime citation provenance, confirmed-argument matching, fixed response assembly, and the two calculations belong to later packages.
- `trace.py`: metadata records, clock-injected duration capture, and exact usage/cost accounting. The sink accepts typed metadata only; text, tool arguments, IP addresses, and raw errors are excluded. No provider request is made by this module.
- `app.py`: the application factory that later HTTP work will extend; only health is currently exposed.

The next package establishes the corpus and offline index. No snapshot date or corpus hash is fabricated in this foundation.
