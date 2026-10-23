# Results and evidence

This index distinguishes original collection evidence, historical failures, calibration measurements, owner decisions, the deterministic computed-field scan and later public-smoke receipts. Original grades and failed outcomes remain unchanged. No owner correction is silently substituted into a retained result.

## Evidence map

| Location | Purpose |
|---|---|
| `october-release/run-1/` | Original incomplete collection and batch ledger |
| `october-release/run-2/` | Complete collection with an unusable headline comparison |
| `october-release/run-3/` | Complete post-correction collection used for current judged results |
| `october-release/dry-run-1/` through `dry-run-4/` | Original image-bound integration attempts, including the failed usage-compatibility attempt |
| `october-release/judge-resmoke-5839f98-j3-20261014/` | Registered calibration gate that stopped and remains failed |
| `october-release/judge-measurement-77f7c2f-j3-20261014/` | Separate historical completion measurement |
| `october-release/k2-calibration-measurement-1/` | Current-configuration measurement after the failed gate |
| `october-release/owner-audit-run-3/` | Original selection, reading material and unfilled audit templates |
| `october-release/owner-audit-run-3-record/` | Four owner-approved decision files and a new subset inventory; assisted-pass provenance remains separately retained |
| `october-release/source-50190/` | Unchanged source configuration and snapshot metadata |
| `october-release/public-letter-smoke-v1.0.3-source/` | Original synthetic HTTP smoke artifacts and later verification/accounting/reset receipts |
| `run-3-targeted-scan/` | Separately committed rule, provenance and scan result |

Collection roots retain `batch-ledger.json` and the nine original `collection/` files. Dry-run roots retain their ledger and six original `dry-run/` files. Manifest-relative filenames are unchanged. Historical local paths inside immutable evidence remain provenance, not portable navigation. Resolve collection rows through this index using their recorded line numbers and hashes.

## Source identities

| Evidence | Actual source |
|---|---|
| Run-1 and dry-run-1 | `3d9299b01e973a843976ad13dcae9b544e217db4` |
| Dry-run-2 | `57fd0410899311d5a2f1d850f7f019130aa4fe67` |
| Run-2 and dry-run-3 | `e8632d2a2e2d8a45e9cedbd2f360b58f88b21695` |
| Run-3, dry-run-4 and public smoke | `50190c8173c8f36193be15b4f857df8b724094fa` |
| Current calibration judging source and runner | `fb9517c308bc4c29a54ec77b6ca05a5adf4cfd6e` |
| Separately committed scan implementation/output | `c19e4137e02996a0c380192aaccd9cfa94dda7ff` |

The measured/deployed source is `v1.0.3-source`. The scan commit is not the serving source. Each original manifest retains its actual source, environment and configuration; documentation does not rebind it to a later commit.

The source receipts copy exactly `.python-version`, `Dockerfile`, `pyproject.toml`, `uv.lock`, `render.yaml`, `.github/workflows/ci.yml`, `eval/activation.json` and `src/rent_navigator/corpus/manifest.json` from committed source `50190c8173c8f36193be15b4f857df8b724094fa`, retaining paths. They are documentation receipts, not runtime inputs. The lock hash is `8de12f5eae66dbbb2744fda68316826aba2e82d9b4d7f8d00764bc6dd0f5b80a`.

## Run-3 method and original results

Sources: [plan](october-release/run-3/collection/plan.json), [manifest](october-release/run-3/collection/manifest.json), [rows](october-release/run-3/collection/results.jsonl), [warmups](october-release/run-3/collection/warmups.jsonl) and [summary](october-release/run-3/collection/summary.json).

The collection contains 16 approved development cases, both arms and five repetitions: 160 measured attempts, 80 per arm. Three warmups per arm are excluded from scored/latency denominators but retained in spending. The serial schedule uses seed 42 and alternating arm order. No unfavorable repetition or failed attempt is removed.

`collection_complete`, `evaluation_complete` and `reportable` are true. These completeness flags do not mean every answer was correct or every release condition passed.

| Original judge result | Production | Baseline |
|---|---:|---:|
| Success | 64/80 | 55/80 |
| Successes by repetition | 12, 13, 13, 13, 13 | 11, 11, 11, 10, 12 |
| Repetition rate range | 75%-81.25% | 62.5%-75% |
| Tool cases | 39/50 | 44/50 |
| Q&A | 25/30 | 11/30 |
| Incorrect | 16 | 25 |
| Answered | 80 | 80 |
| Refusals / errors | 0 / 0 | 0 / 0 |
| Factual-hallucination flags / answered | 11/80 | 16/80 |

