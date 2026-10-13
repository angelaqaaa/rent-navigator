"""Separately inspect pinned history and load externally bound current derivations."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from judge_wire_fixtures import grade_only_wire

from rent_navigator.corpus import SOURCE_URLS, Corpus, load_corpus
from rent_navigator.eval.data import GoldDataset, load_gold
from rent_navigator.eval.judge import _WIRE_POLICY, JUDGE_SYSTEM, judge_config_hash, judge_schema
from rent_navigator.eval.models import Claim
from rent_navigator.eval.runner import (
    ApprovedFacts,
    JudgeInput,
    approved_facts_from_case,
    validate_approved_facts,
)
from rent_navigator.model_policy import JUDGE_MODEL
from rent_navigator.models import AskResponse, Citation, Statement, ToolResult, disclaimer_for

HISTORICAL_CALIBRATION_DIRECTORY = Path(__file__).parent / "fixtures" / "judge_calibration"
CALIBRATION_DIRECTORY = Path(__file__).parent / "fixtures" / "judge_calibration_v2"
APPROVED_DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "eval"
CALIBRATION_IDS = tuple(f"J{number:02}" for number in range(1, 14))
APPROVED_GOLD_HASH = "575cfd2b66465633f7cb04b44eb8455293c1a9c24a4911c0482879313d9b82cb"
APPROVED_APPROVAL_HASH = "f44c07fa54c7c012222fccc79b9ce30ddf576a7191ddb6b0353f969148a1a55d"
APPROVED_CORPUS_HASH = "8be6cf66829e42c59cbec66896d1c762de130283989623a05edc30b427246720"
FIXED_SOURCE_MAPPING: dict[str, str | None] = {
    "J01": "Q04",
    "J02": "Q04",
    "J03": "Q04",
    "J04": "N04",
    "J05": "N04",
    "J06": "N04",
    "J07": "N04",
    "J08": "Q01",
    "J09": None,
    "J10": None,
    "J11": None,
    "J12": None,
    "J13": "R04",
}
HISTORICAL_FILE_HASHES = {
    "J01.json": "11a82f55d0e06c6983c05d081e1f28f80a3c5bfebd7b765e6764c15513b5b21a",
    "J02.json": "ffac28e0108d127b012f4b03bf226f14ae4d9cd723f3e86678440b16029ffbd7",
    "J03.json": "b521aea2a2c3703b6165533662ab8e6963507a448d23e5258f317578525185df",
    "J04.json": "2360a0919ad30751750304450a35e822700a7abab3a4a2bad9995067f5936095",
    "J05.json": "0903b7909e051802113f23b5beab8a37214161d32883936b7a2136195dc6f29a",
    "J06.json": "b93e100c7efe5524b15710e097e01c6099d89b071c6de961eae89fe60c26a5be",
    "J07.json": "567f7f07445fd0bc9a0847b016a3909ad9a2683457bea950137f5db862b1b0fe",
    "J08.json": "fa6cda9b1d19637b33f8b842c67af85d7f72502d6a89b531208c9914b010d413",
    "J09.json": "28222d442e80474a955479291713dac90e7cb18a6717543a7483c4420814303a",
    "J10.json": "c246a80f89fb66dce4cf396760474ddeb7f149f25e04b7b217a44a1db1877ee0",
    "J11.json": "3b10c1c68be507005c5357cf2716c116fe9ff55222c42ce2a84545ea8f6f5c89",
    "J12.json": "5c838fb4c4f1719ea480477ca13b5492d6aae3d382cd7678725cb237195fa821",
    "J13.json": "4c05269dc2deed8ad73b349a8e30d74d5684558c44fd8419a5dde6d8d333def7",
    "README.md": "938cee214d5820ed5fabcf743f4de798e9400d952699dbbf470c953f4a357a4a",
    "manifest.json": "793cf6f3a74616a27e80c796ddefdbab55ef97a912daa5e2a25f2bbbebc8c507",
    "sizes.json": "c8c4082647e0197d231d47bd291e59c07376406caddc4578b6a48d2e9a0dac8a",
}


def compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify_historical_calibration() -> None:
    """Fixed original byte pins are independent of the new derived manifest."""
    names = {path.name for path in HISTORICAL_CALIBRATION_DIRECTORY.iterdir() if path.is_file()}
    if names != set(HISTORICAL_FILE_HASHES):
        raise ValueError("Historical calibration antecedents changed")
    actual = {
        name: sha256((HISTORICAL_CALIBRATION_DIRECTORY / name).read_bytes()).hexdigest()
        for name in HISTORICAL_FILE_HASHES
    }
    if actual != HISTORICAL_FILE_HASHES:
        raise ValueError("Historical calibration antecedents changed")


def load_historical_calibration(identifier: str) -> dict[str, Any]:
    """Inspect immutable old inputs; this is never a current packet fallback."""
    if identifier not in CALIBRATION_IDS:
        raise ValueError("Unknown calibration fixture")
    verify_historical_calibration()
    value: dict[str, Any] = json.loads(
        (HISTORICAL_CALIBRATION_DIRECTORY / f"{identifier}.json").read_text()
    )
    return value


def approved_calibration_dataset(corpus: Corpus) -> GoldDataset:
    """Load the fixed externally approved source before deriving any scenario facts."""
    dataset = load_gold(APPROVED_DATA_DIRECTORY, corpus)
    if (
        dataset.gold_hash != APPROVED_GOLD_HASH
        or dataset.approval_hash != APPROVED_APPROVAL_HASH
        or corpus.corpus_hash != APPROVED_CORPUS_HASH
    ):
        raise ValueError("Calibration fact source differs from fixed external approval")
    return dataset


def calibration_approved_facts(identifier: str, corpus: Corpus) -> ApprovedFacts | None:
    """Only the independently approved request supplies the mapped scenario envelope."""
    dataset = approved_calibration_dataset(corpus)
    if identifier not in FIXED_SOURCE_MAPPING:
        raise ValueError("Unknown calibration fixture")
    case_id = FIXED_SOURCE_MAPPING[identifier]
    if case_id is None:
        return None
    case = next(case for case in dataset.cases if case.id == case_id)
    return approved_facts_from_case(case)


def derive_calibration(identifier: str, corpus: Corpus) -> dict[str, Any]:
    """Reconstruct exactly old content plus the approved envelope and outside-packet binding."""
    value = load_historical_calibration(identifier)
    facts = calibration_approved_facts(identifier, corpus)
    value["provider_packet"]["approved_facts"] = (
        facts.model_dump(mode="json") if facts is not None else None
    )
    value["fact_authority_derivation"] = {
        "schema_version": 1,
        "historical_fixture_file": f"tests/fixtures/judge_calibration/{identifier}.json",
        "historical_fixture_sha256": HISTORICAL_FILE_HASHES[f"{identifier}.json"],
        "gold_file_sha256": APPROVED_GOLD_HASH,
        "approval_file_sha256": APPROVED_APPROVAL_HASH,
        "corpus_hash": APPROVED_CORPUS_HASH,
        "approved_case_id": FIXED_SOURCE_MAPPING[identifier],
        "fact_source": "GoldCase.request.facts" if facts is not None else "null",
        "packet_policy_version": 3,
        "wire_policy_version": 5,
        "historical_amount_gap": identifier == "J13",
    }
    return value


def load_calibration(
    identifier: str, *, corpus: Corpus | None = None, directory: Path = CALIBRATION_DIRECTORY
) -> dict[str, Any]:
    """Reject self-consistent local rehashes that disagree with the fixed approved source."""
    expected = derive_calibration(identifier, corpus if corpus is not None else load_corpus())
    value: dict[str, Any] = json.loads((directory / f"{identifier}.json").read_text())
    if value != expected:
        raise ValueError("Derived calibration differs from independently approved derivation")
    return value


def calibration_input(packet: dict[str, Any], corpus: Corpus) -> JudgeInput:
    """Reconstruct the real packet seam through the unchanged public JSON boundary."""
    statements = [Statement.model_validate_json(json.dumps(row)) for row in packet["statements"]]
    cited_ids = sorted({identifier for item in statements for identifier in item.citation_ids})
    actual = (
        ToolResult.model_validate_json(json.dumps(packet["observed_tool_result"]))
        if packet["observed_tool_result"] is not None
        else None
    )
    response = AskResponse(
        attempt_id=UUID("11111111-1111-4111-8111-111111111111"),
        trace_id=UUID("22222222-2222-4222-8222-222222222222"),
        status="answered",
        answer="\n".join(item.text for item in statements),
        statements=statements,
        tool_result=actual,
        citations=[
            Citation(
                id=identifier,
                url=SOURCE_URLS[corpus.chunk(identifier).source_id],
                heading=corpus.chunk(identifier).heading,
                snapshot_date=corpus.snapshot_date,
            )
            for identifier in cited_ids
        ],
        snapshot_date=corpus.snapshot_date,
        disclaimer=disclaimer_for(corpus.snapshot_date),
    )
    response = AskResponse.model_validate_json(response.model_dump_json())
    return JudgeInput(
        approved_facts=validate_approved_facts(packet["approved_facts"]),
        required_claims=tuple(
            Claim.model_validate_json(json.dumps(row)) for row in packet["required_claims"]
        ),
        evidence=tuple(corpus.chunk(row["id"]) for row in packet["evidence"] if row["canonical"]),
        expected_tool_result=(
            ToolResult.model_validate_json(json.dumps(packet["expected_tool_result"]))
            if packet["expected_tool_result"] is not None
            else None
        ),
        response=response,
        actual_tool_result=actual,
        cited_evidence=tuple(corpus.chunk(identifier) for identifier in cited_ids),
    )


def tool_leaf_paths(packet: dict[str, Any]) -> list[str]:
    """Enumerate RFC6901 scalar/null paths independently for structural sizing."""
    result: list[str] = []

    def visit(value: object, pointer: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                child = value[key]
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                visit(child, f"{pointer}/{escaped}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}")
        else:
            result.append(pointer)

    for root in ("expected_tool_result", "observed_tool_result"):
        if packet[root] is not None:
            visit(packet[root], f"/{root}")
    return result


def stress_audit_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """A valid shape stress payload, deliberately not a semantic judgment."""
    packet = fixture["provider_packet"]
    output: dict[str, Any] = deepcopy(fixture["representative_audit_output"])
    all_ids = [row["id"] for row in packet["statements"]]
    all_refs = [row["reference"] for row in packet["evidence"]]
    pointers = tool_leaf_paths(packet)
    for leaf in output["required_claims"].values():
        leaf["answer_statement_ids"] = all_ids
        leaf["coverage_basis"] = "x" * 360
    for statement in packet["statements"]:
        leaf = output["statements"][statement["id"]]
        leaf["factual_basis"] = {
            "summary": "x" * 320,
            "evidence_refs": all_refs,
            "tool_fields": pointers,
        }
        if statement["citation_ids"]:
            leaf["citation_basis"] = {
                "summary": "x" * 320,
                "evidence_refs": [
                    row["reference"]
                    for row in packet["evidence"]
                    if row["id"] in statement["citation_ids"]
                ],
                "tool_fields": pointers,
            }
    return output


def grade_only_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """Map unchanged proposed expected labels to the current wire without new grading."""
    return grade_only_wire(fixture["expected_judgment"], fixture["provider_packet"])


def grade_only_stress_output(fixture: dict[str, Any]) -> dict[str, Any]:
    """Largest label strings and policy inventory; structural, not semantic, output."""
    wire = grade_only_output(fixture)
    for row in wire["required_claims"]:
        row["result"] = "contradicted"
    citations = {
        row["id"]: bool(row["citation_ids"]) for row in fixture["provider_packet"]["statements"]
    }
    for row in wire["statements"]:
        row["factual"] = "contradicted"
        row["citation_support"] = "unsupported" if citations[row["id"]] else "not_applicable"
    wire["false_pass"] = False
    wire["policy_violations"] = [f"S{n:02}" for n in range(1, 9)]
    return wire


def calibration_sizes(fixture: dict[str, Any]) -> dict[str, int]:
    from rent_navigator.judge_profile import judge_request_settings

    packet = fixture["provider_packet"]
    schema = judge_schema(packet)
    payload: dict[str, Any] = {
        "model": JUDGE_MODEL,
        "system": JUDGE_SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": json.dumps(packet, sort_keys=True, separators=(",", ":")),
            }
        ],
        "thinking": judge_request_settings()["thinking"],
        "output_config": {
            **judge_request_settings()["output_config"],
            "format": {"type": "json_schema", "schema": schema},
        },
    }
    stress_packet = deepcopy(packet)
    for statement in stress_packet["statements"]:
        statement["text"] = "x" * 600
    stress_payload = deepcopy(payload)
    stress_payload["output_config"]["format"]["schema"] = judge_schema(stress_packet)
    stress_payload["messages"] = [
        {
            "role": "user",
            "content": json.dumps(stress_packet, sort_keys=True, separators=(",", ":")),
        }
    ]
    inputs = {
        "packet": packet,
        "schema": schema,
        "count_payload_without_timeout": payload,
        "structural_stress_count_payload_without_timeout": stress_payload,
        "representative_output": grade_only_output(fixture),
        "structural_stress_output": grade_only_stress_output(fixture),
    }
    return {
        f"{name}_{unit}": len(text.encode()) if unit == "utf8_bytes" else len(text)
        for name, value in inputs.items()
        for text in (compact_json(value),)
        for unit in ("characters", "utf8_bytes")
    }


def regenerate_size_worksheets() -> None:
    """Recompute both authorized worksheets while preserving original manifests/packets."""
    from judge_schema_resolution import INITIAL_GRADE_LIMITS, schema_metrics

    corpus = load_corpus()
    fixtures = {
        identifier: load_calibration(identifier, corpus=corpus) for identifier in CALIBRATION_IDS
    }
    for directory in (
        CALIBRATION_DIRECTORY,
        CALIBRATION_DIRECTORY.parent / "judge_schema_representation_v1",
    ):
        manifest_bytes = (directory / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        document = {
            "schema_version": 2,
            "purpose": "offline_grade_only_calibration_sizes",
            "representation": _WIRE_POLICY["schema_representation"],
            "packet_policy_version": 3,
            "wire_policy_version": _WIRE_POLICY["version"],
            "judge_config_hash": judge_config_hash(),
            "historical_manifest_sha256": sha256(manifest_bytes).hexdigest(),
            "historical_sizing_file_sha256": manifest["sizing_file_sha256"],
            "provider_calls": 0,
            "count_calls": 0,
            "token_measurement": None,
            "budget_status": "initial_offline_not_provider_accepted",
            "initial_schema_limits": INITIAL_GRADE_LIMITS,
            "depth_convention": (
                "Raw JSON dict/list container depth; root=1; "
                "definitions counted once; refs not expanded."
            ),
            "limitations": (
                "Synthetic byte/structure measurements, not token counts, provider acceptance "
                "or semantic accuracy. Original manifests remain historical."
            ),
            "fixtures": {
                identifier: {
                    **calibration_sizes(fixture),
                    "schema_metrics": schema_metrics(judge_schema(fixture["provider_packet"])),
                }
                for identifier, fixture in fixtures.items()
            },
        }
        (directory / "sizes.json").write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        )
