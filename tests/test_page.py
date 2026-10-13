"""Page, initial confirmations and metadata privacy without a provider client."""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml  # type: ignore[import-untyped]
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from test_eval_real_collection import SyntheticRealCompositionPort

from rent_navigator.app import create_app
from rent_navigator.corpus import load_corpus
from rent_navigator.models import ERROR_HTTP_STATUSES, RentFacts, ToolResult, disclaimer_for
from rent_navigator.page import page_html
from rent_navigator.rent import rent_increase_check

SOURCE = "2" * 40
SENTINEL = "synthetic-letter-or-key-never-logged"


def test_page_has_verbatim_disclaimer_controls_and_real_fixture_footer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = load_corpus()
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    sink = StringIO()

    def forbidden(**kwargs: Any) -> Any:
        raise AssertionError("Page and health must not create a provider client")

    monkeypatch.setattr("rent_navigator.app.create_client", forbidden)
    with TestClient(create_app(source_commit=SOURCE, corpus=corpus, metadata=sink)) as client:
        response = client.get("/")
        health = client.get("/healthz").json()
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    page = BeautifulSoup(response.text, "html.parser")
    disclaimer = page.select_one("#disclaimer")
    assert disclaimer is not None and disclaimer.get_text() == disclaimer_for(corpus.snapshot_date)
    assert not disclaimer.has_attr("contenteditable")
    source = page.select_one("#source-commit")
    snapshot = page.select_one("#snapshot-date")
    assert source is not None and source.get_text() == health["source_commit"] == SOURCE
    assert snapshot is not None and snapshot.get_text() == corpus.snapshot_date.isoformat()
    controls = page.select(
        ".ordinary, #scope-excluded, #period-confirmed, #period-excluded, #facts-confirmed"
    )
    assert len(controls) == 9 and all(not control.has_attr("checked") for control in controls)
    assert page.select_one("#letter[maxlength='4000']") is not None
    assert page.select_one("#question[maxlength='1500']") is not None
    assert page.select_one("#facts-form[hidden]") is not None
    assert SENTINEL not in response.text + sink.getvalue() + capsys.readouterr().out
    assert sink.getvalue() == ""
    assert response.headers["cache-control"] == "no-store"
    assert "connect-src 'self'" in response.headers["content-security-policy"]


def test_page_script_has_only_same_origin_routes_and_no_unsafe_render_or_storage() -> None:
    corpus = load_corpus()
    page = BeautifulSoup(page_html(corpus.snapshot_date, SOURCE), "html.parser")
    scripts = page.find_all("script")
    assert len(scripts) == 1 and not scripts[0].has_attr("src")
    script = scripts[0].get_text()
    assert set(re.findall(r'["\'](/[^"\']+)["\']', script)) == {
        "/api/extract",
        "/api/ask",
        "/healthz",
    }
    assert "crypto.randomUUID()" in script
    assert not page.select("img, iframe, object, embed, audio, video, link")
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "console.",
        "sendBeacon",
        "document.cookie",
        "eval(",
        "https://",
    ):
        assert forbidden not in script


@pytest.mark.parametrize("code", list(ERROR_HTTP_STATUSES))
def test_every_contract_error_has_a_fixed_plain_words_message(code: str) -> None:
    page = BeautifulSoup(page_html(load_corpus().snapshot_date, SOURCE), "html.parser")
    script = page.find("script")
    assert script is not None
    assert re.search(rf"\b{code}:\s*\"[A-Z]", script.get_text())