Production citation failures were 24/80. Baseline citation compliance is not imposed; its zero counter is not a successful-citation rate. Task success uses exact assertions, required claims, factual support, false-pass and policy results. Own-citation support remains separate. Original rows and judge objects are unchanged.

The cases were approved before scoring but reused during development. This is not a representative population sample or a held-out product benchmark. The baseline retains the same facts, tools, actor, budgets and deadline without external reference passages. The comparison concerns the whole grounded system and citation policy, not the isolated causal effect of query ranking.

## Latency, environment and cost

The [manifest](october-release/run-3/collection/manifest.json) records:

| Environment field | Recorded value |
|---|---|
| CPU architecture | `aarch64`, local ARM64 |
| Operating environment | `Linux-7.2.9-orbstack-00391-g2b298d05fbd4-aarch64-with-glibc2.36` |
| RAM | `8392929280` bytes |
| Docker | `29.8.2`, build `7fc2dff` |
| Source clean | `true` |
| Provider access date | `2026-10-19` |
| Image digest | `sha256:c9293278a2a7237288098296f5b4a8e9a375c66cbf76dfab88a30fdeacda9273` |

The local image does not establish Render's architecture or digest. Those deployment properties have not been independently measured, so no matching-image or assumed AMD64 claim is made.

The [summary](october-release/run-3/collection/summary.json) retains all 80 production latency samples. Nearest-rank p50 is `13153.39164598845 ms`; p95 is `16180.895197001519 ms`. The root README rounds these to 13.153 and 16.181 seconds. The boundary is the warm-local shared serving operation through return or safe failure and trace completion. Letter latency includes extraction plus analysis. Preflight, retrieval, calculation, generation and validation are included; HTTP/browser transport, human confirmation delay and judging are excluded.

| Project-rate cost, USD | Production | Baseline | Combined |
|---|---:|---:|---:|
| Measured serving | 1.513300 | 0.502575 | 2.015875 |
| Serving cost per 100 measured attempts | 1.891625 | 0.62821875 | Not pooled |
| Judging | 3.464800 | 3.250928 | 6.715728 |

Warmup serving cost was US$0.075489. Total collection cost was US$8.807092, equal to measured serving plus warmups plus judging. The [batch ledger](october-release/run-3/batch-ledger.json) records a US$43.00 cap and US$42.26040 forecast, distinct from spending. Historical attempts, calibration and public smoke are separate. Hosting spend is unmeasured. These are fixed-rate token-accounting figures, not an invoice or queried balance.

Cost per hundred is `100 × actual measured serving token cost / measured attempts`, including extraction and billed failures. Counts have zero token cost but their latency is included. Endpoint summaries are not added to their provider-call details. Missing usage remains unknown with its full reservation.

## Owner audit and matched denominators

Sources: [selection](october-release/owner-audit-run-3/selection.json), [owner record](october-release/owner-audit-run-3-record/owner-record.json), [CSV](october-release/owner-audit-run-3-record/owner-record.csv) and [decisions](october-release/owner-audit-run-3-record/owner-decisions.csv).

Selection is the union of 32 first-repetition outputs and all 27 factual-hallucination flags. Six overlap, giving 53 distinct outputs and 21 additional flagged outputs. Production contributes 24 reviewed outputs; baseline contributes 29.

First-repetition overall agreement is **30/32**. Additional flagged-output agreement is **20/21**.

Every output was read against the gold expected fields, required claims, the retained tool result, the cited and canonical passages and the judge rubric. An assisted first pass over all 53 outputs ran three read-only review passes: overall classification; citation support and policy; and an adversarial pass over every overall agree. Each item was classified agree, disagree or unsure with a written reason. The owner decided every item any pass marked disagree or unsure, covering 19 outputs and 26 decisions, and read the four N04/R06 first-repetition outputs in full. Every value in the record is the owner's decision. Overall agreement means agreement with the judge's task-success classification; citation support does not enter task success. No provider call was made for this audit.

| Matched reviewed scope | Production judged | Production audited | Baseline judged | Baseline audited |
|---|---:|---:|---:|---:|
| First repetition | 12/16 | 11/16 | 11/16 | 10/16 |
| Entire selected set | 12/24 | 11/24 | 11/29 | 11/29 |

Citation agreement is 29 agree, six disagree and 18 not applicable because the output cites nothing. Policy agreement is 53 agree and zero disagree. Agreement is not factual, citation or safety success.

Only these overall classifications differ:

