# Supplemental judge calibration

These are exactly 13 synthetic, non-scored packets prepared before any new execution. They are development calibration and proposed review expectations, not additional approved gold, held-out observations or an accuracy result. Passing the offline tests establishes packet/schema binding and faithful projection of handwritten grades only. No live calibration has run.

Each `Jxx.json` separates `provider_packet` from provenance, derivation notes, expected grades and a representative handwritten audit output. Only `provider_packet` may enter the judge request. IDs J01–J13, titles, source identities and expected grades stay outside it. Each evidence handle is mechanically assigned in canonical-ID order; full passage IDs and texts remain present.

`manifest.json` retains full original packet strings, their hashes, original generation responses and original grades for Q04, N04, Q01 and the existing R04 smoke. It names and hashes the original private raw files. Those original files remain untouched. Original source is `defceb35a175b95162b9d13ef7d27e40c3164817`, diagnostic run `53daaeff-1d09-4dd3-9cae-7ed4cbe9bce8`. J01, J04 and J13 differ from those original packets only by new mechanical handles. The R04 tool result was supplied synthetic data, not an observed actor execution.

All table entries are proposed for independent adjudication before any separate execution authority. `S/S` means factual supported / citation supported; `C/U` means contradicted / unsupported; `S/U` means supported / unsupported; `U/U` means unsupported / unsupported. Unless specified, claims are met, statements S/S, false_pass false and policy violations empty. Full grades and audit bases are in each fixture.

| ID | Required-claim targets | Statement targets and sensitivity |
|---|---|---|
| J01 | c1 missing; c2 met; c3 missing; c4 met | s1 U/U, s2 U/U, s3 S/U, s4 S/S. Original Q04; c3 and s3 are the confirmed retained misses. Additional cutoff/protection labels remain explicitly subject to review. |
| J02 | c1–c4 met | All S/S after exact cutoff, protected/subsequent-tenancy qualification and own burden citation correction. |
| J03 | c1–c4 met; c3 coverage spans s2+s5 | Five statements, all S/S. The same qualification is paraphrased across two statements. |
| J04 | c1 met | Original N04; s4 C/U despite its true second sentence. Other statements S/S. |
| J05 | c1 met | Corrected s4 distinguishes a computable deadline from unknown actual compliance; all S/S. |
| J06 | c1 met via s5 | Isolated false deadline s4 C/U; true compliance-unknown s5 S/S. Original s1–s3 retained; old s5 becomes s6 and unrelated old s6 is omitted, preserving the six-statement limit and full original source/tool evidence. |
| J07 | c1 met | The corrected distinction alone is factual supported with citation not_applicable and the exact uncited basis. A derived uncited control, never a historical baseline claim. |
| J08 | c1–c2 met | Both year statements S/U. Correct canonical guideline evidence cannot repair each statement's irrelevant own Board-role citation. |
| J09 | c1 met | Fifth-day mail-service statement S/S with its own complete section 191(3) passage. |
| J10 | c1 contradicted | Matched dispatch-day statement C/U. No invented arithmetic or dates. |
| J11 | c1 met | Ordinary covered-unit N1 mapping excludes care-home units; S/S. |
| J12 | c1 contradicted | Matched universal “any covered rental unit” statement C/U. The frozen claim explicitly excludes care-home units; the source's N3 mapping is a counterexample, so this target is not assigned merely because a qualifier is omitted. |
| J13 | c1–c2 met | Original false-pass smoke: s1/s2 S/S, s3 C/U, false_pass true, no policy violations. |

J01's additional labels intentionally preserve AQ-02's cutoff-day and unqualified-protection concerns. Whether those omissions deserve unsupported or contradicted under the complete statements needs independent adjudication; the file records the current proposed labels rather than hiding the issue. The fixed c3 missing / s3 S/U targets are unaffected. These fixture targets do not close any historical answer-quality finding.

`sizes.json` records compact JSON Unicode-character and UTF-8 byte lengths under its exact prepared judge configuration. It includes full packet/schema/count-payload lengths, representative outputs and structural stress. Input stress fills the existing statement texts to 600 characters without adding evidence or statements. Output stress fills all basis lengths to their allowed maxima and includes every allowed per-basis reference/pointer. Stress is a legal-shape sizing exercise with filler text, not a meaningful grading example or universal upper bound.

Current maximum count-payload size excluding timeout is 44,274 UTF-8 bytes; input shape stress is 46,400 bytes. Representative audit output maximum is 3,172 bytes, while output shape stress reaches 33,188 bytes. These are **not token measurements**. They do not establish that the 3200-token allowance fits every valid response. Any later token count or generation requires separate authority and must retain rejection/truncation without evidence removal or retry.

Run `uv run --locked pytest tests/test_eval_judge_calibration.py` for offline structural checks. The tests prohibit external connections and provider-client construction; reconstruct packets through strict public JSON models; check corpus text, handles, own citations and blinding; verify fixture/original hashes; preserve original disagreements; and check representative/stress outputs against the packet-specific decoder. Fixtures are outside `eval/gold.jsonl` and cannot contribute to official metrics.
