"""Health checks with explicitly synthetic revision fixtures."""

import importlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from rent_navigator.app import create_app

SYNTHETIC_SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_health_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SOURCE_COMMIT", SYNTHETIC_SOURCE_COMMIT)
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "source_commit": SYNTHETIC_SOURCE_COMMIT}


def test_health_is_the_only_route() -> None:
    with TestClient(create_app(source_commit=SYNTHETIC_SOURCE_COMMIT)) as client:
        for path in ("/", "/api/ask", "/api/extract", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 404
        assert client.post("/healthz").status_code == 405


@pytest.mark.parametrize("revision", [None, "", "unknown", "abc123", "A" * 40, "g" * 40])
def test_missing_or_invalid_source_commit_fails_startup(
    monkeypatch: pytest.MonkeyPatch, revision: str | None
) -> None:
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    if revision is not None:
        monkeypatch.setenv("SOURCE_COMMIT", revision)
    with pytest.raises(RuntimeError, match="full lowercase Git commit SHA"):
        create_app()


def test_fixture_revision_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    with TestClient(create_app(source_commit=SYNTHETIC_SOURCE_COMMIT)) as client:
        assert client.get("/healthz").json()["source_commit"] == SYNTHETIC_SOURCE_COMMIT


def test_runtime_and_selected_dependencies() -> None:
    assert sys.version_info[:2] == (3, 12)
    for module in ("rent_navigator", "anthropic", "bs4", "pypdf", "yaml", "uvicorn"):
        importlib.import_module(module)
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE VIRTUAL TABLE synthetic_probe USING fts5(text)")