def test_stale_corpus_keeps_page_and_health_available_without_provider_calls() -> None:
    corpus = load_corpus()
    port = SyntheticRealCompositionPort()
    sink = StringIO()
    clock = datetime.combine(corpus.snapshot_date + timedelta(days=91), datetime.min.time(), UTC)
    app = create_app(
        source_commit=SOURCE, corpus=corpus, metadata=sink, messages=port, wall_clock=lambda: clock
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        response = client.post(
            "/api/extract", json={"attempt_id": str(uuid4()), "letter": SENTINEL}
        )
        assert response.status_code == 503 and response.json()["error"]["code"] == "stale_corpus"
    assert port.actor_calls == port.judge_calls == 0 and SENTINEL not in sink.getvalue()


def test_server_metadata_is_escaped_before_html_rendering() -> None:
    html = page_html(load_corpus().snapshot_date, "<script>synthetic_attack()</script>")
    page = BeautifulSoup(html, "html.parser")
    assert len(page.find_all("script")) == 1
    source = page.select_one("#source-commit")
    assert source is not None and source.get_text() == "<script>synthetic_attack()</script>"


def test_blueprint_is_one_gated_docker_service_without_source_or_secret_values() -> None:
    data = yaml.safe_load(Path("render.yaml").read_text())
    assert set(data) == {"services"} and len(data["services"]) == 1
    service = data["services"][0]
    assert service["type"] == "web" and service["runtime"] == "docker"
    assert service["dockerfilePath"] == "./Dockerfile" and service["numInstances"] == 1
    assert service["autoDeploy"] is False and service["healthCheckPath"] == "/healthz"
    environment = {row["key"]: row for row in service["envVars"]}
    assert set(environment) == {"ANTHROPIC_API_KEY", "PORT", "PUBLIC_ORIGIN", "DEMO_DAILY_USD"}
    for name in ("ANTHROPIC_API_KEY", "PUBLIC_ORIGIN"):
        assert environment[name] == {"key": name, "sync": False}
    assert environment["PORT"]["value"] == "8000"
    assert environment["DEMO_DAILY_USD"]["value"] == "0"


_DOM_RUNNER = r"""
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() {
    return this._text + this.children.map(child => child.textContent).join("\n");
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; this._text = ""; }
  addEventListener() {}
}
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element("div"));
    return elements.get(id);
  },
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; }
};
document.getElementById("source-commit").textContent = input.source;
const before = JSON.stringify(input.response.tool_result);
const context = {
  document, URL,
  fetch(path) {
    if (path !== "/healthz") throw new Error("Unexpected synthetic request");
    return Promise.resolve({ok: true,
      json: async () => ({status: "ok", source_commit: input.source})});
  }
};
vm.runInNewContext(input.script, context);
context.renderAnswer(input.response);
const result = document.getElementById("result");
process.stdout.write(JSON.stringify({
  text: result.textContent,
  paragraphs: result.children.filter(child => child.tagName === "p")
    .map(child => child.textContent),
  visible: !result.hidden,
  toolUnchanged: JSON.stringify(input.response.tool_result) === before
}));
"""


def render_tool_card(tool: ToolResult | dict[str, Any]) -> dict[str, Any]:
    """Execute the full emitted script with an isolated DOM and a fake health response."""
    page = BeautifulSoup(page_html(load_corpus().snapshot_date, SOURCE), "html.parser")
    script = page.find("script")
    assert script is not None
    data = {
        "source": SOURCE,
        "script": script.get_text(),
        "response": {
            "status": "answered",
            "answer": "Synthetic generated explanation without uncertainty information.",
            "tool_result": tool.model_dump(mode="json") if isinstance(tool, ToolResult) else tool,
            "citations": [],
        },
    }
    result = subprocess.run(
        ["node", "-e", _DOM_RUNNER],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert isinstance(output, dict)
    assert output["visible"] is True and output["toolUnchanged"] is True
    return output


def rounding_facts(**changes: Any) -> RentFacts:
    return RentFacts.model_validate_json(
        json.dumps(
            {
                "scope": {"ordinary": "confirmed", "period_start": "confirmed"},
                "effective_on": "2026-09-01",
                "served_on": "2026-06-01",
                "service_method": "hand",
                "current_cents": 200001,
                "proposed_cents": 204202,
                "tenancy_start": "2025-09-01",
                "last_increase": {"state": "known", "date": "2025-09-01"},
                "guideline_status": "controlled",
                "form": "N1",
                **changes,
            }
        )
    )


def test_complete_facts_rounding_uncertainty_has_a_fixed_policy_card() -> None:
    tool = rent_increase_check(rounding_facts(), corpus=load_corpus())
    assert tool.status == "cannot_determine" and tool.cap_cents_exact == "204201.021"
    assert [
        (check.id, check.status, check.reason) for check in tool.checks if check.status != "pass"
    ] == [("guideline", "unknown", "rounding_uncertain")]
    output = render_tool_card(tool)
    assert "fraction of a cent" in output["text"]
    assert "project's policy" in output["text"]
    assert "not a statutory rounding rule" in output["text"]
    assert "More facts are needed" not in output["text"]


def test_missing_fact_card_does_not_invent_rounding_uncertainty() -> None:
    tool = rent_increase_check(
        rounding_facts(current_cents=200000, proposed_cents=204200, served_on=None),
        corpus=load_corpus(),
    )
    assert tool.status == "cannot_determine"
    assert any(check.reason == "missing_fact" for check in tool.checks)
    assert all(check.reason != "rounding_uncertain" for check in tool.checks)
    output = render_tool_card(tool)
    assert "More facts are needed" in output["text"]
    assert "fraction of a cent" not in output["text"]


def test_rounding_and_missing_fact_cards_explain_both_causes() -> None:
    tool = rent_increase_check(rounding_facts(served_on=None), corpus=load_corpus())
    assert tool.status == "cannot_determine"
    assert {"missing_fact", "rounding_uncertain"} <= {check.reason for check in tool.checks}
    output = render_tool_card(tool)
    assert "More facts are needed" in output["text"]
    assert "fraction of a cent" in output["text"]


def test_definite_failure_keeps_its_status_alongside_rounding_uncertainty() -> None:
    tool = rent_increase_check(rounding_facts(served_on="2026-08-01"), corpus=load_corpus())
    assert tool.status == "fails_checked_rules"
    assert any(check.status == "fail" for check in tool.checks)
    assert any(check.reason == "rounding_uncertain" for check in tool.checks)
    output = render_tool_card(tool)
    assert "At least one checked condition is not met." in output["paragraphs"]
    assert "fraction of a cent" in output["text"]
    assert "More facts are needed" not in output["text"]
    assert "The checked conditions cannot be fully determined." not in output["paragraphs"]


def test_missing_scope_precedence_is_preserved_despite_a_definite_notice_failure() -> None:
    tool = rent_increase_check(
        rounding_facts(
            scope={"ordinary": "unknown", "period_start": "confirmed"}, served_on="2026-08-01"
        ),
        corpus=load_corpus(),
    )
    assert tool.status == "cannot_determine"
    assert any(check.status == "fail" for check in tool.checks)
    output = render_tool_card(tool)
    assert "More facts are needed" in output["text"]
    assert "At least one checked condition is not met." not in output["paragraphs"]


def test_unknown_without_check_evidence_uses_neutral_words() -> None:
    # Missing check evidence cannot justify guessing a cause of uncertainty.
    tool = rent_increase_check(rounding_facts(), corpus=load_corpus()).model_dump(mode="json")
    tool["checks"] = []
    output = render_tool_card(tool)
    assert "The checked conditions cannot be fully determined." in output["paragraphs"]
    assert "More facts are needed" not in output["text"]
    assert "fraction of a cent" not in output["text"]


@pytest.mark.parametrize("excluded", [False, True])
def test_passing_and_excluded_cards_remain_unchanged(excluded: bool) -> None:
    tool = rent_increase_check(
        rounding_facts(
            current_cents=200000,
            proposed_cents=204200,
            scope={
                "ordinary": "excluded" if excluded else "confirmed",
                "period_start": "confirmed",
            },
        ),
        corpus=load_corpus(),
    )
    assert tool.status == ("unsupported" if excluded else "passes_checked_rules")
    output = render_tool_card(tool)
    assert (
        "This scenario is outside the supported scope."
        if excluded
        else "The checked conditions are met."
    ) in output["paragraphs"]
    assert "fraction of a cent" not in output["text"]