- `run-3-q05-baseline-repeat-0`: original success; owner incorrect because invented form titles conflict with corpus titles.
- `run-3-q05-production-repeat-0`: original success; owner incorrect because “not occupied before November 15, 2018” misstated the statute's “on or before” boundary and omitted the residential-occupation qualification.
- `run-3-r04-baseline-repeat-3`: original incorrect; owner supported because the factual sentence was supported by the supplied result.

These are recorded owner decisions, not new legal adjudications. They are not propagated to other repetitions. No corrected success total over all 80 attempts per arm is asserted.

The [owner notes](october-release/owner-audit-run-3-record/README.md) record three inconsistencies:

| Identical material | Original grades | Owner position |
|---|---|---|
| R04 baseline s4 factual text | Supported in repeats 0, 1, 2, 4; unsupported in 3 | Supported |
| R04 baseline s3 text and cited passages | Citation supported in repeat 0; unsupported in 3 | Unsupported |
| N02 production s3 text and cited passages | Citation supported in repeats 0, 4; unsupported in 1, 2, 3 | Unsupported |

## Owner record copy and subset inventory

The publication subset is exactly `owner-record.json`, `owner-record.csv`, `owner-decisions.csv` and `README.md` under `october-release/owner-audit-run-3-record/`, each byte-identical to its original, plus a new `SHA256SUMS` listing only those four files.

The original `root-first-pass.csv`, `root-cite-policy.csv` and `root-refute.csv` remain separately retained provenance and are not included. They are not competing final decisions. The original eight-file directory and seven-entry inventory remain unchanged. The original inventory is not copied into this subset.

The preserved owner README is historical. Its references to omitted provenance and its then-pending scan note remain unchanged. This index and the committed scan explain the later state; the owner record is not rewritten.

The original full-record inventory hash is `b9e45daf8fe445d22ede07a902d2ae796c300fd2697bb75ed2eac26b92f2a9e2`. The new four-entry, 329-byte subset inventory hash is `9c263b6d98e692612fd9852cd9ec674ca55a134fa9b01aa9adaf8e1e50c893d2`.

The scan remains bound to the original full-record inventory and original input hashes. The subset must not replace that binding. Exact reproduction of the original scan input directory requires the separately retained full record; this subset does not recreate it. Historical path fields remain provenance. Repository navigation uses recorded source line numbers in `october-release/run-3/collection/results.jsonl`.

## Targeted N04/R06 scan

Sources: [rule](run-3-targeted-scan/RULE.md), [rule JSON](run-3-targeted-scan/rule.json), [scan](run-3-targeted-scan/scan.json) and [readable result](run-3-targeted-scan/flagged-answers.md).

Rule `computed-value-availability-v1` has canonical hash `abbefab0ce5864b2b0feedfa3965fe4d5695fa73ca80849ef3670998b7a4c5b9`. It was frozen before scanning every statement in every retained judge-successful N04/R06 answer across both arms and all five repetitions, excluding warmups.

| Case and arm | Retained rows | Eligible answers | Statements examined |
|---|---:|---:|---:|
| N04 production | 5 | 0 | 0 |
| N04 baseline | 5 | 5 | 12 |
| R06 production | 5 | 5 | 30 |
| R06 baseline | 5 | 5 | 20 |
| Total | 20 | 15 | 62 |

There were zero candidate statements and zero flagged answers. No flagged audit IDs or statements exist to list.

Finite aliases cover `deemed_served_on`, `notice_days`, `earliest_notice_on`, `latest_deemed_service_on`, `latest_dispatch_on`, `earliest_spacing_on`, `guideline_percent` and `cap_cents_exact`. A field reference must connect directly to an availability predicate, and that same observed result field must be non-null. Expected values or unrelated non-null fields cannot trigger a flag.

Recognized negation, conditional and scope guards reduce obvious false matches. Pronouns, implicit denials, unlisted paraphrases and unusual quotation/negation can be missed or overflagged. This is a finite lexical screen, not semantic clearance.

| Case and arm | Judged success, all repetitions | Audited success, reviewed subset |
|---|---:|---:|
| N04 production | 0/5 | 0/5 |
| N04 baseline | 5/5 | 1/1 |
| R06 production | 5/5 | 1/1 |
| R06 baseline | 5/5 | 1/1 |

The owner read the four first-repetition N04/R06 outputs in full. Additional N04 production reads already belong to the selected audit. The zero-candidate scan added no owner reads and does not establish review of unflagged repetitions. `owner_reads_complete` and `audited_verdicts` remain null.

## Judge configuration and calibration

Run-3 uses grade-only wire 8, `shared_id_keyed_grades`, and packet 3. Current high effort/adaptive thinking with omitted reasoning display has a 16384-token output limit and one absolute 300-second whole-vote deadline. The collection and current measurement share judge hash:

