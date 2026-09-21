# Rent Navigator

The current package provides strict data models, metadata tracing, a six-source official snapshot, an offline SQLite FTS5 index, pure notice/rent calculators, and an internal asynchronous provider/extraction seam. The service exposes only `GET /healthz`; calculation, extraction and question-answering endpoints are not implemented. There is no deployed demo, evaluation baseline, or performance measurement.

> Independent project; not affiliated with the Government of Ontario or the Landlord and Tenant Board. General legal information, not legal advice. Rules as of 2026-09-21; results depend on confirmed facts. For advice, consult a licensed Ontario lawyer or paralegal.

The snapshot date is the earliest actual UTC fetch date among the six sources. It is separate from each statute's consolidation period and does not imply automatic updates. [PROJECT-SPEC.md](PROJECT-SPEC.md) and [CONTRACTS.md](CONTRACTS.md) define the approved scope and later acceptance criteria.

## Run locally

Use uv **0.11.21**. The project uses an isolated **Python 3.12.13** environment; system Python is not changed. No API key is required for local health, tests or offline smoke commands.

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

## Snapshot and provenance

The [manifest](src/rent_navigator/corpus/manifest.json) records canonical URLs, actual retrieval URLs, titles, UTC timestamps, consolidation periods, and full SHA-256 hashes of each original response and extracted text. All six originals are retained. The statutes' originals are full responses from the official API used by e-Laws, including complete HTML content; citation URLs remain the canonical statute pages.

