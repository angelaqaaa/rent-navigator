# Rent Navigator

The current package provides strict data models, metadata tracing, a six-source official snapshot, an offline SQLite FTS5 index, pure notice/rent calculators, an internal asynchronous provider/extraction seam, bounded analysis orchestration, pattern redaction, eight offline security fixtures, and an offline evaluation and synthetic collection harness. Development CLIs accept only fixed synthetic inputs. The service exposes only `GET /healthz`; calculation, extraction and question-answering endpoints are not implemented. There is no deployed demo, evaluation baseline, or performance measurement.

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

`ProviderAdapter.generate` preflights the full prepared request and performs at most one asynchronous, non-streaming generation. It uses the locked SDK's standard Messages API, fixed model settings, no retries and conservative per-call reservations. The shared model policy sets actor input preflight/reservation to **15,000/16,000** and judge to **7,000/8,000**; output limits remain **600/800** respectively. Actor sampling is sent as fixed `extra_body={"temperature": 0}` because this SDK exposes it through that parameter; judge sampling overrides are omitted. The adapter preserves native tool-use blocks; the separate analysis entry validates and executes them.

`extract_letter(request, *, provider, trace, redact, deadline)` requires a redactor with no default fallback. It redacts before constructing either provider payload, transforms the public extraction schema for provider support, then validates the response against the original strict three-field model. Missing or ambiguous facts remain null. Refusal, truncation, malformed content and invalid fields fail without repair. The caller supplies one absolute `Deadline` and owns `trace.finish(...)`; errors must finish with the safe `ProviderFailure.code`, and cancellation must finish safely before propagating. The development smoke shows this composition.

Usage is captured before output validation. Missing/partial usage or unpriced billing categories retain unknown actual cost and the full reservation; input usage above the reservation preserves known cost. These conditions stop further paid requests for reconciliation. `SpendLedger` is an atomic in-memory batch ledger, not a durable project account or daily admission counter. Endpoint summaries and provider details are not added twice.

`model_policy.MODEL_POLICIES` supplies admission, accounting and runner budgets from one immutable table. Actor calls reserve **US$0.019**, judge calls **US$0.024**, at the unchanged token rates. The limits apply uniformly by model across extraction, questions, tool selection, final answers and both internal arms. Configuration and pricing hashes cover these model limits; historical evidence retains its original reservations and identities.

`guards.redact_text` replaces supported email, NANP phone and Canadian postal patterns with `[EMAIL]`, `[PHONE]` and `[POSTAL]`, in that order. It preserves unmatched text and is idempotent. Letters and questions are redacted before the first token count, with the same redacted content used for generation. Confirmed facts have no free-text fields and are serialized unchanged; their numeric values never enter regex redaction. The extraction smoke still enforces its two-letter allowlist before applying this policy; arbitrary letters are not accepted by the CLI.

The policy supports ASCII dot-atom email addresses with dotted domains, ten-digit NANP numbers with optional country code/area parentheses/extensions, and Canadian postal codes in compact, horizontal-space or single-hyphen form. Horizontal whitespace is space, tab, nonbreaking space or narrow nonbreaking space; patterns do not join lines. Explicit `$`, `C$` and `CAD` decimal amounts are protected, including `$4165550123.00`; an unlabelled ten-digit NANP token is treated as a phone number. Dates, ordinary rents and percentages remain intact. Names, street addresses, obfuscated contacts, international phone formats and Unicode hyphens are outside this policy. Pattern redaction does not establish anonymity.

`REDACTION_POLICY_HASH` identifies the fixed pattern strings, flags, order, placeholders and currency policy. Extraction and analysis configuration hashes include it; request contents never enter these hashes.

Run the explicit offline smoke from a clean committed checkout, using a new output directory each time:

```sh
test -z "$(git status --porcelain)" && \
uv run --locked python -m rent_navigator.smoke_extraction --offline \
  --source-commit "$(git rev-parse HEAD)" \
  --evidence-dir /tmp/rent-navigator-extraction-offline
```

