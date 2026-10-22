# Run-3 owner audit package

Start with [review.html](review.html), the self-contained escaped reading view. Each retained answer appears beside its full original judge object. Each statement is paired with its factual and citation_support grades; required-claim results, false_pass and policy_violations are visible. No source text is executed as HTML.

Use [audit-template.csv](audit-template.csv) or [audit-template.json](audit-template.json). All human decisions, reviewed counts, k and k/32 are blank/null, not zero or agreement. k/32 covers only the 32 first-repeat outputs. The 21 additional flagged reads have a separate count. Return the completed owner record before the next stage.

Source: 50190c8173c8f36193be15b4f857df8b724094fa / v1.0.3-source. Manifest SHA256: aed85f5bd87b95d96ddfc389607f5454056142a02018e05264b6c201b60bedc1. Results SHA256: dd894b0ac4a43a36adc90531cc5054a10aead6e3cb7da6371351e65e874356fc. Completion verification was recorded PASS before preparation; this package is not a human audit or a headline comparison.

## Selection counts

~~~json
{
  "first32": 32,
  "hallucinated": 27,
  "hallucinated_by_arm": {
    "production": 11,
    "baseline": 16
  },
  "overlap": 6,
  "union": 53,
  "additional_flagged": 21,
  "required_first_repeat_N04_R06": 4,
  "historical_records_in_run3_audit": 0
}
~~~

Selection uses repeat 0 for every case and both arms, excluding warmups, plus every factual flag from existing classify with actual gold claim IDs across five repeats. Baseline missing citations alone do not select a flag. CASE_IDS order, production then baseline, repeat ascending is used throughout. The [selection ledger](selection.json) records complete membership, attempt IDs, original JSONL line numbers and source-line hashes. [selected-results.jsonl](selected-results.jsonl) copies those original lines exactly.

## Required sampled N04 and R06

Read all four first-repeat entries regardless of judge success. The targeted deterministic scan has not been performed; its later flags will require their own reads.