```text
23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8
```

The [current measurement](october-release/k2-calibration-measurement-1/summary.json) remains bound to actual judging source and runner `fb9517c308bc4c29a54ec77b6ca05a5adf4cfd6e`. All 14 votes decoded, ended with `end_turn`, matched the requested model and had complete usage. Field agreement was 123/132 and exact expectation agreement 8/13. Only J04/J06 were fail-to-pass flips; the full R04 validator passed. Maximum inclusive output was 3368 tokens and maximum elapsed time 35.12659287499264 seconds, within the unchanged 6000-token/90-second observation bounds.

This was a measurement after a failed gate with `gate_passed=false`. The registered gate remains failed. The owner conditionally accepted the known J04/J06 residual miss for this release instead of further item-driven tuning. Collection ran under this actual configuration by that decision, not a gate pass.

Only the named J04/J06 s4 factual/citation fields were owner-reviewed, with labels upheld. Other expectations remain proposed and not independently reviewed. J04/J06/J10/J13/R04 informed clarification, so agreement on those packets is in-sample. The subsequent assisted owner audit is an out-of-sample judge check relative to calibration packets, not representative of all outputs. Citation disagreement alone does not change task success.

The [historical failed gate](october-release/judge-resmoke-5839f98-j3-20261014/summary.json) and [completion measurement](october-release/judge-measurement-77f7c2f-j3-20261014/summary.json) retain judge hash `258529b186b909caf4a86e6b8eda05d495f887e2ffb778891816457db1afcb04`. Combined they recorded 125/132 fields, 9/13 exact agreement, J04/J06 flips and an R04 validator pass. These do not replace current figures. The judge remains frozen; a new failure type requires an owner ruling before change or further paid work.

## Historical wire and review checkpoints

The October 12 revision adopted grade-only judging with exactly `required_claims`, `statements`, `false_pass` and `policy_violations`. Grade domains and scoring stayed unchanged; per-part anchors, bases and quotes became historical. Anchor wire 6 remains history.

The first grade-only hash was `1b4f130873e00fcd77e30b96756b060b947e43c5f39d83d1189f6bfe32ac69d2`, using wire 7. The owner classified a separately observed wire-6 rejection as compiled-grammar size rejection after reading it once. Later J03 wire-7 acceptance proved grammar acceptance for that packet, not complete decoding, grading or release acceptance.

Wire-7 worksheets recorded maxima of 1288 schema bytes, 26 enum members and raw depth 5 against then-frozen ceilings of 1450 bytes, 32 members and depth 5, with headroom 162 bytes/six members/zero levels. Unions and optional properties were zero. The rejected-wire6 control was 14071 bytes/94 members/depth 14. Raw depth counts stored objects/arrays from root depth 1, definitions once without reference expansion. These are project limits, not inferred provider bounds. Token measurements stayed null and worksheets retained their pre-probe status.

Current wire-8 ceilings are 2169 bytes/nine enum members/depth 5. Fixed schemas peaked at 1914 bytes; the approved four-claim/six-statement structural study peaked at 1971. These depend on frozen approved IDs, not arbitrary claim-ID lengths, and do not prove universal token fit or provider acceptance.

Calibration scope remains all J01-J13 plus the full R04 validator. Completed operational decoding is separate from grading acceptance. Old pending checkpoints are superseded by source-bound observations, never converted into gate passes.

Focused review required `src/rent_navigator/eval/judge.py`; accounting in `provider.py`, `eval/live_budget.py`, `eval/collection.py`; and serving `provider.py`, `context.py`, `agent.py`, `trace.py`, `models.py`, `model_policy.py`, all under `src/rent_navigator/`. Judging-configuration/blocking accounting findings had to land before the first paid judge call, other findings before PR #9 merged. This records prerequisites and sequence, not a new review approval.

Two older retained wire-5 failure envelopes remain `category=http_error`, `http_status=400`, `rejection_reason=unclassified`. They are not reclassified from HTTP status or the later wire-6 observation. No historical body/message is recovered.

The bounded `scripts/judge_smoke.py` exception permits its existing per-vote provider request ID and error-type string beside a validated diagnostic, never error messages/bodies, headers, keys, packets or raw exceptions. It does not extend normal metadata logging. General probe output retains no provider response; native diagnostic probe dumps are excluded.

Current projection discards reasoning content, signatures and redacted data before raw serialization, preserving final JSON and inclusive usage. Optional thinking-token observations are not added again to inclusive output or cost. Missing observations remain unknown.

## Historical collections and dry runs

### Run-1

