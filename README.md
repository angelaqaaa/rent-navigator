# Rent Navigator

The current package provides strict data models, metadata tracing, a six-source official snapshot, an offline SQLite FTS5 index, pure notice/rent calculators, bounded analysis orchestration, pattern redaction, eight offline security fixtures and the evaluation harness. The service exposes a text-only page at `GET /`, `GET /healthz`, `POST /api/extract` and `POST /api/ask`. Local image entry points support the fixed collection and a separate three-attempt dry run. A Render blueprint is included; there is no deployed demo, activated baseline or release collection. Free tests establish integration behavior, not genuine quality, latency or cost measurements.

> Independent project; not affiliated with the Government of Ontario or the Landlord and Tenant Board. General legal information, not legal advice. Rules as of 2026-09-21; results depend on confirmed facts. For advice, consult a licensed Ontario lawyer or paralegal.

The snapshot date is the earliest actual UTC fetch date among the six sources. It is separate from each statute's consolidation period and does not imply automatic updates. [PROJECT-SPEC.md](PROJECT-SPEC.md) and [CONTRACTS.md](CONTRACTS.md) define the approved scope and later acceptance criteria.

## Owner release-scope revision, 2026-10-12

The owner ruled the revised scope in PROJECT-SPEC.md §12 and CONTRACTS.md §11. It is not acceptance of the original definition of shipped. There is no deployed demo, activated baseline or release collection. The current implementation uses grade-only wire policy 7; anchor wire version 6 remains in history. HTTP and shared-service collection entry points are implemented; live release validation remains pending.

main requires `checks`, `docker` and `offline-eval`, with strict required checks and administrator enforcement. `live-eval-gate` is not required this cycle, and the live-eval environment reviewer rule is not restored. The workflow now applies the approved guards `github.event_name == 'pull_request' && vars.LIVE_EVAL_ENABLED == 'true'` for `live-eval-plan` and `always() && vars.LIVE_EVAL_ENABLED == 'true'` for `live-eval-gate`. Environments, secrets and variables are unchanged. The full live gate, its unchanged 28/32 and 7/7 thresholds and seven live attacks are not exercised this cycle. Baseline activation and the retrieval floor remain pending. Retrieval and answer-quality regressions are not gated.

The reporting, probe and review paragraphs below retain their pre-smoke checkpoint. The later completed operational smoke does not establish grading acceptance; the current collection hold is recorded under Local image collection and dry run.

The narrower release wording for bullet 2 is exactly: "exact-match tool evals and security fixtures run in CI as required checks and block regressions; LLM-as-judge scoring runs at release (calibration agreement m of 13; human audit agreement k of 32)". This is the approved reporting scope, not a completed measurement. No actual m/13 or k/32 value exists yet. The original PROJECT-SPEC.md §1 targets remain verbatim.

Deferred, not deleted: the live gate as a required check, the permit/environment/ledger protocol, the 22 fixed diagnostics, per-run independent review, baseline activation and the anchor wire. Private calibration, admission and ledger work stops; preserve existing evidence. Earlier funding and gate requirements below remain historical and do not authorize current execution.

The grade-only configuration hash is `1b4f130873e00fcd77e30b96756b060b947e43c5f39d83d1189f6bfe32ac69d2`, changed from the anchor configuration. On 2026-10-12 a single probe of the wire-6 J01 generation request returned HTTP 400 invalid_request_error, read once by the owner and classified as a compiled-grammar size rejection, confirming grade-only judging for this cycle. On 2026-10-13 the largest wire7 packet, J03, was accepted by the provider and the response ended normally. This establishes J03 grammar acceptance only, not a complete decoded vote, fourteen-vote smoke pass or release acceptance. The offline measurements cover every J01-J13 packet and R04: maximum schema 1288 bytes (J03), 26 enum members, depth 5, zero unions/optional properties; the owner froze the unchanged lint budgets at 1450 UTF-8 bytes/32 members/5 raw JSON depth, with headroom of 162 bytes/6 members/0 levels and the rejected wire6 schema as a positive control. These are project regression limits, not inferred provider internal bounds. Depth counts stored JSON objects/arrays from root depth 1, definitions once, without reference expansion. Token measurements remain null; the size worksheets retain their status from before the probe. The full fourteen-vote smoke remains pending. The probe exists in `scripts/judge_probe.py` and retains no provider response.

The required judge smoke is all NEW13 packets J01-J13 plus the R04 false-pass smoke, using exactly `required_claims`, `statements`, `false_pass`, `policy_violations`, existing grade domains/rubric and no per-part audit anchors, bases or quotes. If the owner does not independently review the thirteen expected judgments, any m/13 figure must be labeled agreement with proposed fixture expectations that were not independently reviewed. Smoke and release must bind the same `judge_config_hash`. Focused review of judging, accounting and the six serving paths specified in CONTRACTS.md §11 is required. Judging-configuration findings and blocking accounting findings land before the first paid judge call; other findings land before PR #9 merges. Neither that review nor the release smoke is claimed complete.

The five historical unresolved holds are plain accounting facts, using UTC recording dates:

