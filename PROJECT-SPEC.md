**GO — Rent Navigator, conditional on the quality gates below.** The original plan was twelve focused sessions, **32.5 owner hours**, **2.5 hours slack** within 35 hours, an October11 release and October15 cancellation reference, with **US$50** operating spend excluding the builder subscription. These are historical planning estimates, not reasons to cut necessary quality work or automatically cancel. Record actual effort and revise schedule/funding forecasts explicitly. Materially necessary design, implementation, testing, independent review and repairs take priority over the earlier token, cost and time envelope; unrelated scope and unapproved spending remain excluded.

This is the decision brief. [CONTRACTS.md](CONTRACTS.md) is its required implementation annex, approved separately from the three-page brief limit. Both are binding; contradictions require advisor resolution before implementation. The superseded reference is not an implementation authority.

### 1. Target claims — verbatim

1. "Built and deployed an LLM agent that answers Ontario tenant rent-increase and notice questions over a snapshot-versioned corpus of the Residential Tenancies Act and official guidance: tool use with typed schemas (rent-increase legality and notice-deadline checkers), RAG with cited answers, guardrails (prompt-injection tests, PII redaction)."
2. "Designed the evaluation harness: N-case golden dataset of statutory scenarios with provable verdicts, deterministic + LLM-as-judge scoring (task success, retrieval MRR/NDCG, hallucination rate); evals run in CI and block regressions."
3. "Measured: X% task success vs Y% no-retrieval baseline; p50/p95 latency; cost per 100 runs from built-in tracing."
4. "Shipped: public repo, CI (lint/type/test/eval), Docker, live demo."

Use only measured replacements. Retrieval improvement is not required; claim no novelty.

### 2. Architecture and Decision Log