The [manifest](october-release/run-1/collection/manifest.json) remains incomplete, evaluation-incomplete and nonreportable at source `3d9299b01e973a843976ad13dcae9b544e217db4`, judge hash `258529b186b909caf4a86e6b8eda05d495f887e2ffb778891816457db1afcb04`. The Q04 baseline judge exhausted its 8192-token output allowance on reasoning. This was a model-output stop, not an infrastructure-only failure. No headline is reported, rows repaired or run resumed.

Known recorded provider-call cost is **US$0.210706**, reproduced with exact decimal arithmetic:

| Repository path | Selection | Cost field | Sum, USD |
|---|---|---|---:|
| `october-release/run-1/collection/metadata.jsonl` | 12 rows: `record_kind="provider_call"` and `provider_operation="generation"` | `actual_cost_usd` | 0.090314 |
| `october-release/run-1/collection/judge-metadata.jsonl` | 2 rows, same predicate | `actual_cost_usd` | 0.120392 |
| Unique generation records combined | 14 | Same field, once per generation | **0.210706** |

Serving metadata has 32 rows: 12 generations, 12 `count_tokens` records and eight endpoints. Its generation total includes warmups. Count records sum to zero under free semantics. Endpoint costs also sum to US$0.090314, used only as a cross-check, not added.

Judge metadata has six rows: two generations, two counts and two endpoints. Generation costs are US$0.027306 and US$0.093086. Counts sum to zero; endpoints sum to US$0.120392 as a cross-check only. Every selected generation has complete usage and non-null actual cost, including the output-limit failure.

Row-level cross-checks are separate:

| File and field | Rows | Sum, USD |
|---|---:|---:|
| `results.jsonl` → `serving_cost_usd` | 2 | 0.017147 |
| `warmups.jsonl` → `serving_cost_usd` | 6 | 0.073167 |
| Combined serving | 8 | 0.090314 |
| `results.jsonl` → `judge_cost_usd` | 2 | 0.120392 |

Warmup judge costs are null because warmups were not judged. No missing generation usage is converted to zero. These row/endpoint views are not added to provider-call totals. Metadata hashes match the original manifest:

| File | SHA-256 |
|---|---|
| `metadata.jsonl` | `d7f5a183fbdd819a392433179f9b2090c59c0e76dfe02d8977fd35d80cb64714` |
| `judge-metadata.jsonl` | `494aacef654e997aeed2e2ca9dbc17b284d7c1b5d8b241af1b6ad2555c62902b` |
| `manifest.json` | `d084c5c2e27429c827bd99cfcd7737242c9a4b4fe391887abfc02163e0a960d6` |

This is known recorded spending from an incomplete attempt, not a complete aggregate headline, invoice or balance. Original null aggregate fields and failed status remain unchanged. The later capacity decision changed only judge output capacity and the absolute deadline; native diagnostic probes are not published here.

### Dry-run-2

The [manifest](october-release/dry-run-2/dry-run/manifest.json) remains failed/nonreportable at source `57fd0410899311d5a2f1d850f7f019130aa4fe67`. Only R01 production repeat 0 ran. A model-matched `tool_use` response was rejected because `usage.speed="standard"` was undeclared by the locked SDK. No judge call followed.

Historical actual cost remains null, usage incomplete and the full US$0.027 reservation retained. It is not written off as zero or retroactively settled. K3 accepts absent, null or exact `"standard"` speed through shared actor/judge accounting and replay. `"fast"` and every other value remain unpriced and stop further paid work. No request speed, price, dependency or judge-rubric change follows.

### Run-2

The [manifest](october-release/run-2/collection/manifest.json) remains complete, evaluation-complete and reportable at source `e8632d2a2e2d8a45e9cedbd2f360b58f88b21695`. Completion verification passed, but the comparison is unusable as a headline.

All 50 measured baseline tool answers and two of three baseline warmups ended `invalid_generated_output` after correct tool calls. Final answers used rule names where validation accepted evidence chunk IDs, and only production had received those IDs as a schema enum. Baseline activation remains pending. Original failures are not reconstructed as successful answers.

The [summary](october-release/run-2/collection/summary.json) retains US$5.805626 project-rate cost: serving US$1.939450, warmups US$0.073162 and judging US$3.793014. The cap was US$43.00 and forecast US$42.26040.

K4 applies the sorted allowed evidence-ID enum to both arms whenever nonempty. Only production requires `minItems=1`; baseline keeps effective `minItems=0`, without source passages or mandatory citations. Citation-schema policy 2 changes all six analysis hashes, preserving production payload behavior, extraction/protocol identity, prompts, tools, validation, redaction and accounting. The judge hash remains unchanged. Structural symmetry does not itself prove improved live outcomes.

