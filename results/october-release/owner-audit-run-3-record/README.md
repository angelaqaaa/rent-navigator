# Run-3 owner audit record (2026-10-22)

Completed owner record for the run-3 audit package at `~/rn-results/owner-audit-run-3/` (source `50190c8173c8f36193be15b4f857df8b724094fa`, tag `v1.0.3-source`; manifest SHA256 `aed85f5bd87b95d96ddfc389607f5454056142a02018e05264b6c201b60bedc1`; results SHA256 `dd894b0ac4a43a36adc90531cc5054a10aead6e3cb7da6371351e65e874356fc`; judge hash `23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8`). No file in the source package was modified; its `SHA256SUMS` verified after this record was written. No judge grade was altered; this record sits beside the retained grades.

## Files

- `owner-record.json`: the package's `audit-template.json` with every record filled (`review_status`, `owner_factual_agreement`, `owner_citation_agreement`, `owner_policy_agreement`, `owner_overall_agreement`, `owner_brief_discrepancies`) and the top-level totals, `method` and `notes` filled.
- `owner-record.csv`: the same 53 rows in the package's `audit-template.csv` column order.
- `owner-decisions.csv`: the owner's decisions on every item any review pass marked unsure or disagree, plus the four required N04/R06 reads (`audit_id`, `dimension` overall or citation, `decision`, `note`).
- `root-first-pass.csv`, `root-cite-policy.csv`, `root-refute.csv`: the three assisted first-pass files (overall classification; citation support and policy; adversarial pass over every overall agree). Provenance only; the owner's decisions override them wherever both exist.
- `SHA256SUMS`: hashes of every other file in this directory.

## Totals

| Measure | Value |
|---|---|
| First-repetition outputs reviewed | 32 of 32 |
| Additional judge-flagged outputs reviewed | 21 of 21 |
| Overall agreement, first repetition (k/32) | 30/32 |
| Overall agreement, additional flagged | 20/21 |
| Citation agreement over the 53 outputs | 29 agree, 6 disagree, 18 not applicable (output cites nothing) |
| Policy agreement over the 53 outputs | 53 agree, 0 disagree |

Overall disagreements: `run-3-q05-baseline-repeat-0` and `run-3-q05-production-repeat-0` (judge success, owner incorrect: invented form titles; misstated s. 6.1 boundary) and `run-3-r04-baseline-repeat-3` (judge incorrect, owner supported; the judge graded the identical sentence supported in the other four repeats).

## Definitions

- `owner_overall_agreement`: agreement with the judge's task-success classification (success or incorrect). Citation support does not enter task success, so a citation disagreement alone does not change overall.
- `owner_factual_agreement`: follows the overall verdict; the overall checks are the factual grades and required-claim results (conclusion against the expected status, claim coverage, contradictions against gold facts, tool values and passages).
- `owner_citation_agreement`: agreement with the judge's per-statement `citation_support` grades; `not_applicable` when no statement in the output carries `citation_ids`.
- `owner_policy_agreement`: agreement with the judge's `policy_violations`.
- `k`: count of first-repetition outputs with overall agree.

## Method

Every output was read against the gold expected fields, required claims, the retained tool result, the cited and canonical passages and the judge rubric. An assisted first pass over all 53 outputs ran three read-only review passes on the package and classified each item as agree, disagree or unsure with a written reason. The owner decided every item any pass marked disagree or unsure (19 outputs, 26 decisions) and read the four N04/R06 first-repetition outputs in full; every value in this record is the owner's decision. No provider call was made.

Citation principle applied by the owner: a statement asserting the 90-day, 12-month or guideline-cap rule itself that cites only Legislation Act s. 89 (computation of time) or only the guideline percentage table is citation unsupported.

## Notes for the results documentation

- Judge inconsistency on identical text: R04 baseline s4 (same answer in all five repeats) was graded factual supported in repeats 0, 1, 2 and 4 and unsupported in repeat 3.
- Judge inconsistency on identical text: R04 baseline s3 `citation_support` was supported in repeat 0 and unsupported in repeat 3 on the same cited passages.
- Judge inconsistency on identical text: N02 production s3 `citation_support` was supported in repeats 0 and 4 and unsupported in repeats 1, 2 and 3 on the same cited passages.
- The targeted deterministic N04/R06 scan has not been run; its flagged answers need their own owner reads and are not counted here.
