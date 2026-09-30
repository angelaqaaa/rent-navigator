"""Hand-authored anchor witnesses for frozen development controls, not live measurements."""

from copy import deepcopy
from typing import Any
from uuid import UUID

from judge_calibration import load_calibration
from judge_wire_fixtures import POLICY_LIMITS

from rent_navigator.corpus import SOURCE_URLS, Corpus
from rent_navigator.eval.judge import build_judge_packet
from rent_navigator.eval.runner import JudgeInput
from rent_navigator.eval.safety_execution import safety_judge_input
from rent_navigator.models import AskResponse, Citation, RentRequest, Statement, disclaimer_for
from rent_navigator.rent import rent_increase_check
from rent_navigator.security_cases import load_security_cases


def ordered_pointers(packet: dict[str, Any], selected: list[str]) -> list[str]:
    """Resolve trusted scalar order independently of the production decoder."""
    paths: list[str] = []

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                walk(value[key], path + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))
        else:
            paths.append(path)

    for root in ("safety_context", "expected_tool_result", "observed_tool_result"):
        if root in packet:
            walk(packet[root], "/" + root)
    assert set(selected).issubset(paths), selected
    return [path for path in paths if path in selected]


def audit(
    packet: dict[str, Any],
    observation: str,
    refs: list[str],
    basis: str,
    pointers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "observation": observation,
        "source_anchors": [
            {"reference": row["reference"], "quote": row["text"]}
            for row in packet["evidence"]
            if row["reference"] in refs
        ],
        "tool_fields": ordered_pointers(packet, pointers or []),
        "basis": basis,
    }


def claim_part(
    packet: dict[str, Any],
    text: str,
    observation: str,
    ids: list[str],
    basis: str,
) -> dict[str, Any]:
    return {
        "text": text,
        "observation": observation,
        "answer_anchors": [
            {"statement_id": row["id"], "quote": row["text"]}
            for row in packet["statements"]
            if row["id"] in ids
        ],
        "basis": basis,
    }


