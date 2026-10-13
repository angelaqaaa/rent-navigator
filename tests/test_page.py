"""Page, initial confirmations and metadata privacy without a provider client."""

import re
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
from rent_navigator.models import ERROR_HTTP_STATUSES, disclaimer_for
from rent_navigator.page import page_html

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