| UTC date | Hold | Purpose |
|---|---:|---|
| 2026-09-27 | US$0.08200 | Historical judge calibration; failed third generation after two completed votes. |
| 2026-10-03 | US$0.13192 | Fresh judge calibration; failed J01 generation. |
| 2026-10-08 | US$0.13192 | Fresh judge calibration; failed J01 generation. |
| 2026-10-09 | US$0.13192 | Fresh judge calibration; failed J01 generation. |
| 2026-10-10 | US$0.13192 | Fresh judge calibration; failed J01 generation. |
| Total | **US$0.60968** | Actual charges unknown. |

The owner ruled these internal holds written off outside any permit for this cycle. Their actual charges remain unknown, never zero; historical records and ledger events 43, 44 and 45 remain unchanged. Known historical incurred spending is US$0.729081 and historical owner-confirmed funding is US$75. Automatic replenishment remains off; new missing usage retains unknown actual cost and its full reservation.

Both historical retained failure envelopes have `category=http_error`, `http_status=400`, `rejection_reason=unclassified`; both failed generations used wire5. These original diagnostics are preserved without reclassification. The forthcoming `scripts/judge_smoke.py` may retain per-vote provider request ID and error-type string beside the version-2 diagnostic, never error message/body, headers, key, packet or raw exception. Normal metadata privacy is unchanged; no permanent retention exception is added for other scripts.

Historical October 12 local caps were judge smoke US$2.00, dry run US$1.00 and collection US$32.00 per attempt; each exact command required a separate owner go. Current K2 cap proposals are recorded below. The release/warm-up forecast is US$29.15320, not actual spending. One explicitly marked whole-batch retry is allowed only for an infrastructure cause after correction, under a separately approved second US$32.00 cap; preserve both runs. A third attempt is outside budget. A model-output stop is published as incomplete with no headline; unchanged paid runs are not repeated to select a favorable result.

The collect command implements WP10 shared-service integration. It preserves essential HTTP service checks, N=16, three warm-ups per arm, five fixed repetitions, 80 measured attempts per arm, seed-42/alternating-arm order and judging of scored answered attempts under the existing scoring rules. A serving-side `provider_error` or `deadline_exceeded` is a failed attempt in its denominator and collection continues; five consecutive such failures end it as `collection_untrustworthy`, resetting on any other attempt. Warm-ups count toward that counter but no scored denominator. Judge-side reasons and every other error code keep existing behavior. Budget/call guards and missing-usage reservations remain required. No complete reportable release collection exists; run-1 remains incomplete after its judge output-limit stop.

Tag clean source S `v1.0-source` and deploy S to Render with auto-deploy off. Generation is gated at US$0/day during deploy verification, then admitted at US$0.30/day on the day the public letter smoke passes. Release requires accepted judge smoke, reviewed PRs, complete verified collection and the owner's 32-output audit plus flagged outputs. README then reports exactly what ran, the smoke/release hash match, actual measurements and all unexercised gates. Calendar dates do not replace quality gates. The Decision Log remains the decision record; career evidence-card and inventory integration follow shipping outside this repository.

## Judge capacity amendment, 2026-10-14

Run-1 is an incomplete, nonreportable collection attempt, stopped when the Q04 baseline judge exhausted its 8192 output-token limit on reasoning. Its original evidence, configuration, accounting and failed status are preserved; it has no headline result and is not resumed or retried. K1 replay A at high/8192 again stopped at max_tokens. B at medium/8192 decoded in 3328 tokens and 36.047484 seconds; C at high/16384 decoded in 7531 tokens and 74.294595 seconds. The owner selected C, retaining high effort because the prior calibration used it and medium has not been calibrated.

K2 changes only judge max_tokens to 16384 and its whole-vote deadline to 300 seconds. The current judge_config_hash is `23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8`. Rubric, wire8/packet3, schemas, expected labels, adaptive/omitted thinking, high effort, prices and actor behavior remain unchanged. The full fourteen-vote measurement at actual source and runner `fb9517c308bc4c29a54ec77b6ca05a5adf4cfd6e` under this current hash completed once: all votes decoded, ended with end_turn, matched the model and had complete usage; field agreement was 123/132, exact agreement 8/13, only J04/J06 were fail-to-pass flips, and the full R04 validator passed. Maximum inclusive output was 3368 tokens and maximum elapsed time 35.12659287499264 seconds. This is a measurement, never a passed gate, reportable collection or official N=16 evaluation; it remains bound to that actual source, not this documentation or a later merge/tag. The previous failed gate and combined measurement remain historical under `258529b186b909caf4a86e6b8eda05d495f887e2ffb778891816457db1afcb04`: 125/132 fields, 9/13 exact agreement, J04/J06 fail-to-pass flips and R04 full-validator pass. Only the named J04/J06 s4 factual/citation fields were owner-reviewed with labels upheld; other labels remain proposed and not independently reviewed. J04/J06/J10/J13/R04 informed the rubric, making their agreement in-sample.

