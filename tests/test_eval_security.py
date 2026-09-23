"""Hand-authored XML evidence fixtures, independent of live policy judgments."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from rent_navigator.eval.security import parse_security_report


def synthetic_security_xml(path: Path) -> Path:
    names = [
        *(
            f"test_question_attack_cannot_execute_native_tools[{case}]"
            for case in ("S01", "S05", "S08")
        ),
        *(
            f"test_citations_require_supplied_canonical_evidence[{forged}-{case}]"
            for forged in ("True", "False")
            for case in ("S02", "S05")
        ),
        "test_s03_generated_disclaimer_field_is_rejected",
        "test_valid_question_result_keeps_exact_server_disclaimer[S01]",
        "test_valid_question_result_keeps_exact_server_disclaimer[S03]",
        *(
            f"test_s04_changed_confirmed_arguments_fail_before_calculation[{field}]"
            for field in ("cents", "null", "date", "scope")
        ),
        "test_s04_sidecar_does_not_replace_executed_rule_evidence",
        "test_s05_sidecar_preserves_canonical_passages_and_citations",
        "test_s05_baseline_never_receives_retrieval_or_sidecar",
        "test_s06_contacts_redacted_before_extraction_and_question_requests",
        "test_s06_typed_phone_shaped_cents_never_enter_regex_redaction",
        *(
            f"test_contact_combinations_redacted_in_first_provider_payload[{contact}-{stage}]"
            for contact in (
                "dollar-overlap",
                "cad-overlap",
                "slash-emails",
                "plus-emails",
                "joined-phones",
                "postal-joined-phone",
            )
            for stage in ("extraction", "question")
        ),
        "test_s07_excluded_scope_keeps_actual_unsupported_tool_result",
        "test_s07_coercing_excluded_scope_cannot_execute_calculator",
        *(
            f"test_s08_refusal_text_is_owned_by_server_and_accepts_no_extra_prose[{extra}]"
            for extra in ("None", "answer", "statements")
        ),
    ]
    names.extend(f"test_synthetic_additional_boundary_{index}" for index in range(49 - len(names)))
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="49", failures="0", errors="0", skipped="0")
    for name in names:
        ET.SubElement(
            suite, "testcase", classname="tests.test_security_pipeline", name=name, time="0.001"
        )
    path.write_bytes(ET.tostring(root))
    return path


def test_synthetic_xml_has_all_eight_mapped_attack_inventories(tmp_path: Path) -> None:
    report = parse_security_report(synthetic_security_xml(tmp_path / "security.xml"))
    assert report.passed_test_count == 49
    assert report.security_ids == [f"S0{index}" for index in range(1, 9)]
    assert len(report.mapped_nodes["S04"]) == 5
    assert all("::test_" in node for nodes in report.mapped_nodes.values() for node in nodes)


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_synthetic_testcase_outcome_cannot_be_hidden_by_zero_suite_counts(
    tmp_path: Path, outcome: str
) -> None:
    path = synthetic_security_xml(tmp_path / "security.xml")
    root = ET.fromstring(path.read_bytes())
    ET.SubElement(next(root.iter("testcase")), outcome)
    path.write_bytes(ET.tostring(root))
    with pytest.raises(ValueError, match="did not pass"):
        parse_security_report(path)


@pytest.mark.parametrize(
    "mutation", ["missing_variant", "duplicate", "wrong_module", "count", "failed_suite", "empty"]
)
def test_synthetic_xml_rejects_incomplete_or_unrelated_evidence(
    tmp_path: Path, mutation: str
) -> None:
    path = synthetic_security_xml(tmp_path / "security.xml")
    root = ET.fromstring(path.read_bytes())
    suite = next(root.iter("testsuite"))
    cases = list(root.iter("testcase"))
    if mutation == "missing_variant":
        cases[0].set("name", "test_unrelated_padding")
    elif mutation == "duplicate":
        cases[0].set("name", cases[1].attrib["name"])
    elif mutation == "wrong_module":
        cases[0].set("classname", "tests.unrelated")
    elif mutation == "count":
        suite.set("tests", "48")
    elif mutation == "failed_suite":
        suite.set("errors", "1")
    else:
        suite.clear()
    path.write_bytes(ET.tostring(root))
    with pytest.raises(ValueError):
        parse_security_report(path)


@pytest.mark.parametrize(
    "content", [b"<bad", b"<other/>", b"<!DOCTYPE testsuites><testsuites/>", b"<testsuites/>"]
)
def test_synthetic_malformed_xml_is_not_acceptance(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "security.xml"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        parse_security_report(path)
