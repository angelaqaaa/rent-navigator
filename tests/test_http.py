"""HTTP contracts and linked metadata exercised only with synthetic provider ports."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any
from uuid import uuid4

import httpx
import pytest
from anthropic.types import MessageTokensCount
from eval_fixtures import canonical_hits
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_eval_real_collection import SyntheticRealCompositionPort

from rent_navigator.admission import Admission, DailyBudget
from rent_navigator.app import create_app
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.models import ERROR_HTTP_STATUSES, ErrorCode, ErrorResponse, disclaimer_for
from rent_navigator.provider import ProviderFailure
from rent_navigator.trace import TraceRecord

SOURCE = "1" * 40
ORIGIN = "https://example.test"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus()


def fresh_clock(corpus: Corpus) -> datetime:
    return datetime.combine(corpus.snapshot_date + timedelta(days=1), datetime.min.time(), UTC)


def make_app(
    corpus: Corpus, port: SyntheticRealCompositionPort, stream: StringIO, **kwargs: Any
) -> FastAPI:
    return create_app(
        source_commit=SOURCE,
        corpus=corpus,
        messages=port,
        metadata=stream,
        admission=Admission(public_origin=ORIGIN, budget=DailyBudget()),
        retrieve=lambda query: canonical_hits(corpus, query),
        wall_clock=lambda: fresh_clock(corpus),
        **kwargs,
    )


def question() -> dict[str, str]:
    return {"attempt_id": str(uuid4()), "mode": "question", "question": "Synthetic question."}


@pytest.mark.parametrize("code", list(ERROR_HTTP_STATUSES))
def test_every_safe_error_uses_its_contract_status(corpus: Corpus, code: ErrorCode) -> None:
    class FailurePort(SyntheticRealCompositionPort):
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            raise ProviderFailure(code)

    stream = StringIO()
    request = question()
    with TestClient(make_app(corpus, FailurePort(), stream)) as client:
        response = client.post("/api/ask", json=request)
    assert response.status_code == ERROR_HTTP_STATUSES[code]
    error = ErrorResponse.model_validate_json(response.content)
    assert str(error.attempt_id) == request["attempt_id"]
    assert error.error.code == code and error.error.message == ProviderFailure(code).message
    assert error.disclaimer == disclaimer_for(corpus.snapshot_date)
    records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
    endpoints = [row for row in records if row.record_kind == "endpoint"]
    assert len(endpoints) == 1 and endpoints[0].response_code == code


@pytest.mark.parametrize("body", [b"{", b"null", b"\xff", b'{"attempt_id":"bad"}'])
def test_invalid_json_and_request_schema_return_422(corpus: Corpus, body: bytes) -> None:
    with TestClient(make_app(corpus, SyntheticRealCompositionPort(), StringIO())) as client:
        response = client.post("/api/extract", content=body)
    assert response.status_code == 422
    error = ErrorResponse.model_validate_json(response.content)
    assert error.error.code == "invalid_request" and error.attempt_id is None


def test_multibyte_whole_body_limit_is_checked_before_json_parse(corpus: Corpus) -> None:
    port = SyntheticRealCompositionPort()
    body = (
        b" " * 500
        + json.dumps(
            {"attempt_id": str(uuid4()), "letter": "\U0001f600" * 4000}, ensure_ascii=False
        ).encode()
    )
    assert len(body.decode()) < 16384 < len(body)
    with TestClient(make_app(corpus, port, StringIO())) as client:
        response = client.post("/api/extract", content=body)
    assert response.status_code == 413 and port.actor_calls == 0
    assert ErrorResponse.model_validate_json(response.content).error.code == "body_too_large"


@pytest.mark.parametrize(
    "origin,status", [(ORIGIN, 200), ("https://foreign.test", 422), (None, 200)]
)
def test_origin_must_match_when_present(corpus: Corpus, origin: str | None, status: int) -> None:
    headers = {"origin": origin} if origin is not None else {}
    with TestClient(make_app(corpus, SyntheticRealCompositionPort(), StringIO())) as client:
        response = client.post("/api/ask", json=question(), headers=headers)
    assert response.status_code == status
    assert "access-control-allow-origin" not in response.headers


def test_sixty_first_post_without_origin_is_rate_limited(corpus: Corpus) -> None:
    with TestClient(make_app(corpus, SyntheticRealCompositionPort(), StringIO())) as client:
        for _ in range(60):
            assert client.post("/api/ask", json={}).status_code == 422
        response = client.post("/api/ask", json={})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
        assert client.get("/healthz").status_code == 200


def test_stale_posts_do_not_block_an_independent_static_get(corpus: Corpus) -> None:
    port = SyntheticRealCompositionPort()
    app = create_app(
        source_commit=SOURCE,
        corpus=corpus,
        messages=port,
        metadata=StringIO(),
        wall_clock=lambda: fresh_clock(corpus) + timedelta(days=90),
    )

    @app.get("/")
    def synthetic_page() -> str:
        return "Synthetic static GET fixture."

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        for path, body in (
            ("/api/ask", question()),
            ("/api/extract", {"attempt_id": str(uuid4()), "letter": "Synthetic letter."}),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "stale_corpus"
    assert port.actor_calls == 0


def test_extraction_and_analysis_link_attempt_with_distinct_private_traces(corpus: Corpus) -> None:
    stream = StringIO()
    attempt_id = str(uuid4())
    letter = "Synthetic private letter with contact tenant@example.test."
    text = "Synthetic private question with contact tenant@example.test."
    with TestClient(make_app(corpus, SyntheticRealCompositionPort(), stream)) as client:
        extracted = client.post("/api/extract", json={"attempt_id": attempt_id, "letter": letter})
        answered = client.post(
            "/api/ask", json={"attempt_id": attempt_id, "mode": "question", "question": text}
        )
    assert extracted.status_code == answered.status_code == 200
    assert extracted.json()["attempt_id"] == answered.json()["attempt_id"] == attempt_id
    assert extracted.json()["trace_id"] != answered.json()["trace_id"]
    records = [TraceRecord.model_validate_json(line) for line in stream.getvalue().splitlines()]
    endpoints = [row for row in records if row.record_kind == "endpoint"]
    assert {row.phase for row in endpoints} == {"extraction", "analysis"}
    assert {str(row.attempt_id) for row in endpoints} == {attempt_id}
    for secret in (letter, text, "tenant@example.test", "Synthetic private"):
        assert secret not in stream.getvalue()


def test_third_http_request_is_busy_without_queueing(corpus: Corpus) -> None:
    async def run() -> None:
        ready, release = asyncio.Event(), asyncio.Event()

        class BlockingPort(SyntheticRealCompositionPort):
            waiting = 0

            async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
                self.waiting += 1
                if self.waiting == 2:
                    ready.set()
                await release.wait()
                return await super().count_tokens(**kwargs)

        app = make_app(corpus, BlockingPort(), StringIO())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = asyncio.create_task(client.post("/api/ask", json=question()))
            second = asyncio.create_task(client.post("/api/ask", json=question()))
            await asyncio.wait_for(ready.wait(), 2)
            busy = await client.post("/api/ask", json=question())
            assert busy.status_code == 429 and busy.json()["error"]["code"] == "busy"
            release.set()
            responses = await asyncio.gather(first, second)
            assert all(response.status_code == 200 for response in responses)

    asyncio.run(run())


def test_http_deadline_covers_free_preflight_before_generation(corpus: Corpus) -> None:
    current = 0.0

    class ExpiringPort(SyntheticRealCompositionPort):
        async def count_tokens(self, **kwargs: Any) -> MessageTokensCount:
            nonlocal current
            current = 46.0
            return await super().count_tokens(**kwargs)

    port = ExpiringPort()
    app = make_app(corpus, port, StringIO(), monotonic=lambda: current)
    with TestClient(app) as client:
        response = client.post("/api/ask", json=question())
    assert response.status_code == 504 and response.json()["error"]["code"] == "deadline_exceeded"
    assert port.actor_calls == 0


def test_framework_422_is_the_safe_error_envelope(corpus: Corpus) -> None:
    stream = StringIO()
    app = make_app(corpus, SyntheticRealCompositionPort(), stream)

    @app.get("/synthetic-validation")
    def validation_fixture(amount: int) -> int:
        return amount

    with TestClient(app) as client:
        response = client.get("/synthetic-validation?amount=synthetic-invalid-value")
    assert response.status_code == 422
    error = ErrorResponse.model_validate_json(response.content)
    assert error.error.code == "invalid_request" and error.attempt_id is None
    assert "synthetic-invalid-value" not in response.text + stream.getvalue()