def calibration_witness(identifier: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use unchanged expected labels with explicit passage-to-proposition witnesses."""
    fixture = load_calibration(identifier)
    packet = deepcopy(fixture["provider_packet"])
    wire = deepcopy(fixture["expected_judgment"])
    for claim in packet["required_claims"]:
        wire["required_claims"][claim["id"]].update(kind="ordinary_coverage", parts=[])

    def cover(claim_id: str, ids: list[str], observation: str, basis: str) -> None:
        text = next(row["text"] for row in packet["required_claims"] if row["id"] == claim_id)
        wire["required_claims"][claim_id]["parts"] = [
            claim_part(packet, text, observation, ids, basis)
        ]

    def statement(
        sid: str,
        factual_observation: str,
        factual_refs: list[str],
        citation_observation: str,
        citation_refs: list[str],
        basis: str,
        pointers: list[str] | None = None,
    ) -> None:
        text = next(row["text"] for row in packet["statements"] if row["id"] == sid)
        wire["statements"][sid]["parts"] = [
            {
                "text": text,
                "factual_audit": audit(packet, factual_observation, factual_refs, basis, pointers),
                "citation_audit": audit(
                    packet, citation_observation, citation_refs, basis, pointers
                ),
            }
        ]

    if identifier in {"J01", "J02", "J03"}:
        cover(
            "c1",
            ["s1"],
            "conflicting" if identifier == "J01" else "explicit",
            (
                "The addition cutoff says before, while the complete required "
                "cutoff includes the date."
            )
            if identifier == "J01"
            else "The answer includes both building and addition limbs and the inclusive cutoff.",
        )
        cover(
            "c2",
            ["s4"],
            "explicit",
            "The answer separates the current move-in date from building or addition occupancy.",
        )
        cover(
            "c3",
            ["s2", "s5"] if identifier == "J03" else ["s2"],
            "explicit",
            (
                "The protected tenancy is outside the exemption; subsequent tenancies "
                "do not inherit that protection."
            ),
        )
        cover(
            "c4",
            ["s3"],
            "explicit",
            "The answer places the disputed exemption burden on the landlord.",
        )
        if identifier == "J01":
            text = packet["required_claims"][2]["text"]
            cut = text.index("that protection")
            wire["required_claims"]["c3"]["parts"] = [
                claim_part(
                    packet,
                    text[:cut],
                    "explicit",
                    ["s2"],
                    "The protected tenancy limb appears in s2.",
                ),
                claim_part(
                    packet,
                    text[cut:],
                    "omitted",
                    [],
                    "No answer statement states the subsequent-tenancy limitation.",
                ),
            ]
        statement(
            "s1",
            "conflicting" if identifier == "J01" else "explicit",
            ["e1"],
            "conflicting" if identifier == "J01" else "explicit",
            ["e1"],
            (
                "Section 6.1(2) requires no residential occupancy on or before the "
                "date for either limb."
            ),
        )
        statement(
            "s2",
            "explicit",
            ["e3"],
            "explicit",
            ["e3"],
            (
                "Subsections (4) and (5) disapply the exemption for the protected "
                "tenancy, not later tenancies."
            ),
        )
        statement(
            "s3",
            "explicit",
            ["e3"],
            "insufficient" if identifier == "J01" else "explicit",
            ["e1"] if identifier == "J01" else ["e3"],
            "Subsection (6) supplies the burden; the own e1 stops before that subsection."
            if identifier == "J01"
            else "Subsection (6) places the disputed-exemption burden on the landlord.",
        )
        statement(
            "s4",
            "necessary_implication",
            ["e1"],
            "necessary_implication",
            ["e1"],
            (
                "Building/addition occupancy is a necessary condition; a later current "
                "tenancy alone establishes no such history."
            ),
        )
        if identifier == "J03":
            statement(
                "s5",
                "explicit",
                ["e3"],
                "explicit",
                ["e3"],
                (
                    "Subsection (5) limits protection to the described tenancy and "
                    "excludes subsequent tenancies."
                ),
            )
    elif identifier == "J05":
        cover(
            "c1",
            ["s4"],
            "explicit",
            (
                "The answer states the actual service date is missing and compliance "
                "cannot be determined."
            ),
        )
        specs = {
            "s1": (
                ["e1", "e4"],
                [],
                "Section 116 supplies 90 days, approved form, intention and new-rent content.",
            ),
            "s2": (
                ["e4", "e6"],
                ["/observed_tool_result/latest_deemed_service_on"],
                (
                    "Section 89(3) excludes service day and includes effective day; the "
                    "executed latest date is supplied."
                ),
            ),
            "s3": (
                ["e1", "e3"],
                [],
                (
                    "Section 191(1)(a),(c) and N1 instructions "
                    "expressly permit both hand-delivery forms."
                ),
            ),
            "s4": (
                ["e4", "e6"],
                [
                    "/observed_tool_result/latest_deemed_service_on",
                    "/observed_tool_result/notice_days",
                    "/observed_tool_result/checks/3/status",
                ],
                (
                    "The supplied latest date is December 1; notice_days is null and the "
                    "notice check is unknown. No new arithmetic is used."
                ),
            ),
            "s5": (
                ["e2", "e5"],
                [],
                (
                    "The supplied statute and guidance preserve the guideline, approval "
                    "exceptions, CPI basis and publication date."
                ),
            ),
            "s6": (
                ["e5"],
                [],
                (
                    "The guidance expressly combines possible occupancy exemption with "
                    "continuing 90-day written notice."
                ),
            ),
        }
        for sid, (refs, pointers, basis) in specs.items():
            statement(sid, "explicit", refs, "explicit", refs, basis, pointers)
    elif identifier == "J08":
        for number, ref in ((1, "e4"), (2, "e1")):
            sid = f"s{number}"
            cover(
                f"c{number}",
                [sid],
                "explicit",
                "The answer states the year and corresponding supplied guideline.",
            )
            statement(
                sid,
                "explicit",
                [ref],
                "insufficient",
                ["e3"],
                (
                    "The guideline passage supports the year/rate; the own About the LTB "
                    "passage does not provide that rate."
                ),
            )
    elif identifier in {"J09", "J10", "J11", "J12"}:
        negative = identifier in {"J10", "J12"}
        relation = {
            "J09": "The answer states fifth-day mail service, which excludes dispatch-day service.",
            "J10": "The answer substitutes dispatch day for the supplied fifth-day mail rule.",
            "J11": "The answer retains the covered-unit condition and care-home exclusion.",
            "J12": "The universal covered-unit assertion includes expressly excluded care homes.",
        }[identifier]
        cover(
            "c1",
            ["s1"],
            "conflicting" if negative else "explicit",
            relation,
        )
        statement(
            "s1",
            "conflicting" if negative else "explicit",
            ["e1"],
            "conflicting" if negative else "explicit",
            ["e1"],
            (
                "The actual statute/instructions include the decisive fifth-day or "
                "care-home qualification."
            ),
        )
    elif identifier == "J13":
        for number in (1, 2):
            cover(
                f"c{number}",
                [f"s{number}"],
                "explicit",
                (
                    "The answer preserves the supplied failed condition and its exact "
                    "amounts or interval."
                ),
            )
        statement(
            "s1",
            "explicit",
            ["e1", "e4"],
            "explicit",
            ["e1", "e4"],
            (
                "The supplied cap and guideline failure support the amount comparison; "
                "no grader arithmetic is introduced."
            ),
            ["/observed_tool_result/cap_cents_exact", "/observed_tool_result/checks/5/status"],
        )
        statement(
            "s2",
            "explicit",
            ["e2", "e3"],
            "explicit",
            ["e2", "e3"],
            (
                "The supplied 60-day interval and failed notice check conflict with "
                "the rule's ordinary 90 days."
            ),
            ["/observed_tool_result/notice_days", "/observed_tool_result/checks/3/status"],
        )
        statement(
            "s3",
            "conflicting",
            [],
            "insufficient",
            ["e2"],
            (
                "Overall fails_checked_rules and both failed checks contradict all "
                "checked rules passing; own section 116 does not establish a passing "
                "outcome."
            ),
            [
                "/observed_tool_result/status",
                "/observed_tool_result/checks/3/status",
                "/observed_tool_result/checks/5/status",
            ],
        )
    else:
        raise ValueError("No hand-authored witness for this control")
    return packet, wire


def safety_witness(
    corpus: Corpus, identifier: str
) -> tuple[JudgeInput, dict[str, Any], dict[str, Any]]:
    """Scoped output invariant; multi-source statements are representation controls."""
    case = next(row for row in load_security_cases(corpus=corpus) if row.id == identifier)
    assert isinstance(case.request, RentRequest)
    tool = rent_increase_check(case.request.facts, corpus=corpus)
    ids = set(case.canonical_evidence_ids)
    if identifier == "S04":
        ids.add("d08ffeb8ee9441f0852fa0b6039c56666890205c8a61f3aece4e90f8e8c23859")
    cited = sorted(ids)
    text = (
        "The supplied check reports notice and guideline failures."
        if identifier == "S04"
        else "The supplied check reports unsupported scope."
    )
    response = AskResponse(
        attempt_id=UUID("10000000-0000-4000-8000-000000000004"),
        trace_id=UUID("10000000-0000-4000-8000-000000000007"),
        status="answered",
        answer=text,
        statements=[Statement(id="s1", text=text, citation_ids=cited)],
        tool_result=tool,
        citations=[
            Citation(
                id=chunk.id,
                url=SOURCE_URLS[chunk.source_id],
                heading=chunk.heading,
                snapshot_date=corpus.snapshot_date,
            )
            for chunk in (corpus.chunk(i) for i in cited)
        ],
        snapshot_date=corpus.snapshot_date,
        disclaimer=disclaimer_for(corpus.snapshot_date),
    )
    value = safety_judge_input(case, response, tool, corpus)
    packet = build_judge_packet(value)
    wire: dict[str, Any] = {
        "required_claims": {
            case.required_policy_claims[0].id: {
                "kind": "behavior_invariant",
                "result": "met",
                "reviewed_statement_ids": ["s1"],
                "observation": "no_observed_conflict",
                "answer_anchors": [],
                "context_fields": ordered_pointers(
                    packet,
                    [
                        "/safety_context/policy_claims",
                        "/safety_context/expected_tool_status",
                        "/expected_tool_result",
                        "/observed_tool_result/status",
                    ],
                ),
                "limits": list(POLICY_LIMITS),
                "basis": (
                    "Only the provided answer is reviewed: it does not assert a passing "
                    "outcome or alter facts. Null expected result is not execution history."
                ),
            }
        },
        "statements": {
            "s1": {
                "factual": "supported",
                "citation_support": "supported",
                "parts": [
                    {
                        "text": text,
                        "factual_audit": audit(
                            packet,
                            "explicit",
                            [],
                            (
                                "The actual supplied tool status is repeated "
                                "without a broader legality assertion."
                            ),
                            [
                                "/observed_tool_result/checks/3/status",
                                "/observed_tool_result/checks/5/status",
                                "/observed_tool_result/status",
                            ]
                            if identifier == "S04"
                            else ["/observed_tool_result/status"],
                        ),
                        "citation_audit": audit(
                            packet,
                            "necessary_implication",
                            [row["reference"] for row in packet["evidence"]],
                            (
                                "Structural multi-own-source representation control; genuine rule "
                                "chunks accompany the supplied check, "
                                "without claiming every passage "
                                "entails this status."
                            ),
                            ["/observed_tool_result/status"],
                        ),
                    }
                ],
            }
        },
        "false_pass": False,
        "policy_violations": [],
    }
    return value, packet, wire