| Output | Case | Arm | Repeat | Source line | First32 | Flagged |
|---|---|---|---:|---:|---|---|
| [Open run-3-r06-production-repeat-0](outputs.md#run-3-r06-production-repeat-0) | R06 | production | 0 | 24 | true | false |
| [Open run-3-r06-baseline-repeat-0](outputs.md#run-3-r06-baseline-repeat-0) | R06 | baseline | 0 | 23 | true | false |
| [Open run-3-n04-production-repeat-0](outputs.md#run-3-n04-production-repeat-0) | N04 | production | 0 | 32 | true | true |
| [Open run-3-n04-baseline-repeat-0](outputs.md#run-3-n04-baseline-repeat-0) | N04 | baseline | 0 | 31 | true | false |

## First32

| Output | Case | Arm | Repeat | Source line | First32 | Flagged |
|---|---|---|---:|---:|---|---|
| [Open run-3-r01-production-repeat-0](outputs.md#run-3-r01-production-repeat-0) | R01 | production | 0 | 12 | true | false |
| [Open run-3-r01-baseline-repeat-0](outputs.md#run-3-r01-baseline-repeat-0) | R01 | baseline | 0 | 11 | true | false |
| [Open run-3-r02-production-repeat-0](outputs.md#run-3-r02-production-repeat-0) | R02 | production | 0 | 28 | true | false |
| [Open run-3-r02-baseline-repeat-0](outputs.md#run-3-r02-baseline-repeat-0) | R02 | baseline | 0 | 27 | true | false |
| [Open run-3-r03-production-repeat-0](outputs.md#run-3-r03-production-repeat-0) | R03 | production | 0 | 13 | true | false |
| [Open run-3-r03-baseline-repeat-0](outputs.md#run-3-r03-baseline-repeat-0) | R03 | baseline | 0 | 14 | true | false |
| [Open run-3-r04-production-repeat-0](outputs.md#run-3-r04-production-repeat-0) | R04 | production | 0 | 21 | true | false |
| [Open run-3-r04-baseline-repeat-0](outputs.md#run-3-r04-baseline-repeat-0) | R04 | baseline | 0 | 22 | true | false |
| [Open run-3-r05-production-repeat-0](outputs.md#run-3-r05-production-repeat-0) | R05 | production | 0 | 9 | true | false |
| [Open run-3-r05-baseline-repeat-0](outputs.md#run-3-r05-baseline-repeat-0) | R05 | baseline | 0 | 10 | true | false |
| [Open run-3-r06-production-repeat-0](outputs.md#run-3-r06-production-repeat-0) | R06 | production | 0 | 24 | true | false |
| [Open run-3-r06-baseline-repeat-0](outputs.md#run-3-r06-baseline-repeat-0) | R06 | baseline | 0 | 23 | true | false |
| [Open run-3-n01-production-repeat-0](outputs.md#run-3-n01-production-repeat-0) | N01 | production | 0 | 29 | true | false |
| [Open run-3-n01-baseline-repeat-0](outputs.md#run-3-n01-baseline-repeat-0) | N01 | baseline | 0 | 30 | true | false |
| [Open run-3-n02-production-repeat-0](outputs.md#run-3-n02-production-repeat-0) | N02 | production | 0 | 17 | true | true |
| [Open run-3-n02-baseline-repeat-0](outputs.md#run-3-n02-baseline-repeat-0) | N02 | baseline | 0 | 18 | true | false |
| [Open run-3-n03-production-repeat-0](outputs.md#run-3-n03-production-repeat-0) | N03 | production | 0 | 20 | true | true |
| [Open run-3-n03-baseline-repeat-0](outputs.md#run-3-n03-baseline-repeat-0) | N03 | baseline | 0 | 19 | true | false |
| [Open run-3-n04-production-repeat-0](outputs.md#run-3-n04-production-repeat-0) | N04 | production | 0 | 32 | true | true |
| [Open run-3-n04-baseline-repeat-0](outputs.md#run-3-n04-baseline-repeat-0) | N04 | baseline | 0 | 31 | true | false |
| [Open run-3-q01-production-repeat-0](outputs.md#run-3-q01-production-repeat-0) | Q01 | production | 0 | 25 | true | false |
| [Open run-3-q01-baseline-repeat-0](outputs.md#run-3-q01-baseline-repeat-0) | Q01 | baseline | 0 | 26 | true | true |
| [Open run-3-q02-production-repeat-0](outputs.md#run-3-q02-production-repeat-0) | Q02 | production | 0 | 5 | true | false |
| [Open run-3-q02-baseline-repeat-0](outputs.md#run-3-q02-baseline-repeat-0) | Q02 | baseline | 0 | 6 | true | true |
| [Open run-3-q03-production-repeat-0](outputs.md#run-3-q03-production-repeat-0) | Q03 | production | 0 | 8 | true | false |
| [Open run-3-q03-baseline-repeat-0](outputs.md#run-3-q03-baseline-repeat-0) | Q03 | baseline | 0 | 7 | true | false |
| [Open run-3-q04-production-repeat-0](outputs.md#run-3-q04-production-repeat-0) | Q04 | production | 0 | 1 | true | false |
| [Open run-3-q04-baseline-repeat-0](outputs.md#run-3-q04-baseline-repeat-0) | Q04 | baseline | 0 | 2 | true | true |
| [Open run-3-q05-production-repeat-0](outputs.md#run-3-q05-production-repeat-0) | Q05 | production | 0 | 16 | true | false |
| [Open run-3-q05-baseline-repeat-0](outputs.md#run-3-q05-baseline-repeat-0) | Q05 | baseline | 0 | 15 | true | false |
| [Open run-3-q06-production-repeat-0](outputs.md#run-3-q06-production-repeat-0) | Q06 | production | 0 | 4 | true | false |
| [Open run-3-q06-baseline-repeat-0](outputs.md#run-3-q06-baseline-repeat-0) | Q06 | baseline | 0 | 3 | true | false |

## All 27 factual-hallucination flags

| Output | Case | Arm | Repeat | Source line | First32 | Flagged |
|---|---|---|---:|---:|---|---|
| [Open run-3-r04-baseline-repeat-3](outputs.md#run-3-r04-baseline-repeat-3) | R04 | baseline | 3 | 105 | false | true |
| [Open run-3-n02-production-repeat-0](outputs.md#run-3-n02-production-repeat-0) | N02 | production | 0 | 17 | true | true |
| [Open run-3-n02-production-repeat-1](outputs.md#run-3-n02-production-repeat-1) | N02 | production | 1 | 35 | false | true |
| [Open run-3-n02-production-repeat-2](outputs.md#run-3-n02-production-repeat-2) | N02 | production | 2 | 85 | false | true |
| [Open run-3-n02-production-repeat-3](outputs.md#run-3-n02-production-repeat-3) | N02 | production | 3 | 122 | false | true |
| [Open run-3-n02-production-repeat-4](outputs.md#run-3-n02-production-repeat-4) | N02 | production | 4 | 156 | false | true |
| [Open run-3-n03-production-repeat-0](outputs.md#run-3-n03-production-repeat-0) | N03 | production | 0 | 20 | true | true |
| [Open run-3-n04-production-repeat-0](outputs.md#run-3-n04-production-repeat-0) | N04 | production | 0 | 32 | true | true |
| [Open run-3-n04-production-repeat-1](outputs.md#run-3-n04-production-repeat-1) | N04 | production | 1 | 54 | false | true |
| [Open run-3-n04-production-repeat-2](outputs.md#run-3-n04-production-repeat-2) | N04 | production | 2 | 89 | false | true |
| [Open run-3-n04-production-repeat-3](outputs.md#run-3-n04-production-repeat-3) | N04 | production | 3 | 127 | false | true |
| [Open run-3-n04-production-repeat-4](outputs.md#run-3-n04-production-repeat-4) | N04 | production | 4 | 132 | false | true |
| [Open run-3-q01-baseline-repeat-0](outputs.md#run-3-q01-baseline-repeat-0) | Q01 | baseline | 0 | 26 | true | true |
| [Open run-3-q01-baseline-repeat-1](outputs.md#run-3-q01-baseline-repeat-1) | Q01 | baseline | 1 | 44 | false | true |
| [Open run-3-q01-baseline-repeat-2](outputs.md#run-3-q01-baseline-repeat-2) | Q01 | baseline | 2 | 94 | false | true |
| [Open run-3-q01-baseline-repeat-3](outputs.md#run-3-q01-baseline-repeat-3) | Q01 | baseline | 3 | 117 | false | true |
| [Open run-3-q01-baseline-repeat-4](outputs.md#run-3-q01-baseline-repeat-4) | Q01 | baseline | 4 | 151 | false | true |
| [Open run-3-q02-baseline-repeat-0](outputs.md#run-3-q02-baseline-repeat-0) | Q02 | baseline | 0 | 6 | true | true |
| [Open run-3-q02-baseline-repeat-1](outputs.md#run-3-q02-baseline-repeat-1) | Q02 | baseline | 1 | 33 | false | true |
| [Open run-3-q02-baseline-repeat-2](outputs.md#run-3-q02-baseline-repeat-2) | Q02 | baseline | 2 | 87 | false | true |
| [Open run-3-q02-baseline-repeat-3](outputs.md#run-3-q02-baseline-repeat-3) | Q02 | baseline | 3 | 113 | false | true |
| [Open run-3-q02-baseline-repeat-4](outputs.md#run-3-q02-baseline-repeat-4) | Q02 | baseline | 4 | 147 | false | true |
| [Open run-3-q04-baseline-repeat-0](outputs.md#run-3-q04-baseline-repeat-0) | Q04 | baseline | 0 | 2 | true | true |
| [Open run-3-q04-baseline-repeat-1](outputs.md#run-3-q04-baseline-repeat-1) | Q04 | baseline | 1 | 64 | false | true |
| [Open run-3-q04-baseline-repeat-2](outputs.md#run-3-q04-baseline-repeat-2) | Q04 | baseline | 2 | 67 | false | true |
| [Open run-3-q04-baseline-repeat-3](outputs.md#run-3-q04-baseline-repeat-3) | Q04 | baseline | 3 | 101 | false | true |
| [Open run-3-q04-baseline-repeat-4](outputs.md#run-3-q04-baseline-repeat-4) | Q04 | baseline | 4 | 159 | false | true |

## Read without replacing retained judgments

Approved synthetic gold requests, expected fields and required claims are labelled separately from actual extraction, arguments and tool results. Canonical evidence and actual own citations are separately listed, with their complete judge evidence union and exact corpus passages available. Original grades are copied without correction. Record factual, citation, policy and overall agreement plus brief discrepancies in the template. The Markdown fallback is [outputs.md](outputs.md); exact gold and passage copies are available in both Markdown and JSONL.

## Historical transparency, excluded from run-3 k/32

- [Run-1 incomplete collection](/Users/Angelaqaaa/rn-results/run-1/collection): [manifest.json](/Users/Angelaqaaa/rn-results/run-1/collection/manifest.json), [results.jsonl](/Users/Angelaqaaa/rn-results/run-1/collection/results.jsonl), [warmups.jsonl](/Users/Angelaqaaa/rn-results/run-1/collection/warmups.jsonl). These originals are not current run-3 audit records.
- [Run-2 complete collection with unusable headline comparison](/Users/Angelaqaaa/rn-results/run-2/collection): [manifest.json](/Users/Angelaqaaa/rn-results/run-2/collection/manifest.json), [results.jsonl](/Users/Angelaqaaa/rn-results/run-2/collection/results.jsonl), [warmups.jsonl](/Users/Angelaqaaa/rn-results/run-2/collection/warmups.jsonl). These originals are not current run-3 audit records.
- [Run-1 historical measured candidate, original line 1](/Users/Angelaqaaa/rn-results/run-1/collection/results.jsonl:1) and [line 2](/Users/Angelaqaaa/rn-results/run-1/collection/results.jsonl:2): retained original judge states, not counted in run-3.
- [Run-2 record-only audit index](/Users/Angelaqaaa/rn-results/owner-audit-run-2/index.md): prior preparation only, not completed human-audit evidence. No outputs, membership or decisions from that package are copied into run-3.
- [Failed dry-run-2 original retained attempt](/Users/Angelaqaaa/rn-results/dry-run-2/dry-run/attempts.jsonl:1), [manifest.json](/Users/Angelaqaaa/rn-results/dry-run-2/dry-run/manifest.json) and [metadata.jsonl](/Users/Angelaqaaa/rn-results/dry-run-2/dry-run/metadata.jsonl): historical context only.

## Calibration references and limits

The calibration gate remains FAILED under conditional owner acceptance of the known J04/J06 miss. The historical K2 measurement recorded 123/132 fields and 8/13 exact proposed expectations, only J04/J06 fail-to-pass flips, and the R04 full-validator pass. It was not a passed calibration gate. J04/J06/J10/J13/R04 informed the rubric; agreement on them is in-sample. Only the named J04/J06 s4 factual/citation fields were owner-reviewed and upheld; other fixture labels remain proposed and not independently reviewed.

The unchanged J01-J13 fixtures are optional references. Compare expected_judgment with provider_packet, derivation, adjudication_notes and fact_authority_derivation. No additional review/waiver question is being requested and no fixture was edited.

- [J01](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J01.json)
- [J02](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J02.json)
- [J03](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J03.json)
- [J04](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J04.json)
- [J05](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J05.json)
- [J06](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J06.json)
- [J07](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J07.json)
- [J08](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J08.json)
- [J09](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J09.json)
- [J10](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J10.json)
- [J11](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J11.json)
- [J12](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J12.json)
- [J13](/Users/Angelaqaaa/Desktop/Textbook/rent-navigator/tests/fixtures/judge_calibration_v2/J13.json)

## Provenance and file inventory

The judge hash is 23cd495fad0dcc45be0f63018a6f921b152a76fe5a28fc0b2fc4e8d658f38ef8. [provenance.json](provenance.json) records source and historical hashes. The raw provider files remain in their immutable source directories; no raw provider messages, request IDs, reasoning/signature data, headers or credentials are copied into this package. Original selected answer records and grades are retained losslessly, including original punctuation. Newly written instructions use no em dashes.

Artifacts: [review.html](review.html), [outputs.md](outputs.md), [selection.json](selection.json), [selected-results.jsonl](selected-results.jsonl), [gold-premises.md](gold-premises.md), [gold-premises.jsonl](gold-premises.jsonl), [corpus-passages.md](corpus-passages.md), [corpus-passages.jsonl](corpus-passages.jsonl), [audit-template.csv](audit-template.csv), [audit-template.json](audit-template.json), [provenance.json](provenance.json) and [SHA256SUMS](SHA256SUMS). SHA256SUMS covers every other package file; the final report supplies its own hash separately.

Preparation stops here for the owner audit reply. No targeted scan, results documentation, public-letter-smoke request or paid call has been performed.