Python 3.12, uv, FastAPI/Uvicorn, Pydantic 2, Anthropic SDK, SQLite FTS5, PyYAML, BeautifulSoup, pypdf; ruff, strict mypy, pytest/httpx. WP1 locks exact versions and Docker digest. Actor: `claude-haiku-4-5-20251001`, temperature 0; judge: `claude-sonnet-5`, default sampling. One Docker service/worker on [Render's $7 tier](https://render.com/pricing), static page, default hostname, no external database. Provider assumptions and dated verification are in annex §1.

| Decision | Alternative | Independent justification |
|---|---|---|
| Explicit bounded agent loop | Framework | Few interfaces; real typed provider tool calls remain observable |
| FTS5 BM25, top five; fixed rule references for production Q&A | Embeddings/service; query rewriting | Preserve reproducible ranked seeds while supplying the existing rule foundation and query-retrieved supplements |
| Committed snapshots; release drift review | Live fetch/scheduled refresh | Reproducible rules with an explicit release drift check |
| Server citation provenance + offline support judge | Prompt-only citations/runtime judge | Reject forged IDs; measure actual support without another serving call |
| Exact offline + stochastic live CI gates | Single gate | Arithmetic and generated explanations fail differently |
| Metadata tracing from WP1 | Later observability | Measurements need complete attempts, including failures |
| Same-tool baseline; blind judge and audit | Tool-free baseline/multiple judges | Compare the whole grounded system with the same tools/model without external context; expose judge disagreement without claiming a causal ranking effect |
| N=16; twelve bounded packages | N=20; ten larger packages | Preserve six retrieval questions while splitting risky seams |
| Paid single-service deployment | Sleeping free tier | Reliable recruiting demo with minimal operations |
| Actor input preflight/reservation 20k/21k; judge 24k/25k; output 1200/800 | Trim evidence; add per-case budgets | The 189-input foundation sizing study peaked at 14,749 actor and 20,351 judge stress tokens; preserve full evidence with uniform margins, complete preflight rejection and the confirmed funding allocation in §8 |
| Server-formatted CAD context for rent explanations | Leave cents conversion to generated prose | A real response confused cents with dollars and the rent ceiling with the increase; preserve exact amounts and meanings |
| Pattern redaction of free text; additive test injection | Rewrite typed facts; replace corpus chunks | Preserve confirmed money and evidence identity while testing payload privacy and hostile context |
| Owner-approved gold hash; shared-operation measurement | Implicit gold approval; a separate measurement service | Freeze the independent oracle before scoring and reuse the actual serving behavior |
| One authorized live batch; verified evidence reuse for unchanged commits | Pay on every push or trust stale checks | Bound API spending while proving the tested behavior matches each required CI summary |

These freshly justify adopted reference patterns; its domain, thresholds and schedule are discarded. Review amendments also freeze `max(28, B−2)`: the former N=16 formula reduced to a constant 28 and did not compare meaningfully with B.

**Official evidence checked September 19, 2026:** [RTA](https://www.ontario.ca/laws/statute/06r17), especially ss. 6.1, 116, 119, 120, 191; [Ontario guideline](https://www.ontario.ca/page/residential-rent-increases), **2026 2.1%, 2027 1.9%**; [Legislation Act s. 89](https://www.ontario.ca/laws/statute/06l21); [LTB guide](https://tribunalsontario.ca/documents/ltb/Brochures/Guide%20to%20RTA%20%28English%29.html); [N1 instructions](https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase%20%26%20Instructions/N1%20instructions_final_Nov30_2015.pdf), “Notice of Rent Increase”; [N2 instructions](https://tribunalsontario.ca/documents/ltb/Notices%20of%20Rent%20Increase%20%26%20Instructions/N2%20instructions_final_Nov30_2015.pdf), “Notice of Rent Increase (Unit Partially Exempt).” Seasonal AC rules are an exception to ordinary timing and are excluded. WP2 records actual fetch dates and consolidation periods; pre-release operative drift blocks release.

Two pure tools: `notice_deadline_check` and `rent_increase_check`. Both require the same scope envelope. Generic Q&A is informational; case calculations require confirmed facts. Flow: anonymized letter → extraction → confirmation → retrieval → model-emitted tool call → validation/execution → cited answer. No history. Public schemas, arithmetic, refusal/error states and call limits are frozen in annex §§2–5.

Production Q&A assembles a fixed foundation from all existing validated rules' evidence plus the unchanged top-five query seeds, deduplicated in corpus order. Rule references do not imply tool execution. Fact modes retain seed order and add only actual executed-rule evidence later; baseline and extraction receive no reference passages. Baseline facts may still cite actual executed rules without receiving their passage text. Keep ordered query seeds, foundation and initial context separately in required metadata and raw provenance; accepted replay independently recomputes the original seed ranking and distinguishes prepared context from an observed analysis request. Annex §§1, 5 and 7 freeze this policy and its failure lifecycle.

### 3. Work packages

Sequential PRs, one owner review each. Record actual builder effort and resource use; quality, correctness and complete evidence take priority over earlier implementation time estimates. The former three-hour implementation stop and eight-minute repair target are withdrawn; the original owner-time and release-date estimates are historical and must be reforecast rather than used to omit necessary work. Paths below use `src/rent_navigator/` unless prefixed otherwise. Each package includes its tests; prior gates stay green. No package includes later seams.

| WP / done | Files and seam | Checkable acceptance → state |
|---|---|---|
| 1 / Sept 21 | Packaging, Dockerfile, base CI, `models.py`, `trace.py`, health stub | Schema fixtures, synthetic timing/usage/cost, lint/type/test/container pass → skeleton |
| 2 / Sept 23 | `corpus/`, `index.py`, rules, NOTICE | Six hashed snapshots, resolving rule IDs, identical offline rebuild → evidence/index |
| 3 / Sept 25 | `notice.py` | Scope, late-service and 89/90/91-day hand/mail tests → notice tool |
| 4 / Sept 26 | `rent.py` | Anniversary/leap, money, form, exemptions, unknowns, double failures → rent tool |
| 5 / Sept 28 | `provider.py`, `extract.py` | Fake/live extraction, token/usage/deadline adapter; no loop → provider seam |
| 6 / Sept 30 | `agent.py`, CLI | Genuine tool-use round trip; wrong/missing calls and forged IDs fail → cited slice |
| 7 / Oct 1 | `guards.py`, security fixtures | Eight deterministic attacks, redaction before all provider payloads → guarded pipeline |
| 8 / Oct 3 | `eval/` runner/metrics/gold, offline CI | Approved 16 cases; metric fixtures; both arms and collection modes work → reusable harness |
| 9 / Oct 4 | Judge, live CI, baseline manifest | Full live gate passes; deliberate failures cannot pass summaries → enforced gates |
| 10 / Oct 6 | `app.py` HTTP adapter | Schemas, errors, limits and linked extraction/analysis traces → tested API |
| 11 / Oct 7 | Static page, Render configuration | Confirmation and public letter smoke; required gates green → deployed demo |
| 12 / Oct 10 | `results/`, README only | Existing runner collects complete evidence; audit packet ready → release candidate |

October11 was the original human-audit/release target. Release requires complete accepted evidence, with an updated schedule when necessary. WP12 adds no runner, prompt or service code; defects reopen the owning package for repair and revised effort estimates.

### 4. Builder guardrails

Pin on README, page and every answer:

> Independent project; not affiliated with the Government of Ontario or the Landlord and Tenant Board. General legal information, not legal advice. Rules as of {snapshot_date}; results depend on confirmed facts. For advice, consult a licensed Ontario lawyer or paralegal.

No recommendations to pay, withhold, file or challenge. Redact email/phone/postal patterns in free text before any provider request; preserve typed facts under annex §5 and warn users to remove names/addresses manually. Persist metadata only; synthetic evaluation artifacts are allowed.

Builder may choose private helpers and test organization. Do not change contracts, dependencies, sources, models, scope, labels, thresholds, disclaimer or budgets. Read both documents and prior PR evidence each session. Escalate unavailable dependencies, legal contradictions, material ambiguity, failed acceptance or material resource/scope changes. Conventional lowercase commits/PR titles; no assistant attribution or co-author lines.

### 5. Definition of shipped

Shipped means a public `rent-navigator` repo, required lint/type/test/offline/live checks green, Docker build, live demo, frozen measured evidence. README: problem, architecture diagram, actual results/method, limitations, setup, attribution and URL. MIT covers code; retain [Crown attribution](https://www.ontario.ca/page/copyright-information) separately.

Demo: hypothetical ordinary controlled tenancy, N1, $2,000 → $2,048, September 1, 2026 effective date, July 3 hand service, previous increase September 1, 2025. Display both guideline and notice failures, citing ss. 120 and 116.

This is engineering shipment. Career integration (inventory evidence card → resume → application claims) follows shipping, costs another 1–2 hours and is not silently funded by repair slack. This Decision Log supplies the decision record; no duplicate DECISIONS.md. Do not claim completion before inventory integration.

### 6. Measurement and evaluation

**N=16:** six rent, four notice, six Q&A; two letters within rent. Angela approves cases, expected results, evidence and relevance labels by September 27, before inspecting retrieval results. Additional boundary tests and eight security fixtures are never counted in N. Disclose development-set reuse.

Exact tool/extraction scoring plus one blind structured judge: factual support uses identical approved evidence across arms; citation support uses the answer's actual cited passages. No serving judge. Offline MRR@5/NDCG@5 uses the six Q&A cases; frozen scores cannot decrease. Live CI: 32 gold attempts plus seven live attacks; initial B ≥28, subsequent successes ≥`max(28, B−2)`; citation/security failures and false passes independently block. Always-running summaries prevent skipped checks from passing. Full schemas and bootstrap rules: annex §§6–8.

MRR/NDCG measure the original ranked query seeds only; fixed references do not improve those scores. The frozen offline foundation map records 36/37 diagnostic required claims, with the D01 alternative explanation kept post-hoc, D12's ordinary section 6.1 premise explicit and D15's representation limitation unresolved. These are material-availability findings, not generated-answer quality. The complete 189-input foundation sizing study supports the approved capacity policy, but its original working-tree identity must be retained and unchanged full request bytes proved before reusing its counts for newer source. Independent implementation review and all 22 separately authorized fixed diagnostics still precede the unchanged full live gate; the earlier 84-request count study does not cover this context.

Collection at clean commit S: same Docker image/machine, three warm-ups/arm, five full runs/arm, serial seed-42 order, alternating arms, no cache. Baseline removes all external reference passages and citation enforcement, retaining tools/model/budgets and the executed-rule citation exception. The comparison covers the whole grounded system, not the causal contribution of query ranking alone. Letter mismatches fail without correction. Only admission quotas are disabled.

Report **X/Y = successes/80 attempts**, counts, per-run ranges, tool/Q&A breakdown; hallucinated answered outputs/answered outputs plus errors/refusals; nearest-rank **p50/p95** for all 80 production attempts, measured at the warm-local serving-operation boundary (excluding HTTP/browser transport); **cost/100 = 100 × serving token cost/attempts**, including extraction. Judge/hosting costs are separate. Trace all failures; missing usage makes the cost claim incomplete. Audit first-run answers for all cases/both arms plus all flagged hallucinations; publish agreement/discrepancies. Exact protocol: annex §9.

### 7. Scope and quality priority

The approved N=16 dataset and evaluation denominators remain fixed. Earlier deadline-driven styling/sample-picker cuts are not automatic instructions; retain materially quality-improving work and discuss genuinely optional scope separately. Do not shrink the dataset or weaken acceptance to fit an obsolete estimate.

**Preserve:** both tools, confirmation, citations, guardrails, tracing, harness/CI, measurements, Docker, live demo and necessary independent review/repairs. Slipped historical milestones trigger a candid progress and resource reforecast, not automatic scope cuts, cancellation or an incomplete acceptance claim. Quality priority does not authorize unrelated features, favorable-run selection, automatic purchases or unbounded paid execution.

### 8. Owner-hours and cash

Original owner-time estimate: reviews **12h**; kickoffs **3h**; gold/labels **5h**; audit **2h**; accounts/deploy **2h**; measurement/release **3.5h**; design/review/amendments **5h** = **32.5h**. Weekly totals September 20–26 / September 27–October 3 / October 4–10 / October 11–14: **10 / 9.5 / 9.5 / 3.5h** against **10 / 10 / 10 / 5h**. Slack: **0 / 0.5 / 0.5 / 1.5h**. Replace estimates with actual time; reviews/rework are not free. Builder time is separately **24–36h**.

Original cash plan: US$50, hosting **14**, API development/CI/measurement **21**, public demo **9**, contingency **6**. The owner has confirmed another **$20** provider prepayment, bringing cumulative confirmed prepayment from **$30 to $50**; automatic replenishment remains disabled. This is owner-confirmed funding, not a queried account balance or authority for further calls. The selected allocations remain development/CI/measurement **$36** and protected demo **$9**; the additional **$5** stays unallocated provider reserve. Including hosting 14 and contingency 6, the current cash allocation is **$70**. Demo admission remains **$0.30/day for 30 days**; restart-independent protection comes from prepaid funding, not the in-memory counter.

At unchanged token prices, actor reservation A is **$0.027** and judge reservation J is **$0.058**. The selected future work is all 22 fixed diagnostics (26A+7J=$1.108), release and warm-ups (298A+160J=$17.326), three full gates (231A+117J=$13.023) and seven generic development allowances (21A+7J=$0.973): **576A+291J=$32.430**. Add settled incurred **$0.232356** for **$32.662356** development exposure; with demo 9, the plan requires **$41.662356**. Confirmed provider funding 50 leaves **$8.337644** beyond that forecast: **$3.337644** within the unchanged 36 development allocation and **$5** unallocated provider reserve. Reservations are forecasts, not spending or absolute billing caps.

The actual 21-event ledger records the owner funding confirmation and explicit obligation replacement: **13 generic development allowances become 22 fixed diagnostics plus seven generic allowances**; **three future live batches** remain. Incurred **$0.232356**, unknown hold 0 and active reservation 0 are unchanged; the original 19 event bytes and consumed grants remain preserved. The existing **wp9_bootstrap_savings_repair** slot is reforecast to **$4.341**; the failed original bootstrap and correction remain consumed, B=null. Funding confirmation and the obligation update are complete, but independent repair/runner review and completion/settlement of all 22 diagnostics still precede a separately issued LivePermit v2. It preserves two future gates and seven generic allowances, with **$26.981** still required; diagnostic actual costs enter the latest incurred rather than remaining in the future forecast. Later regression permits preserve the frozen baseline and actual remaining obligations. Offline integration and synthetic permit tests confer no paid authority.

The approved private judge wire, actor 1–6 sequential statements of 1–600 characters and 1200 output tokens, foundation policy, scoring, model choices and token prices remain unchanged. Shared instructions prioritize actual checks and necessary qualifications without unrelated recitation; no 160-character soft target is restored. Historical failures, original costs and identities remain in annex §10.

### 9. Integrity

Tag clean source `v1.0-source`; evidence commit records its SHA and configuration/corpus/gold/pricing hashes. Deploy that source. Any shipped behavior, dependency, prompt, configuration or corpus change requires the full protocol again. Evidence-only documentation is exempt. Preserve failed/incomplete runs; never select favorable runs, lower baselines or invent numbers.

### 10. Interview appendix

| Probe | Answer location |
|---|---|
| Why lexical retrieval? | Decision Log; annex §1; index |
| What do citations guarantee? | Annex §§4, 7; support scores |
| How do dates/money work? | Annex §3; boundary tests |
| How are exemptions treated? | Annex §§2–3; gold unknown cases |
| Is the baseline fair? | Annex §§7, 9; run configuration |
| Is the judge trustworthy? | Annex §7; human audit |
| How do noisy gates block changes? | Annex §8; CI manifest |
| What stops injection/PII leakage? | Annex §§4–6; security fixtures |
| How were latency/cost measured? | Annex §§5, 9–10; traces |
| What breaks with scale/new law? | Annex §§1, 4; README limits |

### 11. Out of scope

Disputed RTA coverage, social/care/co-op/assigned tenancies, AGIs, agreements/additional services, seasonal AC charges, retrospective paid increases, disputed base rent, mid-period increases, other service methods, eviction/filing deadlines, form selection/completion, uploads/OCR, history, accounts, embeddings, automatic refresh, analytics and legal advice.