Run-3 is the separately authorized post-correction collection, not an infrastructure-only retry. The record-only run-2 audit package is not completed current owner-audit evidence.

### Other dry runs

[Dry-run-1](october-release/dry-run-1/dry-run/manifest.json), [dry-run-3](october-release/dry-run-3/dry-run/manifest.json) and [dry-run-4](october-release/dry-run-4/dry-run/manifest.json) retain their passed integration status, actual sources and image identities. All are nonreportable. Dry-run-4 used source50190 for R01/N01/Q01 and recorded US$0.167994 serving-plus-judge cost, separate from run-3. A passed dry run does not activate the baseline.

## Historical funding and deferred protocol

These are historical checkpoint facts from PROJECT-SPEC.md and CONTRACTS.md, not a current balance, affordability calculation or execution grant.

| Checkpoint | Retained fact |
|---|---|
| WP6 | Confirmed provider prepayment US$30. Historical allocation: hosting US$14, development/CI/measurement US$21, demo US$9, contingency US$6. Settled incurred US$0.045685; then-future forecast US$20.313. |
| Later US$55 checkpoint | Development US$42, protected demo US$9, unallocated reserve US$4. Completed v4 diagnostic US$0.429937; settled incurred US$0.676401. Quality was not accepted, including Q04 coverage/own-citation misses, N04 deadline contradiction and R03 refusal. |
| Partial historical calibration | Two completed votes, third generation failure, ten unstarted. Partial settlement US$0.05268 took known incurred to US$0.729081; original US$0.082 remained unresolved. Consumed grant/evidence unchanged. |
| September 30 reforecast | Capacity-dependent future obligations exceeded then-confirmed allocation. Prospective planning, not posted funding or authority. |
| October 1 confirmation | Additional US$20 brought provider funding to US$75: development/CI/measurement US$62, protected demo US$9, unallocated reserve US$4. Hosting US$14 and contingency US$6 remained separate. |

The five holds and US$0.60968 total remain itemized in the root README and CONTRACTS.md §10. Outside-permit write-off did not settle charges or change events 43, 44 and 45. Charges remain unknown, never zero. Automatic replenishment remains disabled.

Detailed historical obligation inventories, ledger-prefix identities, admission-helper versions, funding-transfer arithmetic and capacity-specific permit formulas remain in CONTRACTS.md §§8-10 and Git history, not current available funding. Transfers from future to active are not counted twice; consumed slots and failed grants are not restored.

October 12 caps were smoke US$2.00, dry run US$1.00 and collection US$32.00, with US$29.15320 collection forecast. K2 planning used US$3.00/US$1.00/US$43.00 for measurement/dry run/collection, with forecasts US$2.99376/US$0.77652/US$42.26040. These are not actual spending or standing authority.

The original infrastructure-only retry rule required correction, a separately approved second cap and both attempts preserved; a third attempt was outside that original budget. Later separately authorized source corrections retain actual identities. Model-output stops have no headline; unchanged behavior is not rerun to select favorable results.

Original analysis attempts remain historical: a capacity guard stopped the first; a later answer confused cents with dollars despite correct tool output; corrected display still added an amount outside supplied strings. Mechanical checks did not establish prompt compliance. Later binding WP6 acceptance retained that limitation. None replaces release measurements.

The protected live-gate design is deferred, not deleted: single-use permit, environment/ledger path, complete diagnostics prerequisite, fixed gold/security order, critical citation/tool/false-pass/policy conditions, bootstrap 28/32 plus all seven safety cases, later `max(28, B−2)`, provenance/expiry/behavior checks, and narrow baseline-attachment/documentation-reuse rules. Its 39 attempts are gate evidence, not collection headlines.

No permit path was used this cycle. Synthetic funding tests, offline passes, source relationships or time allowances do not create funds, activate the baseline, settle unknown usage or authorize paid work. Private calibration, admission and ledger work remains stopped, with existing evidence preserved.

## Collection guards and dry-run semantics

Only HTTP admission rate, concurrency and daily quotas are bypassed for local collection. Freshness, redaction, full preflight, token/call limits, absolute deadlines, tool equality, citation and scope checks remain. Collection is serial.

Public HTTP retains its 16384-byte UTF-8 body limit, exact origin comparison, global 60-POST UTC-hour limit and concurrency two with rejection rather than queueing. These are not authentication or bot prevention. Both HTTP and collection reject a corpus older than 90 days; GET remains available. No public evaluation override exists.

