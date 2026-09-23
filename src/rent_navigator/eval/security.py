"""Verify existing test evidence without executing attacks or importing test code."""

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final, Literal
from xml.etree import ElementTree

from pydantic import BeforeValidator, Field, model_validator

from rent_navigator.models import Sha256, StrictModel
from rent_navigator.security_cases import SecurityCaseId

SECURITY_IDS: Final = ("S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08")
_PREFIX: Final = "tests/test_security_pipeline.py::"


def _nodes(function: str, *parameters: str) -> tuple[str, ...]:
    name = _PREFIX + "test_" + function
    return tuple(f"{name}[{parameter}]" for parameter in parameters) if parameters else (name,)


REQUIRED_SECURITY_NODES: Final[dict[SecurityCaseId, tuple[str, ...]]] = {
    "S01": _nodes("question_attack_cannot_execute_native_tools", "S01")
    + _nodes("valid_question_result_keeps_exact_server_disclaimer", "S01"),
    "S02": _nodes("citations_require_supplied_canonical_evidence", "True-S02", "False-S02"),
    "S03": _nodes("s03_generated_disclaimer_field_is_rejected")
    + _nodes("valid_question_result_keeps_exact_server_disclaimer", "S03"),
    "S04": _nodes(
        "s04_changed_confirmed_arguments_fail_before_calculation", "cents", "null", "date", "scope"
    )
    + _nodes("s04_sidecar_does_not_replace_executed_rule_evidence"),
    "S05": _nodes("question_attack_cannot_execute_native_tools", "S05")
    + _nodes("citations_require_supplied_canonical_evidence", "True-S05", "False-S05")
    + _nodes("s05_sidecar_preserves_canonical_passages_and_citations")
    + _nodes("s05_baseline_never_receives_retrieval_or_sidecar"),
    "S06": _nodes("s06_contacts_redacted_before_extraction_and_question_requests")
    + _nodes("s06_typed_phone_shaped_cents_never_enter_regex_redaction")
    + _nodes(
        "contact_combinations_redacted_in_first_provider_payload",
        *(
            f"{contact}-{path}"
            for contact in (
                "dollar-overlap",
                "cad-overlap",
                "slash-emails",
                "plus-emails",
                "joined-phones",
                "postal-joined-phone",
            )
            for path in ("extraction", "question")
        ),
    ),
    "S07": _nodes("s07_excluded_scope_keeps_actual_unsupported_tool_result")
    + _nodes("s07_coercing_excluded_scope_cannot_execute_calculator"),
    "S08": _nodes("question_attack_cannot_execute_native_tools", "S08")
    + _nodes(
        "s08_refusal_text_is_owned_by_server_and_accepts_no_extra_prose",
        "None",
        "answer",
        "statements",
    ),
}


class SecurityInventory(StrictModel):
    schema_version: Annotated[
        Literal[1], BeforeValidator(lambda value: value if type(value) is int else None)
    ]
    xml_sha256: Sha256
    passed_test_count: Annotated[int, Field(ge=49)]
    security_ids: list[SecurityCaseId]
    mapped_nodes: dict[SecurityCaseId, list[str]]

    @model_validator(mode="after")
    def complete_mapping(self) -> "SecurityInventory":
        expected = {
            identifier: sorted(nodes) for identifier, nodes in REQUIRED_SECURITY_NODES.items()
        }
        if self.security_ids != list(REQUIRED_SECURITY_NODES) or self.mapped_nodes != expected:
            raise ValueError("Security inventory must contain the exact eight mapped attack IDs")
        return self


def parse_security_report(path: Path) -> SecurityInventory:
    """Require successful exact parameter variants from the existing pytest suite."""
    content = path.read_bytes()
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("Security XML declarations are not allowed")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise ValueError("Invalid security XML") from None
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Security XML must contain test suites")
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError("Security XML has no suite")
    for suite in suites:
        try:
            counts = {
                name: int(suite.attrib[name]) for name in ("tests", "errors", "failures", "skipped")
            }
        except (KeyError, ValueError):
            raise ValueError("Security XML suite counts are invalid") from None
        if counts["tests"] != len(list(suite.iter("testcase"))) or any(
            counts[name] != 0 for name in ("errors", "failures", "skipped")
        ):
            raise ValueError("Security suite was failed, incomplete, or skipped")
    nodes: list[str] = []
    for testcase in root.iter("testcase"):
        if any(child.tag in {"failure", "error", "skipped"} for child in testcase):
            raise ValueError("Security testcase did not pass")
        if testcase.attrib.get("classname") != "tests.test_security_pipeline":
            raise ValueError("Security evidence must come from the expected test module")
        name = testcase.attrib.get("name", "")
        if not name.startswith("test_"):
            raise ValueError("Security testcase identity is missing")
        nodes.append(_PREFIX + name)
    if len(nodes) < 49 or len(set(nodes)) != len(nodes):
        raise ValueError("Security suite is incomplete or repeats test cases")
    actual = set(nodes)
    if any(not set(expected) <= actual for expected in REQUIRED_SECURITY_NODES.values()):
        raise ValueError("Security suite is missing required attack variants")
    return SecurityInventory(
        schema_version=1,
        xml_sha256=sha256(content).hexdigest(),
        passed_test_count=len(nodes),
        security_ids=list(REQUIRED_SECURITY_NODES),
        mapped_nodes={
            identifier: sorted(nodes) for identifier, nodes in REQUIRED_SECURITY_NODES.items()
        },
    )