At locked rates, A=US$0.027 and J=US$0.21384. Collection reserves 298A+160J=US$42.26040 (proposed cap US$43.00); the dry-run reservation is 5A+3J=US$0.77652 (cap US$1.00); the completed fourteen-vote measurement had a reservation forecast of 14J=US$2.99376 and a US$3.00 cap. These are forecasts, not spending or paid authority. On 2026-10-14 the owner carried her conditional acceptance of the known J04/J06 miss to this configuration and the new measurement figures. She noted that the two changed citation grades do not enter task success; labels and scoring are unchanged. The registered calibration gate remains failed. Measurement records criterion misses and never qualifies a passed gate; operational failures stop for owner ruling. The existing 6000-output/90-second measurement criteria remain unchanged. Publication, required CI, merge, a new annotated release tag, image verification, one new-image dry run and collection run-2 remain pending in that order. Every remote or paid step needs its own exact go; no new-image dry run or run-2 has occurred. Collection stays held. The N04/R06 targeted scan and owner reads plus the 32-output audit remain pending and required. The old v1.0-source tag remains untouched; its image contains the old judge limits/hash and cannot stand in for K2; no publication, retagging or new image is claimed here.

On 2026-10-17, after the preceding October 14 checkpoint, source `v1.0.1-source` and its verified image reached `dry-run-2`. Only `R01` production repeat `0` ran: its actor returned the requested model with `tool_use`, then the usage guard rejected the newly observed response extra `usage.speed="standard"` under the locked SDK. This was a usage-compatibility stop, with no judge call. The run remains failed and nonreportable, with historical `actual_cost=null`, `usage_complete=false` and the full US$0.027 actor reservation; its evidence and run-1 remain unchanged. K3 accepts absent, `null` or exact `"standard"` response speed through shared actor/judge accounting and retained replay, rejecting `"fast"` and all other values. It adds no request `speed`, changes no prices or dependencies and does not settle the historical charge. The judge hash above, failed gate, conditional owner acceptance, label qualifications, targeted `N04`/`R06` reads and 32-output audit obligations remain unchanged. Collection stays held. K3 publication and required CI, merge, a separately authorized `v1.0.2-source` tag and image verification, a fresh image-bound dry run and collection run-2 remain pending in that order, with separate exact authorizations.

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

The response contains `status: "ok"` and the full commit SHA. Startup fails if `SOURCE_COMMIT` is missing or malformed. API documentation routes are disabled. Open `/` for the letter and general-question flows. Provider credentials are read only when an admitted, valid API request needs the client.

## Page and deployment configuration

The page uses inline script and text-only DOM rendering without external assets, analytics or browser storage. Extracted amounts/date are suggestions for correction. Ordinary-scope checklist, rental-period start and reviewed-facts confirmations are explicit actions, never inferred from extraction. Blank fields remain unknown; no previous increase and an unknown previous increase remain distinct. Extraction and confirmed analysis share one attempt ID; general questions use a fresh attempt. Clearing or changing the letter resets its draft confirmations. Errors, including exhausted daily budget and stale rules, appear in plain words. The disclaimer stays fixed, and the footer shows the actual snapshot date and running source SHA. HTTPS or localhost is needed for browser UUID creation. The page and health remain available when POST checks are paused for a stale corpus.

`render.yaml` defines one Docker web service with `/healthz`, one instance and automatic deploys off. The owner supplies `ANTHROPIC_API_KEY` and the exact `PUBLIC_ORIGIN` through the dashboard; neither value is committed. Initial `DEMO_DAILY_USD` is `0`, and `PORT` is `8000`, matching the single-worker Docker command. Admission at `0.30` belongs to the later approved public smoke, not blueprint creation. This configuration has not been deployed or validated through an account operation.