| Source | Fetched on 2026-09-21, UTC | Consolidation period |
|---|---|---|
| [Residential Tenancies Act](https://www.ontario.ca/laws/statute/06r17) | 15:56:58.151446 | From September 21, 2026 to the e-Laws currency date |
| [Legislation Act](https://www.ontario.ca/laws/statute/06l21) | 15:56:58.224498 | From December 11, 2025 to the e-Laws currency date |
| [Residential rent increases](https://www.ontario.ca/page/residential-rent-increases) | 15:54:17.555377 | null; guidance |
| [LTB guide](https://tribunalsontario.ca/documents/ltb/Brochures/Guide%20to%20RTA%20%28English%29.html) | 15:54:17.747216 | null; guidance |
| [N1 instructions](https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase%20%26%20Instructions/N1%20instructions_final_Nov30_2015.pdf) | 15:54:18.101647 | null; instructions dated November 30, 2015 |
| [N2 instructions](https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase%20%26%20Instructions/N2%20instructions_final_Nov30_2015.pdf) | 15:54:18.005179 | null; instructions dated May 2017 |

The RTA consolidation advanced from the design's July 1 period to September 21. The selected 17 operative sections were compared with [official historical v52](https://www.ontario.ca/laws/statute/06r17/v52), covering July 1–September 20: all selected normalized paragraphs were unchanged. Future-amendment annotations remain in full text and are excluded from operative chunks. The guideline source confirms 2026 **2.1%** and 2027 **1.9%**. Historical examples in the PDFs remain unchanged.

At retrieval, the [e-Laws currency endpoint](https://www.ontario.ca/laws/api/v2/legislation/en/currency-date) returned September 16, 2026. The [e-Laws glossary](https://www.ontario.ca/laws/e-laws-definitions#toc-e) explains that a later consolidation start date controls currency. The manifest preserves the official period wording. Release still requires a new operative-text comparison.

Full extracted text uses NFC, LF newlines, collapsed horizontal whitespace, and preserved paragraphs/headings/numbering. [Chunks](src/rent_navigator/corpus/chunks.jsonl) select only the contract's 17 RTA sections, Legislation Act s. 89, specified guideline/LTB sections, and both complete instruction PDFs. Each section is packed at paragraph boundaries up to 500 whitespace-delimited words; oversized paragraphs split into consecutive 500-word pieces. Parts start at **1** independently for each heading. IDs hash canonical URL, heading, decimal part, and normalized text joined by LF, with no final LF.

There are **53 chunks**: RTA 26, Legislation Act 1, guideline 9, LTB guide 4, N1 7, N2 6. [Rules](src/rent_navigator/corpus/rules.json) map all ten existing RuleIds to constants or explicit semantics and resolving evidence IDs. These mappings contain no date or amount calculation implementation. Government material retains its original rights; attribution and unofficial-copy notices are in [NOTICE](NOTICE), also included in distribution metadata.

`corpus_hash` is:

```text
8be6cf66829e42c59cbec66896d1c762de130283989623a05edc30b427246720
```

It hashes exactly fifteen data files under `rent_navigator/corpus/`: `manifest.json`, `chunks.jsonl`, `rules.json`, six `raw/` originals, and six `text/` files. Sort their relative POSIX paths, concatenate `path + LF + sha256(file_bytes) + LF` for each, then hash the UTF-8 result. SQLite, code, caches and any hash output are excluded.

## Offline index

```sh
uv run --locked python -m rent_navigator.corpus.snapshot src/rent_navigator/corpus
uv run --locked python -m rent_navigator.index rebuild /tmp/rent-navigator.sqlite3
uv run --locked python -m rent_navigator.index query /tmp/rent-navigator.sqlite3 'tenant'
```

The first command reproduces all text, chunks and rules from committed originals to verify their bytes. It never fetches or changes files. The index rebuild is also entirely offline. Choose a writable database destination outside installed package data; the derived database is not committed.

FTS5 indexes headings and text with `unicode61`, default BM25 weights, ascending score and chunk-ID tie breaking. Search quotes casefolded Unicode alphanumeric tokens, deduplicates them in first-seen order and joins them with OR. It returns at most five unique chunks, or none for an empty query, without rewriting or a stop-word list. Fixed fact queries are exported as `NOTICE_QUERY` and `RENT_QUERY`.

Rebuild tests compare logical rows and ordered IDs/scores from two clean rebuilds in the same SQLite environment. They do not require identical database bytes or scores across SQLite versions. Generic smoke queries check mechanics and provenance only; they are not gold questions, relevance labels or retrieval-quality measurements.

## Notice calculator

`notice.notice_deadline_check(facts: NoticeFacts, *, corpus: Corpus)` calculates ordinary notice-only thresholds for hand or mail service, with proposed effective years 2026–2027. The caller loads and validates the immutable corpus before calling the function. The calculation reads rule values in memory and performs no file, database, network, clock, or environment access. The internal `corpus` argument is not part of the public tool-input schema.

Every result includes scope, supported-year, rental-period and notice checks. Known exclusions return `unsupported` with null derived fields. Unknown confirmations prevent a passing overall result while preserving a determinable notice failure; missing facts preserve each independently calculable date. Signed day intervals retain late-service failures. If a derived date exceeds years 0001–9999, only that date becomes null. A passing result means only the checked conditions passed; these thresholds do not establish a lawful increase or valid notice.

## Rent calculator

`rent.rent_increase_check(facts: RentFacts, *, corpus: Corpus)` reuses the public notice calculator and adds spacing, guideline and form checks. It has the same scope, supported years and requirement for an already loaded corpus, with no file, database, network, clock or environment access. All seven checks remain present, including simultaneous failures. A definite failure takes priority over missing facts unless scope or rental-period confirmation is unknown; known exclusions clear every derived field.

Spacing uses the confirmed last increase, or the tenancy start only when no previous increase is confirmed. It adds calendar months, mapping February 29 to February 28 when needed. An anniversary beyond year 9999 is null in the output but still fails against a known supported effective date. Exact guideline caps use scaled integers, independent of decimal precision. A fractional cap's next whole cent returns `rounding_uncertain`; this is project uncertainty policy, not a statutory rounding rule. Available percentages and caps remain visible when other required facts are missing.

An explicitly confirmed section 6.1 exemption affects only the guideline check. The form check compares N1/N2 with the confirmed status; neither a form nor a date establishes an exemption. Exemption evidence, form completeness, actual delivery contents and overall legal validity are not adjudicated.

## Internal provider and extraction

`ProviderAdapter.generate` preflights the full prepared request and performs at most one asynchronous, non-streaming generation. It uses the locked SDK's standard Messages API, fixed model settings, no retries, a 7,000-token preflight limit and conservative per-call reservations. Actor sampling is sent as fixed `extra_body={"temperature": 0}` because this SDK exposes it through that parameter; judge sampling overrides are omitted. Native tool-use blocks are preserved for later integration, with no tool execution or loop.

`extract_letter(request, *, provider, trace, redact, deadline)` requires a redactor with no default fallback. It redacts before constructing either provider payload, transforms the public extraction schema for provider support, then validates the response against the original strict three-field model. Missing or ambiguous facts remain null. Refusal, truncation, malformed content and invalid fields fail without repair. The caller supplies one absolute `Deadline` and owns `trace.finish(...)`; errors must finish with the safe `ProviderFailure.code`, and cancellation must finish safely before propagating. The development smoke shows this composition.

Usage is captured before output validation. Missing/partial usage or unpriced billing categories retain unknown actual cost and the full reservation; input usage above the reservation preserves known cost. These conditions stop further paid requests for reconciliation. `SpendLedger` is an atomic in-memory batch ledger, not a durable project account or daily admission counter. Endpoint summaries and provider details are not added twice.

The formal email/phone/postal redactor is **not implemented**. The current smoke accepts only two fixed anonymous synthetic letters; arbitrary real letters are not supported. Agent integration, HTTP business routes and the full safety pipeline remain future work.

Run the explicit offline smoke from a clean committed checkout, using a new output directory each time:

```sh
test -z "$(git status --porcelain)" && \
uv run --locked python -m rent_navigator.smoke_extraction --offline \
  --source-commit "$(git rev-parse HEAD)" \
  --evidence-dir /tmp/rent-navigator-extraction-offline
```

It uses in-process fakes, writes metadata plus a clearly marked synthetic report, and makes no real requests. It also runs from an installed wheel or a container with networking disabled. Synthetic token costs and timings are not production measurements.

Runtime model availability and live extraction compatibility are **NOT RUN: credentials pending**. A separate `--live --billing-ready` mode exists for later authorized verification after an API key is available in the process environment and prepaid billing/replenishment settings are confirmed. It checks both approved model IDs, then attempts at most two actor generations, reserving at most US$0.022 for the batch; any failure or mismatch stops it. Do not rerun a paid attempt without checking the remaining authorized call/budget allowance. Evidence directories cannot overwrite an earlier attempt. No judge generation is performed.

The real client is explicitly constructed with the official base URL and `max_retries=0`; SDK/transport logging is disabled and custom-header environment overrides are rejected. Never place keys in source, command arguments, evidence, or logs. Default tests and PR CI require neither credentials nor provider access.

## Verify

```sh
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

`uv.lock` records exact dependency versions and distribution hashes. Strict mypy covers source and tests. Tests cover public JSON boundaries, synthetic trace accounting, source/chunk integrity, rule resolution, deterministic extraction, query tokenization, offline rebuilds, notice boundaries, calendar anniversaries, exact rent caps and form/status combinations. Synthetic timing/usage values are not serving measurements.

The unfiltered PR workflow runs basic checks, installs the wheel outside the checkout with locked dependencies, and runs `tests/smoke_corpus.py`. This checks package data, citations, rules and two rebuilds with socket access disabled. Offline evaluation, live evaluation and branch protection remain future work; the workflow does not yet provide evaluation regression blocking.

## Docker

The Dockerfile retains Python **3.12.13-slim-bookworm** at digest `sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2` and uv **0.11.21** at `sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946`. Both include Linux ARM64 and AMD64.

Build from the committed archive so uncommitted files cannot enter an image labeled with a clean revision:

```sh
git archive --format=tar HEAD | docker build \
  --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" \
  --tag rent-navigator:wp2 -
docker run --rm --publish 127.0.0.1:8000:8000 rent-navigator:wp2
```

The image runs as a non-root user with one worker and access logging disabled. CI checks health and the OCI revision against the full source SHA. It separately runs the installed-corpus smoke from `/tmp` with no network, a read-only root filesystem and writable temporary storage. No runtime source fetch is performed.

## Stable interfaces

- `models.py`, `trace.py`, and the health-only application factory retain their WP1 interfaces.
- `corpus.load_corpus()` returns immutable source/chunk/rule metadata plus `snapshot_date` and `corpus_hash`. `.chunk(id)`, `.rule(id)`, and `.citation(id)` resolve identifiers and reject missing ones.
- `index.build_index(path)` creates a derived index; `inspect_index(path)` reads its metadata; `search(path, question, expected_corpus_hash=...)` returns typed chunks and BM25 scores. Data loading does not depend on the working directory.
- `notice.notice_deadline_check(facts, *, corpus)` returns the existing `ToolResult` with four ordered notice checks, partial derived fields, and resolving rule IDs.
- `rent.rent_increase_check(facts, *, corpus)` returns the existing `ToolResult` with seven ordered checks, exact decimal caps, partial derived fields, and resolving rule IDs. Its provider-visible arguments remain exactly `RentFacts`.
- `provider.ProviderAdapter` accepts an injected async messages port and batch budget. `Deadline` is shared across calls, and `configuration_hash` identifies fixed settings and request schemas without including message contents.
- `extract.extract_letter(request, *, provider, trace, redact, deadline)` returns the existing `Extraction`; `extraction_config_hash()` covers its fixed prompt and transformed schema. Composition with the real SDK uses `cast(MessagesPort, client.messages)` at this tested boundary.

Agent/request integration, formal redaction, evaluation and deployment remain future work.