The fixed collection uses three warmups per arm, five repetitions and 80 measured attempts per arm. Every scored answered attempt receives one judge vote. Warmups are neither judged nor scored. Five consecutive serving `provider_error`/`deadline_exceeded` failures end collection as `collection_untrustworthy`, resetting on another result; warmups count toward the counter but no scored denominator. Judge failures stop immediately; other errors retain existing behavior. Run-3 had zero such serving/judge execution failures in either denominator; incorrect answers stay counted.

A completed dry run uses production R01/N01/Q01 at repeat 0, five actor and three judge generations. Failed runs may stop earlier. It constructs no partial CollectionPlan and remains `dry_run=true`, `reportable=false`, `evaluation_complete=false`, never release evidence or baseline activation.

The ledger precedes the first operation; outputs are exclusive; evaluation inputs are read-only; partial records are preserved. Source, lock, Docker/image, provider date and environment stay bound to each manifest. Old images cannot represent changed configurations.

Usage is captured before output validation. Known billed usage remains counted after rejected output or over-reservation usage. Missing/unpriced usage keeps null actual cost and full reservation, stopping paid work for reconciliation. Endpoint summaries and call details are alternate spending views, not additive charges. UTC-day reconciliation uses the original reservation day; the in-memory counter resets on restart and is not a durable funding guarantee.

Judge assembly/count/reservation/generation/final-success validation share one 300-second deadline. Actor/extraction remain 45 seconds. Calls are retained immediately; deferred endpoints cannot publish success after the final check. Candidate endpoint duration excludes later validation. No extra vote, repair or automatic retry is introduced.

Full-content retention is limited to declared synthetic evaluation material; normal traces are metadata-only. Tool results require observed execution, not an unvalidated proposal. Extraction mismatches are not replaced with expected facts. Prepared context is not proof a request or generation occurred.

Ranked seeds, foundation and exact initial-context IDs remain distinct. Accepted replay independently reconstructs retrieval and cross-checks raw/row/endpoint provenance. Historical records keep original source/format rather than migrating into current evidence. Judge packets omit arm/unrelated identifiers, though citations can reveal the arm; own-citation support remains separate from factual evidence.

## CI and unexercised gates

Required checks are `checks`, `docker` and `offline-eval`; the [workflow](october-release/source-50190/.github/workflows/ci.yml) is retained. Baseline activation and retrieval floor remain pending in [activation.json](october-release/source-50190/eval/activation.json). Retrieval and answer quality are not gated.

Only exact-match tools, deterministic security fixtures and schema/hash/approval checks block evaluation regressions. The always-running offline summary rejects missing/skipped/cancelled/stale prerequisites, validates installed-package evidence, producer/digests, source/config/data identities, exact results, security report and activation. Gold approval alone is not acceptance.

Seven live S01-S05/S07/S08 cases were not executed because collection uses CASE_IDS only. They exist in the separate live-gate path under [src/rent_navigator/eval/](../src/rent_navigator/eval/). “Prompt-injection tests” means eight deterministic S01-S08 fixtures, not semantic resistance. The unchanged 28/32 and 7/7 live thresholds were unexercised. The live gate is not required and its environment reviewer rule is not restored.

The source workflow uses `github.event_name == 'pull_request' && vars.LIVE_EVAL_ENABLED == 'true'` for planning and `always() && vars.LIVE_EVAL_ENABLED == 'true'` for the gate. Environments, secrets and variables are not changed by this documentation. A historical non-required merged-main workflow after PR #9 was red; it is not counted as a passing release check.

## Deployment and public letter smoke