It uses in-process fakes, writes metadata plus a clearly marked synthetic report, and makes no real requests. It also runs from an installed wheel or a container with networking disabled. Synthetic token costs and timings are not production measurements.

On **2026-09-21**, one live development smoke on source commit `b3de863abf7306fa1b4dee7ad21c70dcb583af69` confirmed availability of `claude-haiku-4-5-20251001` and `claude-sonnet-5`, and both fixed extraction fixtures passed: `(123450, 125980, 2027-04-01)` and `(null, null, null)`. Two actor generations used **1,142 input / 56 output tokens**, with **US$0.001422** actual cost at the frozen rates and US$0.022 total reservations. These synthetic development results are not release measurements. [PR #5](https://github.com/angelaqaaa/rent-navigator/pull/5) records the results, full hashes and persistent evidence location; the live run was not repeated for subsequent documentation changes.

Further `--live --billing-ready` runs require explicit budget authorization, an API key in the process environment, and confirmed prepaid billing/replenishment settings. The runner checks both approved model IDs, then attempts at most two actor generations, reserving at most US$0.038 for the batch; any failure or mismatch stops it. Do not rerun a paid attempt without checking the remaining authorized call/budget allowance. Evidence directories cannot overwrite an earlier attempt. No judge generation is performed.

The real client is explicitly constructed with the official base URL and `max_retries=0`; SDK/transport logging is disabled and custom-header environment overrides are rejected. Never place keys in source, command arguments, evidence, or logs. Default tests and PR CI require neither credentials nor provider access.

## Internal analysis and restricted CLI

`agent.answer(request, *, provider, corpus, retrieve, redact, deadline, context, sink, arm="production")` owns and finishes exactly one analysis trace. It requires an injected redactor and the caller's absolute deadline. Question mode retrieves the original question once locally, sends only redacted text, and makes one actor call without tools. Confirmed notice/rent facts require a native provider tool call with exactly matching arguments, one execution of the existing calculator, and a second generation with the matching tool-result ID and tools disabled. Invalid calls and outputs fail without repair or retries.

The second request includes every evidence chunk mapped by every executed rule, deduplicated against the original top five. Every production statement must cite an allowed retrieved or executed-rule chunk; the server resolves the canonical URL, heading and snapshot. This guarantees provenance and citation coverage, not semantic support. The actual calculator result remains authoritative. All prepared history, tools, evidence and output schema enter token preflight; evidence is never truncated to pass the budget limit.

Rent final requests also receive exact CAD display strings for the confirmed current rent, proposed total new rent and the calculator's exact new-rent ceiling. The server shifts cents by two decimal places without rounding or depending on decimal precision, preserving fractional cents and nulls. Both arms receive the same context after the unchanged native tool result. Final instructions distinguish the total rent ceiling from the increase, prohibit additional calculated amounts and monthly assumptions, and preserve uncertain results. Production citation instructions require a named statutory section's supplied chunk or omission of that section reference. These instructions do not validate the meaning of generated prose.

The trusted internal `baseline` arm omits retrieval and evidence passages while preserving the same facts, tools, model, budgets and deadline. Citations may be empty; supplied IDs must still belong to executed rules. It is tested with fakes only and is not a public request or CLI option. `agent_config_hash(mode, arm)` identifies fixed prompts, tool/output schemas, stage choices, display text and provider configuration without request text or identifiers.

The CLI accepts exactly one preset anonymous confirmed-rent fixture: $2,000 to $2,048, effective September 1, 2026, hand-served July 3, with a September 1, 2025 previous increase, controlled status and N1. Its expected result has both notice and guideline failures, a 60-day notice interval and an exact $2,042 cap. It accepts no arbitrary question, letter, file or standard-input content. The runner validates the fixed request before recording any request payload, independently of redaction.

`agent.RetrievalContext(hits, untrusted_text)` is an internal test seam alongside the ordinary retrieval tuple. Production analysis places its optional 1–4,000-character sidecar under `untrusted_retrieved_text` in user context beside unchanged canonical excerpts. It receives no evidence identity or citation permission and is absent from trace metadata. Full preflight includes it; the baseline never retrieves or receives it. No public request field or CLI flag exposes this seam. Its fixed policy is included in `agent_config_hash`.

`security_cases.load_security_cases(corpus=...)` explicitly loads eight ordered, packaged synthetic cases, S01–S08, and validates their canonical evidence references. `security_cases_hash()` hashes the exact resource bytes. Offline tests drive the real extraction and analysis boundaries for unauthorized tools, forged citations, strict output/disclaimer ownership, altered confirmed facts, hostile retrieval, contact redaction, excluded scope and fixed refusal text. These fixtures are not gold labels or evaluation scores. Safe fake outputs do not establish instruction obedience, affiliation claims, narrative false-pass prevention or advice avoidance. **Seven real-model safety cases remain pending WP9**; there is no semantic safety guarantee or live regression gate.

From a clean committed checkout:

```sh
test -z "$(git status --porcelain)" && \
uv run --locked python -m rent_navigator.cli --offline \
  --source-commit "$(git rev-parse HEAD)" \
  --evidence-dir /tmp/rent-navigator-analysis-offline
```

Use a fresh external writable directory. The CLI builds SQLite there and retains its report, metadata trace, synthetic requests/returns and preflight estimates; it refuses to overwrite an earlier attempt. Offline mode uses recording fakes and incurs no provider cost. The same command works from an installed wheel outside the checkout and in a non-root container with a read-only filesystem, networking disabled and writable temporary storage. Synthetic token counts and timings are fixture values, not measured production behavior.

Explicit `--live --billing-ready` uses the same production path for one fixed fixture, with at most two actor generations, two preflights, a US$0.038 batch reservation, no judge and no retries. It requires separately authorized budget, confirmed prepaid billing with replenishment disabled and a key provided only through the process environment. Any failure stops the attempt and preserves incomplete evidence. A passing mechanical check still requires review that the generated explanation describes both failures faithfully; it is not a legal gold case, evaluation result or proof of general safety. No live success is claimed by the offline fixtures.

The first live analysis attempt on source `599c44b6777414a27f31d28a7e329819333d7299` stopped correctly when the complete second request counted **12,289 tokens**, above its then-current 7,000 limit. It produced one correct native tool call and the expected calculator result, but no final answer. Its complete usage cost **US$0.004659**; the original US$0.022 batch ceiling, US$0.011 entered-call reservation and original hashes remain unchanged in the retained [PR #6 evidence](https://github.com/angelaqaaa/rent-navigator/pull/6).

Following independent offline acceptance and funding confirmation, one live smoke on **`93399f4b6e66f62d9a70f4de01b3dfc10c5724da`** completed both counts and generations without retries. Its final answer correctly described the 60-day notice failure and 2.4% versus 2.1% guideline failure, but incorrectly rendered cents as dollars: **$204,800 / $204,200 / $600** instead of **$2,048 / $2,042 / $6**. The original tool result remained correct and server citations resolved. **That attempt failed explanation acceptance because of the monetary error**, despite the runner's mechanical PASS. Complete billed usage was **15,703 input / 800 output tokens**, costing **US$0.019703** against US$0.038 reservations. Both raw returns and failed acceptance evidence are retained; the result was not repaired or rerun.

The subsequent money-context repair passed independent offline review. It changes the request context and instructions while preserving the original tool inputs/results and complete evidence; its tests cover fractional, null and large-integer amounts. A separately authorized live smoke on **`5c3d7642c9b6e6f03f90caa993caa29ce29c39ad`** then completed two counts and two generations, correctly displaying **$2048.00 proposed rent / $2042.00 maximum total new rent**, the 60-day notice failure and guideline failure. It additionally stated a correct **$6.00 difference** not present in the supplied money strings, a deviation from the instruction against other calculated amounts. Mechanical PASS does not establish strict prompt compliance; final explanation acceptance awaits independent evidence review. Complete usage was **15,911 input / 798 output tokens**, costing **US$0.019901** against US$0.038 reservations. All three attempts remain preserved; none was silently repaired or replaced.

The owner has confirmed the additional US$5, bringing confirmed prepayment to **US$30**, with automatic replenishment disabled. Further paid work requires a new explicit authorization. The amended US$50 allocation remains hosting14, development/CI/measurement21, demo9 and contingency6. Future reservations total **US$20.313**, including **15 remaining development slots**; cumulative incurred cost is **US$0.045685**, leaving **US$0.641315** within development. These are budget calculations, not a queried provider balance; the full US$9 demo allocation remains protected. The documentation update does not change the measured source, and no live run was repeated for it.

## Offline evaluation and collection

The 16-case candidate in `eval/gold.jsonl` is **draft, awaiting owner approval**. Its SHA-256 is `575cfd2b66465633f7cb04b44eb8455293c1a9c24a4911c0482879313d9b82cb`. The approval file binds every dataset byte and the corpus hash. Draft validation checks strict schemas and canonical evidence without calling calculators, searching, scoring or contacting a provider:

```sh
uv run --locked python -m rent_navigator.eval validate-gold --data-dir eval
uv run --locked python -m rent_navigator.eval plan \
  --data-dir eval --output-dir /tmp/rent-navigator-plan
```

Output directories must be new. `plan` only writes the fixed schedule: three warm-ups per arm, then five paired repetitions of the 16 cases, using one seed-42 random generator and alternating arm order. It makes no calls and does not approve the candidate.

After explicit approval is recorded for the exact hash, the offline command compares all ten tool results and their ordered checks, validates all sixteen cases, and measures deterministic MRR@5/NDCG@5 on the six questions. It requires passing evidence from the existing security suite:

```sh
uv run --locked pytest tests/test_security_pipeline.py \
  --junitxml=/tmp/rent-navigator-security.xml
uv run --locked python -m rent_navigator.eval offline \
  --data-dir eval --output-dir /tmp/rent-navigator-offline \
  --source-sha "$(git rev-parse HEAD)" --lockfile uv.lock \
  --security-report /tmp/rent-navigator-security.xml
```

With the current draft, `offline` exits nonzero before tool or retrieval execution and preserves a failure artifact. Approval cannot be inferred from passing unit tests. `eval/activation.json` explicitly leaves the comparison baseline pending; this defers only the baseline comparison. No baseline numbers are bootstrapped by this package. `verify-offline --help` describes the required source, manifest digest and prerequisite conclusions for independent artifact verification.

`eval.runner.run_attempt` uses the actual extraction and analysis operations through injected messages and judge ports. It preserves actual tool execution evidence, ranked retrieval IDs, failed attempts, separate serving/judge accounting, and distinct traces sharing one attempt ID. An extraction mismatch ends the attempt without correcting facts. Missing usage remains unknown, retains reservations and stops collection for budget reconciliation. Full-content recording is restricted to declared synthetic scenarios and prepared requests; ordinary trace records remain metadata-only.

`eval.collection.collect_synthetic` exercises the serial collection protocol through injected ports. It writes the plan first, persists each attempt, and retains partial runs on interruption. Its seven artifacts are `plan.json`, `manifest.json`, `results.jsonl`, `warmups.jsonl`, `metadata.jsonl`, `raw-provider.jsonl`, and `summary.json`. Verification checks their hashes, planned entries, observations and trace accounting. Synthetic manifests always record `execution_mode=synthetic`, `reportable=false`, and `evaluation_complete=false`. These tests establish the harness protocol; their timing, costs and fake judgments are not project measurements. The future measured boundary is extraction plus analysis through trace completion inside the recorded image, excluding judge work, HTTP/browser transport and human delay.

The evaluation CLI has no live mode and reads no API credentials. A real judge, paid evaluation composition, baseline activation and branch protection remain for WP9.

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

The unfiltered PR workflow runs basic checks, installs the wheel outside the checkout with locked dependencies, and runs `tests/smoke_corpus.py`. It also validates the evaluation data through the installed package. `offline-eval-run` executes approved offline checks and uploads complete or failed evidence. The always-running `offline-eval` summary requires successful checks, Docker and offline execution, then verifies the producer manifest digest, payload hashes, source/data/config identities, exact results, security XML and activation state. Missing, skipped, cancelled or stale prerequisites fail. While gold approval is pending, these offline acceptance jobs remain nonpassing. Live evaluation and merge protection are not configured yet.

## Docker

The Dockerfile retains Python **3.12.13-slim-bookworm** at digest `sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2` and uv **0.11.21** at `sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946`. Both include Linux ARM64 and AMD64.

Build from the committed archive so uncommitted files cannot enter an image labeled with a clean revision:

```sh
git archive --format=tar HEAD | docker build \
  --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" \
  --tag rent-navigator:wp2 -
docker run --rm --publish 127.0.0.1:8000:8000 rent-navigator:wp2
```

The image runs as a non-root user with one worker and access logging disabled. CI checks health and the OCI revision against the full source SHA. It separately runs the installed-corpus smoke from `/tmp` with no network, a read-only root filesystem and writable temporary storage. Evaluation input files are mounted read-only for candidate validation; they are not copied into the service image. No runtime source fetch is performed.

## Stable interfaces

- `models.py`, `trace.py`, and the health-only application factory retain their WP1 interfaces.
- `corpus.load_corpus()` returns immutable source/chunk/rule metadata plus `snapshot_date` and `corpus_hash`. `.chunk(id)`, `.rule(id)`, and `.citation(id)` resolve identifiers and reject missing ones.
- `index.build_index(path)` creates a derived index; `inspect_index(path)` reads its metadata; `search(path, question, expected_corpus_hash=...)` returns typed chunks and BM25 scores. Data loading does not depend on the working directory.
- `notice.notice_deadline_check(facts, *, corpus)` returns the existing `ToolResult` with four ordered notice checks, partial derived fields, and resolving rule IDs.
- `rent.rent_increase_check(facts, *, corpus)` returns the existing `ToolResult` with seven ordered checks, exact decimal caps, partial derived fields, and resolving rule IDs. Its provider-visible arguments remain exactly `RentFacts`.
- `provider.ProviderAdapter` accepts an injected async messages port and batch budget. `Deadline` is shared across calls, and `configuration_hash` identifies fixed settings and request schemas without including message contents.
- `extract.extract_letter(request, *, provider, trace, redact, deadline)` returns the existing `Extraction`; `extraction_config_hash()` covers its fixed prompt, transformed schema and redaction policy. Composition with the real SDK uses `cast(MessagesPort, client.messages)` at this tested boundary.
- `agent.answer(...)` returns the existing `AskResponse` and owns analysis trace completion. `agent_config_hash(mode, arm)` supplies its configuration identity; extraction keeps its separate caller-owned trace contract.

- `guards.redact_text(text)` applies the versioned free-text policy; `security_cases.load_security_cases(corpus=...)` returns the eight strict synthetic fixtures without executing them.
- `eval.data.load_gold(data_dir, corpus, require_approved=True)` validates the complete dataset and approval identities before execution. `eval.models` defines gold, judgments and result rows; `eval.metrics` provides deterministic scoring and collection aggregates.
- `eval.runner` provides `build_plan`, `run_attempt` and the injected `JudgePort`. `eval.collection` persists and verifies synthetic collections; `eval.offline` verifies exact offline artifacts and the frozen baseline schema.

HTTP request integration, real-model evaluation and security verification, and deployment remain future work.
