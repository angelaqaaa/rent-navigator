# Supplemental judge calibration v2

These 13 derived development fixtures are synthetic, non-scored review controls. They do not add official gold cases or establish semantic accuracy. Current packets use packet policy 3 and audit wire policy 5. Handwritten current audit candidates remain independent review targets.

Each JSON preserves its entire corresponding historical fixture, adding only `provider_packet.approved_facts` and outside-packet `fact_authority_derivation`. Historical representative audit outputs, grades and provenance are preserved for inspection; historical wires are rejected by the current decoder. Only `provider_packet` enters a provider request. Fixture identities, labels, hashes and derivation notes remain outside that packet.

The original 16 files under `tests/fixtures/judge_calibration/` remain byte-for-byte historical evidence. Current loading first verifies those fixed byte pins, then uses `load_gold` with approval required and the fixed external gold, approval and corpus hashes. The new manifest cannot authorize a different scenario by signing its own facts.

| Derived fixtures | Approved request source | Supplied facts |
|---|---|---|
| J01–J03 | Q04 | null; general qualification rules |
| J04–J07 | N04 | complete original notice facts |
| J08 | Q01 | null; general annual guideline rules |
| J09–J12 | general source-rule controls | null; no scenario facts |
| J13 | R04 | complete original rent facts |

N04's actual service date remains null. Its effective date and hand-service method are input premises; the latest permissible service date is supplied calculated evidence and never fills the unknown actual date. R04's proposed amount comes from the approved request. Historical J13 omitted that premise from its old judge packet, so its earlier amount witness was incomplete; this new derivation does not retroactively repair the historical evidence.

Approved facts are conditional synthetic scenario premises. They do not establish a real tenancy, lawful exemptions, executed tool arguments or absence of attacks. Calculation assertions still need supplied correct tool results and relevant law. A cited positive or conflicting audit still needs its own relevant legal-source anchor; fact pointers cannot replace it. Unknown, null and none retain their existing distinct meanings.

`manifest.json` records each historical byte pin, the fixed mapping and external source identities. `sizes.json` is recomputed from the current packet, schema, system and structurally generated audit examples. Exact Unicode character and UTF-8 byte lengths are not tokens or a capacity-fit result. No provider or counting calls occur in these offline checks.