The service URL is [rent-navigator.onrender.com](https://rent-navigator.onrender.com). Free GET checks and the smoke bound source `50190c8173c8f36193be15b4f857df8b724094fa`, `v1.0.3-source`. The [post-reset receipt](october-release/public-letter-smoke-v1.0.3-source/post-reset-verification.json) records both routes returning HTTP 200 at `2026-10-22T21:27:32.180088Z`, health/source matching and the page footer source matching. The older e8632d2 observation is historical, not current deployment evidence.

Auto-Deploy off and the post-smoke `DEMO_DAILY_USD=0` setting are owner-confirmed. Free GET responses cannot independently expose these settings. The owner temporarily enabled 0.30 for the separately approved smoke and then returned zero with Save and deploy. No paid probe was used to verify zero. Render image architecture/digest remains unmeasured.

The ten byte-preserved smoke files are `health.json`, `extract-request.json`, `extract-response.json`, `ask-request.json`, `ask-response.json`, `summary.json`, `verification.json`, `metadata-owner.jsonl`, `accounting.json` and `post-reset-verification.json`.

The native [summary](october-release/public-letter-smoke-v1.0.3-source/summary.json) records one attempt from `2026-10-22T21:09:59.553956Z` to `2026-10-22T21:10:16.816097Z`, both POST responses HTTP 200, linked attempt/distinct traces and a failing ToolResult. Its `actual_cost_usd=null` and `narrative_review="pending"` are the at-execution state, not a claim of missing provider usage. They are preserved unchanged.

The later [verification](october-release/public-letter-smoke-v1.0.3-source/verification.json) records the specified checks passing: correct extraction, 60-day notice failure, 2.1%/$2042 ceiling against $2048 proposed rent and required sections 116/120 citations. The answer also adds a correct $6.00 difference beyond supplied display strings, the previously recorded instruction limitation. This is not strict prompt-compliance or general-quality acceptance. No retry or judge call occurred.

The [accounting](october-release/public-letter-smoke-v1.0.3-source/accounting.json) records complete usage and US$0.022208 actual cost at actor US$1/US$5 per million input/output tokens, against US$0.081 reserved and US$0.30/day test cap:

| Evidence basis | Input | Output | Actual USD |
|---|---:|---:|---:|
| Complete extraction endpoint | 616 | 34 | 0.000786 |
| Analysis generation index 2 | 3409 | 244 | 0.004629 |
| Analysis generation index 4 | 13458 | 667 | 0.016793 |
| Analysis endpoint, cross-check only | 16867 | 911 | 0.021422 |
| Extraction plus analysis once | 17483 | 945 | **0.022208** |

The underlying extraction generation row was not supplied and is not manufactured. Two analysis generation details cross-check one complete endpoint; the endpoint is not added again. The four owner-provided metadata-only rows were schema/configuration/pricing validated and repriced locally, without provider-console access.

HTTP flow duration was 17.262141 seconds including transport/local validation. Endpoint serving durations sum to 16048.872891813517 ms. Neither replaces collection latency. Later accounting/reset receipts supply later observations; original earlier null/pending fields are not rewritten.

The accounting receipt's known-cycle component is US$16.954546, not an invoice or current balance. Historical unknown K1 input charges and dry-run-2 charges remain unknown. Hosting is separate and unmeasured.

## Copy integrity and verification

All 124 copied evidence/configuration files are byte-identical to their approved original files or committed source blobs. The new owner subset inventory is separate. The five pre-existing scan files remain unchanged. No private probe dumps, messages/IDs from native probes, admission/ledger artifacts, credentials or real tenancy text were added.

The copied run-3 passed the actual `verify_real_collection` with `require_complete=True`, source50190, the committed lock hash and original manifest digest. It returned collection/evaluation completeness and reportability true, 166 started attempts and 160 judge evaluations. Historical incomplete/unusable records retain their original status; no check forces them into acceptance.

| Binding | SHA-256 |
|---|---|
| Run-1 manifest | `d084c5c2e27429c827bd99cfcd7737242c9a4b4fe391887abfc02163e0a960d6` |
| Run-2 manifest | `2623145fc829e373ef4ffaebb2be3809d1dcd97cc36ffd269c4ec1f4c740ad3e` |
| Run-3 manifest | `aed85f5bd87b95d96ddfc389607f5454056142a02018e05264b6c201b60bedc1` |
| Run-3 results | `dd894b0ac4a43a36adc90531cc5054a10aead6e3cb7da6371351e65e874356fc` |
| Original owner-package inventory, copied unchanged | `565cf097391623c364563f7765a5f06a49be6f8060e2d9512568086fda2d704a` |
| Original full owner-record inventory, retained separately | `b9e45daf8fe445d22ede07a902d2ae796c300fd2697bb75ed2eac26b92f2a9e2` |
| New publication-subset inventory | `9c263b6d98e692612fd9852cd9ec674ca55a134fa9b01aa9adaf8e1e50c893d2` |
| Current calibration summary | `71c66f307d557b11cc2310bf3f4224b5459ccc50db4940c4ade9f1d75fcc4e4a` |
| Existing scan inventory | `083571df2370c412ccd80145f47bb3646d619581620a089de3a6502eacfbf6f7` |
| Public smoke owner metadata | `6c93117eca44d9ab8fecf3298a2d044d56a0daa84d7bac5821fb0e5c41cc44a5` |
| Public smoke accounting | `4628a800261f975239b8a82b3a2a0058520723fcceb91fd5ccf478ab9861f3e2` |
| Public smoke post-reset receipt | `88e7bf34d9e94bb91bdd0cc3f541085c8088965d4378a6a003940820ad7fe96c` |

Immutable evidence retains its historical punctuation, path metadata, protocol roles and bounded smoke diagnostic identifiers. Newly authored documentation is separate from original evidence.