Render documents `autoDeploy: false` as the deprecated equivalent of `autoDeployTrigger: off`. Its default environment variables are available during builds unless otherwise noted, and Docker service variables become build arguments. `ARG RENDER_GIT_COMMIT` supplies the default for `ARG SOURCE_COMMIT`; the existing full-SHA assertion, runtime environment and revision label remain. Local/CI builds may provide the actual Git SHA through the existing build argument. Do not place `SOURCE_COMMIT` in the blueprint or an environment file: the build must bind the actual deployed source. See the official [blueprint reference](https://render.com/docs/blueprint-spec), [default variables](https://render.com/docs/environment-variables) and [Docker behavior](https://render.com/docs/docker).

## HTTP admission and responses

Both POST routes accept only the existing public contracts. The entire UTF-8 request body is limited to 16,384 bytes before JSON parsing. Requests with an Origin must match `PUBLIC_ORIGIN` exactly; requests without Origin share the same global 60-POST UTC-hour limit. In-flight concurrency is two, rejecting excess requests rather than queueing. There is no permissive CORS or authentication claim.

Set `PUBLIC_ORIGIN` to the exact browser origin. `DEMO_DAILY_USD` defaults to `0.30` and accepts lower limits, including `0` while generation is disabled. Reservations are reconciled to known actual usage on their original UTC day, including replies after midnight; unknown usage retains the full reservation and stops further calls. The in-memory counter resets on restart and is not a durable funding guarantee. Each request retains the 45-second deadline and metadata-only logging. Reuse one client attempt ID for extraction and confirmed analysis; each request receives a distinct server trace ID.

Both HTTP routes and local collection reject a corpus older than 90 days. Admission does not affect GET handlers. Unknown routes retain normal 404 responses and wrong methods normal 405 responses; contract errors use the fixed ErrorResponse envelope. There are no uploads, history or public evaluation overrides.

## Local image collection and dry run

These entry points are implemented, but a command is not paid authority. The calibration gate remains failed; the owner's 2026-10-14 conditional release decision is recorded in `CONTRACTS.md`, and collection remains held pending fresh paid execution approval and subject to its disclosure, targeted audit and judge freeze obligations. No judge configuration, rubric, labels or evidence changed in this integration. A future paid command requires a fresh exact-command go with its cap.

Keyless help:

```sh
uv run --locked python -m rent_navigator.eval collect --help
uv run --locked python -m rent_navigator.eval collect-dry-run --help
```

Before building a measurement image, verify the host source tree is clean:

```sh
test -z "$(git status --porcelain)"
docker build --build-arg SOURCE_COMMIT="$(git rev-parse HEAD)" -t rent-navigator:source .
```

The image contains the packaged corpus and runs as uid 65532; it contains no root `eval/` data. Mount `-v "$PWD/eval:/data/eval:ro"`, use `--data-dir /data/eval`, enable network access, and supply a results volume writable by uid 65532. `--source-sha` defaults to the image's `SOURCE_COMMIT`; `--lockfile` defaults to `/app/uv.lock` through the image working directory. Supply the image digest, Docker version, provider access date and pricing URL. OS/CPU/RAM have automatic defaults with explicit overrides. `--source-clean` is a required caller assertion after the host check, not a Git check inside the image.

`collect` uses the unchanged six-warm-up/160-measured protocol. Its current default forecast is US$42.26040, with a cap no greater than US$43.00. Forecasts must be finite, nonnegative and no greater than the cap. The new results root contains `batch-ledger.json`, written before the first operation, plus `collection/` with the existing strictly verified artifacts. Input data are read only; output directories are exclusive. HTTP rate, concurrency and daily quotas alone are disabled. Stale-corpus, redaction, token, call, deadline, citation and scope protections remain.

`collect-dry-run` uses exactly R01, N01 and Q01, production arm, repeat 0, with judging rather than warm-up handling. It constructs no partial CollectionPlan. Its current forecast is US$0.77652 (five actor reservations plus three judge reservations), with a cap no greater than US$1.00. The fixed attempts make exactly five actor and three judge calls. The results root contains the batch ledger plus `dry-run/`. The manifest explicitly marks `dry_run=true`, `reportable=false` and `evaluation_complete=false`; it never runs complete-collection verification or produces release headlines. Each attempt records warm-local serving duration, whole-vote judge wall time, stop reasons, usage, requested/returned identifiers and actual/reserved costs, retaining incomplete accounting safely.

Code is licensed under [MIT](LICENSE); [NOTICE](NOTICE) preserves the separate official-source attribution.

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

`ProviderAdapter.generate` preflights the full prepared request and performs at most one asynchronous, non-streaming generation. It uses the locked SDK's standard Messages API, fixed model settings, no retries and conservative per-call reservations. The shared model policy sets actor input preflight/reservation to **20,000/21,000** and judge to **24,000/25,000**; output limits are **1200/16384** respectively. Actor sampling is sent as fixed `extra_body={"temperature": 0}` because this SDK exposes it through that parameter; judge sampling overrides are omitted. The adapter preserves native tool-use blocks; the separate analysis entry validates and executes them.

`extract_letter(request, *, provider, trace, redact, deadline)` requires a redactor with no default fallback. It redacts before constructing either provider payload, transforms the public extraction schema for provider support, then validates the response against the original strict three-field model. Missing or ambiguous facts remain null. Refusal, truncation, malformed content and invalid fields fail without repair. The caller supplies one absolute `Deadline` and owns `trace.finish(...)`; errors must finish with the safe `ProviderFailure.code`, and cancellation must finish safely before propagating. The development smoke shows this composition.

Usage is captured before output validation. Missing/partial usage or unpriced billing categories retain unknown actual cost and the full reservation; input usage above the reservation preserves known cost. These conditions stop further paid requests for reconciliation. `SpendLedger` is an atomic in-memory batch ledger, not a durable project account or daily admission counter. Endpoint summaries and provider details are not added twice.

`model_policy.MODEL_POLICIES` supplies admission, accounting and runner budgets from one immutable table. Actor calls reserve **US$0.027**, judge calls **US$0.21384**, at the unchanged token rates. The limits apply uniformly by model across extraction, questions, tool selection, final answers and both internal arms. Configuration and pricing hashes cover these model limits; historical evidence retains its original reservations and identities.

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

Further `--live --billing-ready` runs require explicit budget authorization, an API key in the process environment, and confirmed prepaid billing/replenishment settings. The runner checks both approved model IDs, then attempts at most two actor generations, reserving at most US$0.054 for the batch; any failure or mismatch stops it. Do not rerun a paid attempt without checking the remaining authorized call/budget allowance. Evidence directories cannot overwrite an earlier attempt. No judge generation is performed.

The real client is explicitly constructed with the official base URL and `max_retries=0`; SDK/transport logging is disabled and custom-header environment overrides are rejected. Never place keys in source, command arguments, evidence, or logs. Default tests and PR CI require neither credentials nor provider access.

## Internal analysis and restricted CLI

`agent.answer(request, *, provider, corpus, retrieve, redact, deadline, context, sink, arm="production")` owns and finishes exactly one analysis trace. It requires an injected redactor and the caller's absolute deadline. Question mode retrieves the original question once locally, sends only redacted question text, and makes one actor call without tools. Its initial evidence is the union of fixed references from all validated rules and the unchanged top-five query seeds, deduplicated in corpus order. The current foundation contains 21 chunks / 5,092 words; this composition is tested offline and has no measured answer-quality result. Confirmed notice/rent facts retain only their original ranked seeds initially, then require a native provider tool call with exactly matching arguments, one execution of the existing calculator, and a second generation with the matching tool-result ID and tools disabled. Invalid calls and outputs fail without repair or retries.

The second request includes every evidence chunk mapped by every executed rule, deduplicated against the original top five. Every production statement must cite an initial-context or executed-rule chunk; the server resolves the canonical URL, heading and snapshot. Fixed reference availability does not imply that any rule was executed. This guarantees provenance and citation coverage, not semantic support. The actual calculator result remains authoritative. All prepared history, tools, evidence and output schema enter token preflight; evidence is never truncated to pass the budget limit.

Rent final requests also receive exact CAD display strings for the confirmed current rent, proposed total new rent and the calculator's exact new-rent ceiling. The server shifts cents by two decimal places without rounding or depending on decimal precision, preserving fractional cents and nulls. Both arms receive the same context after the unchanged native tool result. Final instructions distinguish the total rent ceiling from the increase, prohibit additional calculated amounts and monthly assumptions, and preserve uncertain results. Production citation instructions require a named statutory section's supplied chunk or omission of that section reference. These instructions do not validate the meaning of generated prose.

The trusted internal `baseline` arm omits retrieval and evidence passages while preserving the same facts, tools, model, budgets and deadline. Citations may be empty; supplied IDs must still belong to executed rules. It is tested with fakes only and is not a public request or CLI option. The comparison measures the whole grounded system against the same tools/model without external context; it does not isolate query ranking's causal effect. MRR@5 and NDCG@5 continue to use only the original ranked seeds. `agent_config_hash(mode, arm)` identifies fixed prompts, tool/output schemas, stage choices, display text, context-composition policy and provider configuration without request text or identifiers.

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

Explicit `--live --billing-ready` uses the same production path for one fixed fixture, with at most two actor generations, two preflights, a US$0.054 batch reservation, no judge and no retries. It requires separately authorized budget, confirmed prepaid billing with replenishment disabled and a key provided only through the process environment. Any failure stops the attempt and preserves incomplete evidence. A passing mechanical check still requires review that the generated explanation describes both failures faithfully; it is not a legal gold case, evaluation result or proof of general safety. No live success is claimed by the offline fixtures.

The first live analysis attempt on source `599c44b6777414a27f31d28a7e329819333d7299` stopped correctly when the complete second request counted **12,289 tokens**, above its then-current 7,000 limit. It produced one correct native tool call and the expected calculator result, but no final answer. Its complete usage cost **US$0.004659**; the original US$0.022 batch ceiling, US$0.011 entered-call reservation and original hashes remain unchanged in the retained [PR #6 evidence](https://github.com/angelaqaaa/rent-navigator/pull/6).

Following independent offline acceptance and funding confirmation, one live smoke on **`93399f4b6e66f62d9a70f4de01b3dfc10c5724da`** completed both counts and generations without retries. Its final answer correctly described the 60-day notice failure and 2.4% versus 2.1% guideline failure, but incorrectly rendered cents as dollars: **$204,800 / $204,200 / $600** instead of **$2,048 / $2,042 / $6**. The original tool result remained correct and server citations resolved. **That attempt failed explanation acceptance because of the monetary error**, despite the runner's mechanical PASS. Complete billed usage was **15,703 input / 800 output tokens**, costing **US$0.019703** against US$0.038 reservations. Both raw returns and failed acceptance evidence are retained; the result was not repaired or rerun.

The subsequent money-context repair passed independent offline review. It changes the request context and instructions while preserving the original tool inputs/results and complete evidence; its tests cover fractional, null and large-integer amounts. A separately authorized live smoke on **`5c3d7642c9b6e6f03f90caa993caa29ce29c39ad`** then completed two counts and two generations, correctly displaying **$2048.00 proposed rent / $2042.00 maximum total new rent**, the 60-day notice failure and guideline failure. It additionally stated a correct **$6.00 difference** not present in the supplied money strings, a deviation from the instruction against other calculated amounts. Mechanical PASS does not establish strict prompt compliance; final explanation acceptance awaits independent evidence review. Complete usage was **15,911 input / 798 output tokens**, costing **US$0.019901** against US$0.038 reservations. All three attempts remain preserved; none was silently repaired or replaced.

At the WP6 checkpoint, the owner confirmed the additional US$5, bringing prepayment to **US$30**, with automatic replenishment disabled. Further paid work requires a new explicit authorization. The historical US$50 allocation was hosting14, development/CI/measurement21, demo9 and contingency6; the current funding confirmation is recorded separately below. At the WP6 checkpoint, future reservations totalled **US$20.313**, including **15 development slots**; cumulative incurred cost was **US$0.045685**, leaving **US$0.641315** within development. Subsequent authorized evaluation receipts retain their separate charges and reservations. These are budget calculations, not a queried provider balance; the full US$9 demo allocation remains protected. The documentation update does not change the measured source, and no live run was repeated for it.

## Offline evaluation and collection

Angela approved the 16 cases in `eval/gold.jsonl`, their prepared answers and official-source mappings at **2026-09-23T14:34:14.302Z**. The dataset SHA-256 is `575cfd2b66465633f7cb04b44eb8455293c1a9c24a4911c0482879313d9b82cb`. The approval file binds every dataset byte and the corpus hash. Read-only validation checks strict schemas and canonical evidence without calling calculators, searching, scoring or contacting a provider:

```sh
uv run --locked python -m rent_navigator.eval validate-gold --data-dir eval
uv run --locked python -m rent_navigator.eval plan \
  --data-dir eval --output-dir /tmp/rent-navigator-plan
```

Output directories must be new. `plan` only writes the fixed schedule: three warm-ups per arm, then five paired repetitions of the 16 cases, using one seed-42 random generator and alternating arm order. It makes no calls and does not approve the candidate.

The offline command verifies approval for the exact hash, compares all ten tool results and their ordered checks, validates all sixteen cases, and measures deterministic MRR@5/NDCG@5 on the six questions. It requires passing evidence from the existing security suite:

```sh
uv run --locked pytest tests/test_security_pipeline.py \
  --junitxml=/tmp/rent-navigator-security.xml
uv run --locked python -m rent_navigator.eval offline \
  --data-dir eval --output-dir /tmp/rent-navigator-offline \
  --source-sha "$(git rev-parse HEAD)" --lockfile uv.lock \
  --security-report /tmp/rent-navigator-security.xml
```

Missing or mismatched approval makes `offline` exit nonzero before tool or retrieval execution and preserve a failure artifact. Approval cannot be inferred from passing unit tests. `eval/activation.json` records whether the comparison baseline is pending or active. Pending defers only the baseline comparison. Activation requires a complete passing live bootstrap and a separate reviewed attachment of `eval/baseline.json`; numbers are never inferred from scripted tests. `verify-offline --help` describes the required source, manifest digest and prerequisite conclusions for independent artifact verification.

`eval.runner.run_attempt` uses the actual extraction and analysis operations through injected messages and judge ports. It preserves actual tool execution evidence, ranked retrieval IDs, failed attempts, separate serving/judge accounting, and distinct traces sharing one attempt ID. An extraction mismatch ends the attempt without correcting facts. Missing usage remains unknown, retains reservations and stops collection for budget reconciliation. Full-content recording is restricted to declared synthetic scenarios and prepared requests; ordinary trace records remain metadata-only.

Analysis endpoints and result rows separately retain ranked seed IDs, fixed foundation IDs and exact initial-context IDs. Each synthetic raw event carries mandatory context provenance; provider-call, extraction and judge metadata use explicit empty context lists. A prepared endpoint does not prove that an analysis request reached the recording port. Accepted replay independently rebuilds the fixed index and recomputes ranked seeds before checking raw/row/endpoint agreement; older records missing the new fields are not accepted as current evidence. Historical artifacts remain unchanged under their original identities.

`eval.collection.collect_synthetic` exercises the serial collection protocol through injected ports. It writes the plan first, persists each attempt, and retains partial runs on interruption. Its seven artifacts are `plan.json`, `manifest.json`, `results.jsonl`, `warmups.jsonl`, `metadata.jsonl`, `raw-provider.jsonl`, and `summary.json`. Verification checks their hashes, planned entries, observations and trace accounting. Synthetic manifests always record `execution_mode=synthetic`, `reportable=false`, and `evaluation_complete=false`. These tests establish the harness protocol; their timing, costs and fake judgments are not project measurements. The future measured boundary is extraction plus analysis through trace completion inside the recorded image, excluding judge work, HTTP/browser transport and human delay.

`eval.collection.collect_real` uses the same six-warm-up/160-measured schedule and serving operations with the concrete judge. It keeps judge raw responses and metadata separate from serving work and derives completeness from verified observations, usage and judgments. No complete reportable release collection exists; run-1 remains incomplete after its judge output-limit stop. The judge receives approved claims/evidence and actual statements/results, without arm or unrelated request identifiers; citations can still reveal the arm. Failed judge output retains its billed cost without inventing a judgment.

Historical judge wire policy version 5 required complete original-text partitions, exact contiguous answer/source quotations and explicit relation observations in the same single scoring vote. Its decoder checked trusted quotation provenance, full target reconstruction, ordered references/pointers and agreement between observations and explicit grades. Ordinary claim coverage could span statements; factual audits used the full evidence union while citation audits used only the statement's own passages. Trusted policy claims had a separate output-invariant branch. The current source uses grade-only wire7 with shared ID/grade arrays; the dated revision above records its changed configuration. No anchors, bases or quotes enter the emitted wire. Internal grades, gold, rubric and thresholds remain unchanged; historical raw evidence stays unchanged and outside normal metadata.

Every current judge input requires an explicit typed approved-facts envelope or null. Ordinary facts come from the original approved gold request, identically in both arms, including letters; fixed security facts come from the independently loaded security request and agree exactly with its trusted context. Required `fact_fields` name only real scalar/null leaves below `/approved_facts/facts`. Positive/conflicting factual audits need a source, tool or fact witness; cited audits still need a pertinent own-source quotation. These are conditional scenario premises, not evidence of real tenancy or execution. Unknown service remains unknown, and calculated deadlines remain tool evidence. Serving contracts and internal grades are unchanged.

The separate current [derived calibration fixtures](tests/fixtures/judge_calibration_v2/) preserve all thirteen original targets, tool values, evidence and intended labels while adding facts from fixed approved N04/R04 inputs. The original sixteen historical fixture files remain unchanged. Derivation and replay verify the original fixtures plus the external approved gold/approval/corpus, so locally rehashed foreign facts cannot become trusted authority. Fresh synthetic size worksheets describe current character/byte representations; they do not establish token fit, output sufficiency or model accuracy.

Judge output is 16384 with one absolute 300-second budget beginning before assembly and covering count, reservation, generation and final successful accounting/evaluation validation, with no second vote or retry. Actor/extraction and shared client/default deadlines remain 45 seconds. Provider-call metadata/raw records are retained immediately; only the endpoint summary is deferred locally until validation and the final deadline check, so late expiry publishes one failure endpoint with no judgment and preserved accounting. The candidate endpoint duration precedes that last validation and is not a measurement of all later work. Complete output fit and real provider acceptance have not been established for this wire. The thirteen fixed [supplemental calibration fixtures](tests/fixtures/judge_calibration/) retain their historical wire3 outputs, source configuration, sizes and unchanged expected labels. Fresh offline packet3/wire5 examples are derived separately; the current decoder rejects old-wire votes. Exact quotations and internally consistent observations do not establish entailment, faithful bases or actual model quality. The previous calibration's grade/basis failures and observed answer-quality findings remain open.

## Historical funding and live-gate design

The following checkpoints preserve their original amounts, identities and requirements. The October 12 revision above governs the current cycle; these historical requirements do not claim that private prerequisites, baseline activation or the live gate have completed.

At the accepted funding checkpoint, confirmed cumulative provider prepayment was **US$55**, allocated to development 42, protected demo 9 and unallocated reserve 4. This is owner-confirmed funding, not a queried balance or execution grant; the prior 50/36/9/5 allocation is historical. The completed v4 diagnostic cost $0.429937; settled incurred at that checkpoint was $0.676401, active/unknown hold 0. Historical responses and grades remain preserved: answer quality was not accepted, including Q04 coverage/own-citation misses, N04's deadline contradiction and R03's refusal.

Ledger events 27–28 include the thirteen single-vote calibration packets (13J=$1.066) and another complete fixed 22 diagnosis (26A+7J=$1.276). Together with release/warm-ups, three full gates and seven generic allowances, future obligations at that checkpoint totalled **576A+304J=$40.480** at A=$0.027/J=$0.082. Including incurred, development exposure is **$41.156401**, leaving **$0.843599** inside 42. A later calibration grant transfers the existing $1.066 from future to active, leaving $39.414 future; it does not add the obligation twice. Historical ledger bytes remain unchanged, and this funded plan issues no execution authority.

The subsequent single-use calibration stopped after two completed votes and a third generation failure; the remaining ten items were unstarted. Independent review rejected judge quality, confirming six grade errors and additional inaccurate bases. Partial settlement recognizes $0.05268, bringing known incurred to $0.729081, with $0.082 unresolved, active reservation 0 and future obligations $39.414. The total actual charge remains unknown. The consumed grant and original evidence remain unchanged.

Historical unfunded capacity reforecast, September 30, 2026: judge output 8192 makes J=$0.13192 while A stays $0.027. The complete remaining 576A+291J inventory reprices to $53.94072; a fresh thirteen-item calibration is an additional $1.71496, not a restored consumed slot. Known incurred $0.729081 plus the preserved historical $0.082 hold give development exposure $56.466761 including that fresh calibration. Protecting demo9 and unallocated4 requires $69.466761, exceeding owner-confirmed55 by $14.466761. At that checkpoint these were prospective calculations, not posted ledger events, owner funding confirmation or execution authority. Its ledger31 and original $39.414 forecast are retained unchanged; the then-current development42/provider55/held==0 guards were closed. The later confirmed funding and narrow private admission policy appear below; neither retrospectively authorizes that checkpoint.

Current confirmed funding, October 1, 2026: the owner confirmed a completed additional **US$20** provider prepayment, bringing cumulative owner-confirmed funding to **US$75**, allocated to development/CI/measurement **US$62**, protected demo **US$9** and unallocated provider reserve **US$4**. Hosting14 and separate cash contingency6 remain unchanged, making the total cash allocation95. This is a human funding confirmation, not a queried account balance or execution grant. Automatic replenishment remains disabled.

Ledger34 preserves the complete original31-event prefix byte-for-byte and adds only funding and planning events32–34. Known incurred remains **$0.729081**, the consumed historical calibration's unknown-cost hold remains **$0.082**, and active reservation remains **$0**. At A=$0.027/J=$0.13192, existing576A/291J future obligations are **$53.94072**: release/warm-ups298A/160J=$29.15320, three gates231A/117J=$21.67164, seven generic allowances21A/7J=$1.49044 and all open22 diagnostics26A/7J=$1.62544. A distinctly NEW13J calibration adds **$1.71496**, making future576A/304J=**$55.65568**. Development exposure is **$56.466761**, leaving **$5.533239** inside62; demo9/reserve4 remain protected. No consumed slot or unstarted historical item is restored. A later separate reservation transfers the same1.71496 from future to active, leaving future53.94072, rather than counting it twice.

The independently reviewed historical-hold policy permits only one new private thirteen-vote calibration with externally validated root admission evidence for the exact old run and its0.082 hold. Default nonzero-hold rejection and production held==0 remain. Current ledger34 has no new reservation event35, root admission snapshot or exact grant, so it cannot authorize execution. Enforcement awaits independent implementation acceptance. New missing usage keeps the full fresh0.13192 reservation and permanently stops the fresh run; the old hold is not seeded into its SpendLedger. Production permits still require completion and settlement of all22 diagnostics and separate exact-run authority; a private admission handle cannot authorize production.

Future failure audit records now require `{code, diagnostic}`, where diagnostic version 1 contains only a fixed category and a validated HTTP status or null. Typed failures are classified before normalization in serving and judge recording paths; strict replay rejects missing or inconsistent diagnostics. Public errors and metadata traces are unchanged. This cannot recover the historical third-call cause, prove a schema rejection from HTTP 400, resolve its charge, improve judge accuracy or authorize another request. Historical records retain their original source-bound format; Retained v1 diagnostic calibration evidence keeps protocol version 3 and its original source. Separate historical prospective v2 helpers/preparation retain protocol4 and their original capacity source. Historical v3/v4 helpers and their prospective preparation keep protocol5 and their original fact-authority source identities. Current v5 helpers/preparation bind private protocol6 to the accepted historical-hold policy, funding75 confirmation and exact planning ledger34; packet3/wire5/diagnostic1 and model-facing requests remain unchanged. These are offline enforcement candidates awaiting independent implementation review, not paid authority.

`LivePermit` retains strict schema version2, actual call counts, floor$0.232356, held exactly zero and source/run/expiry binding. At the current capacity, a pending-baseline bootstrap reserves $7.22388 and requires $45.09140 afterward. Confirmed development62/provider75 makes its synthetic funding arithmetic affordable; the actual historical$0.082 hold still rejects before client construction. Current known spend and complete22-diagnostics/quality prerequisites must be independently settled before a real production permit. A private historical-admission handle cannot cross that boundary; funding alone issues no grant. Offline positive tests confer no real authority. Baseline activation, protected paid execution, merge and release remain pending. Historical sizing counts keep their original identities; changed judge packets require new separately authorized counting and output checks. No real count or generation is part of this offline amendment.

The protected paid execution ceiling is 240 minutes; nonpaid planning/checks do not approve permits, release a hold or create funds. Hypothetical later-regression tests (zero future gates/seven generic slots) and private synthetic funding fixtures demonstrate arithmetic only, separately from the actual remaining inventory.

`live-gate` is a protected CI entry, not a general local demo command. It validates a single-run permit and the current offline evidence before creating a provider client. The fixed order is all 16 approved cases twice in production, followed by S01–S05/S07/S08 once; S06 remains covered by the offline security suite. Known incorrect results remain in the complete batch. Infrastructure failure, unknown billing, incomplete observations or invalid judging stop further paid calls and preserve partial evidence.

A passing bootstrap needs at least 28 of 32 task successes, all seven safety cases passing, and no critical citation, tool, false-pass or policy failures. Rejected native proposals are inspected separately from actual tool execution. Later gates use `max(28, B−2)` against the original baseline. These 39 attempts produce gate evidence, not release success/latency/cost headlines.

The `live-eval` environment holds the provider credential and a permit binding one repository, source, run/attempt, batch and funded reservation. Reservations are written before generation and reconciled once against known usage. Missing usage keeps its reservation. A rerun cannot automatically purchase another batch. Read-only artifact verification checks producer provenance, archive and payload digests, source relationships, original observations, judging and accounting. Live artifacts are retained for 90 days; missing or expired evidence fails closed.

A baseline attachment may change only the two baseline/activation files directly after the scored source. Documentation reuse requires proved unchanged behavior from both the trusted base and a passing checked ancestor; an accepted tree-identical merge may retain the original scored identity. Ordinary behavior changes cannot reuse an older success merely because configuration hashes match. `verify-live --help` lists the artifact and producer identities required for verification. Passing offline tests alone is not live acceptance; current results are recorded by the required workflow summary and its retained artifact.

## Verify

```sh
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

`uv.lock` records exact dependency versions and distribution hashes. Strict mypy covers source, tests and CI scripts. Tests cover public JSON boundaries, synthetic trace accounting, source/chunk integrity, rule resolution, deterministic extraction, query tokenization, offline rebuilds, notice boundaries, calendar anniversaries, exact rent caps and form/status combinations. Synthetic timing/usage values are not serving measurements.

The unfiltered PR workflow runs basic checks, installs the wheel outside the checkout with locked dependencies, and runs `tests/smoke_corpus.py`. It also validates evaluation data through the installed package. `offline-eval-run` executes approved offline checks and uploads complete or failed evidence. The always-running `offline-eval` summary requires successful checks, Docker and offline execution, then verifies producer identity/digests, source/data/config hashes, exact results, security XML and activation state. Missing, skipped, cancelled or stale prerequisites fail. Gold approval alone is not offline acceptance. main currently requires `checks`, `docker` and `offline-eval`; `live-eval-gate` is deferred as a required check this cycle. Its original evidence-verification design remains available, with the variable guards approved above pending implementation. No passing live gate, activated retrieval floor or answer-quality regression blocking is claimed. Workflow success does not replace the revised focused review and release audit.

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
