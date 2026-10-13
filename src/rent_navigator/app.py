"""HTTP serving adapter with private admission and metadata-only tracing."""

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TextIO, cast
from uuid import UUID, uuid4

from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import TypeAdapter, ValidationError

from rent_navigator.admission import BODY_LIMIT_BYTES, Admission, DailyBudget, utc_now
from rent_navigator.agent import RetrievalContext, agent_config_hash, answer
from rent_navigator.corpus import Corpus, load_corpus
from rent_navigator.extract import extract_letter, extraction_config_hash
from rent_navigator.guards import redact_text
from rent_navigator.index import SearchHit, build_index, search
from rent_navigator.models import (
    ASK_REQUEST_ADAPTER,
    ERROR_HTTP_STATUSES,
    AskResponse,
    CanonicalUUID,
    ErrorDetail,
    ErrorResponse,
    ExtractRequest,
    ExtractResponse,
    SourceCommit,
    StrictModel,
    disclaimer_for,
)
from rent_navigator.page import page_html
from rent_navigator.provider import (
    Deadline,
    MessagesPort,
    ProviderAdapter,
    ProviderFailure,
    create_client,
)
from rent_navigator.service_checks import require_fresh_corpus
from rent_navigator.trace import (
    PRICING_HASH,
    MetadataSink,
    ResponseCode,
    TraceContext,
    TraceRecorder,
)


class HealthResponse(StrictModel):
    status: Literal["ok"]
    source_commit: SourceCommit


def create_app(
    *,
    source_commit: str | None = None,
    corpus: Corpus | None = None,
    messages: MessagesPort | None = None,
    admission: Admission | None = None,
    metadata: TextIO | None = None,
    retrieve: Callable[[str], tuple[SearchHit, ...] | RetrievalContext] | None = None,
    wall_clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Require a build revision; tests may explicitly inject a synthetic revision."""
    revision = source_commit if source_commit is not None else os.environ.get("SOURCE_COMMIT")
    try:
        checked_revision = TypeAdapter(SourceCommit).validate_python(revision)
    except ValidationError:
        raise RuntimeError("SOURCE_COMMIT must be a full lowercase Git commit SHA") from None

    source = load_corpus() if corpus is None else corpus
    if admission is None:
        try:
            budget = DailyBudget(
                Decimal(os.environ.get("DEMO_DAILY_USD", "0.30")), clock=wall_clock
            )
        except (InvalidOperation, ValueError):
            raise RuntimeError("DEMO_DAILY_USD must be a decimal within zero and 0.30") from None
        admission = Admission(
            public_origin=os.environ.get("PUBLIC_ORIGIN"), budget=budget, clock=wall_clock
        )
    gate = admission
    sink = MetadataSink(sys.stdout if metadata is None else metadata)
    client: AsyncAnthropic | None = None
    index_directory: TemporaryDirectory[str] | None = None
    index_path: Path | None = None

    def provider() -> ProviderAdapter:
        nonlocal client
        port = messages
        if port is None:
            if client is None:
                client = create_client(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            port = cast(MessagesPort, client.messages)
        return ProviderAdapter(port, budget=gate.budget)

    def retrieval(query: str) -> tuple[SearchHit, ...] | RetrievalContext:
        nonlocal index_directory, index_path
        if retrieve is not None:
            return retrieve(query)
        if index_path is None:
            index_directory = TemporaryDirectory(prefix="rent-navigator-index-")
            index_path = Path(index_directory.name) / "index.sqlite3"
            build_index(index_path, source)
        return search(index_path, query, expected_corpus_hash=source.corpus_hash)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if client is not None:
                await client.close()
            if index_directory is not None:
                index_directory.cleanup()

    app = FastAPI(
        title="Rent Navigator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        debug=False,
        lifespan=lifespan,
    )

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", source_commit=checked_revision)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            page_html(source.snapshot_date, checked_revision),
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                ),
            },
        )

    def context(
        attempt_id: UUID | None,
        trace_id: UUID,
        phase: Literal["extraction", "analysis"],
        mode: Literal["question", "notice", "rent"] = "question",
    ) -> TraceContext:
        return TraceContext(
            attempt_id=attempt_id,
            trace_id=trace_id,
            phase=phase,
            source_commit=checked_revision,
            config_hash=extraction_config_hash()
            if phase == "extraction"
            else agent_config_hash(mode, "production"),
            corpus_hash=source.corpus_hash,
            pricing_hash=PRICING_HASH,
        )

    def error_response(failure: ProviderFailure, ctx: TraceContext) -> JSONResponse:
        response = ErrorResponse(
            attempt_id=ctx.attempt_id,
            trace_id=ctx.trace_id,
            error=ErrorDetail(code=failure.code, message=failure.message),
            snapshot_date=source.snapshot_date,
            disclaimer=disclaimer_for(source.snapshot_date),
        )
        return JSONResponse(
            content=response.model_dump(mode="json"), status_code=ERROR_HTTP_STATUSES[failure.code]
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
        phase: Literal["extraction", "analysis"] = (
            "extraction" if request.url.path == "/api/extract" else "analysis"
        )
        ctx = context(None, uuid4(), phase)
        TraceRecorder(ctx, sink, monotonic=monotonic, utc_now=wall_clock).finish("invalid_request")
        return error_response(ProviderFailure("invalid_request"), ctx)

    async def handle(request: Request, phase: Literal["extraction", "analysis"]) -> JSONResponse:
        ctx = context(None, uuid4(), phase)
        trace: TraceRecorder | None = None
        core_started = False
        code: ResponseCode = "provider_error"
        failure: ProviderFailure | None = None
        deadline = Deadline.start(clock=monotonic, duration_seconds=45.0)
        try:
            async with deadline.limit(), gate.enter(request.headers.get("origin")):
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > BODY_LIMIT_BYTES:
                        raise ProviderFailure("body_too_large")
                    body.extend(chunk)
                deadline.check()
                try:
                    text = bytes(body).decode("utf-8")
                    payload = json.loads(text)
                    if isinstance(payload, dict):
                        try:
                            attempt_id = TypeAdapter(CanonicalUUID).validate_python(
                                payload.get("attempt_id")
                            )
                        except ValidationError:
                            attempt_id = None
                        ctx = context(attempt_id, ctx.trace_id, phase)
                    if phase == "extraction":
                        extracted_request = ExtractRequest.model_validate_json(text)
                        ctx = context(extracted_request.attempt_id, ctx.trace_id, phase)
                    else:
                        ask_request = ASK_REQUEST_ADAPTER.validate_json(text)
                        ctx = context(ask_request.attempt_id, ctx.trace_id, phase, ask_request.mode)
                except (ValidationError, ValueError, UnicodeError):
                    raise ProviderFailure("invalid_request") from None
                require_fresh_corpus(
                    source.snapshot_date, today=wall_clock().astimezone(UTC).date()
                )
                gate.budget.require_available()
                adapter = provider()
                response: ExtractResponse | AskResponse
                if phase == "extraction":
                    trace = TraceRecorder(ctx, sink, monotonic=monotonic, utc_now=wall_clock)
                    extracted = await extract_letter(
                        extracted_request,
                        provider=adapter,
                        trace=trace,
                        redact=redact_text,
                        deadline=deadline,
                    )
                    response = ExtractResponse(
                        attempt_id=extracted_request.attempt_id,
                        trace_id=ctx.trace_id,
                        extraction=extracted,
                        snapshot_date=source.snapshot_date,
                        disclaimer=disclaimer_for(source.snapshot_date),
                    )
                else:
                    core_started = True
                    response = await answer(
                        ask_request,
                        provider=adapter,
                        corpus=source,
                        retrieve=retrieval,
                        redact=redact_text,
                        deadline=deadline,
                        context=ctx,
                        sink=sink,
                    )
                code = "ok"
                return JSONResponse(content=response.model_dump(mode="json"))
        except (ProviderFailure, asyncio.CancelledError) as error:
            failure = ProviderFailure.from_exception(error)
        except Exception:
            failure = ProviderFailure("provider_error")
        finally:
            if failure is not None:
                code = failure.code
            if trace is not None:
                trace.finish(code)
            elif not core_started:
                TraceRecorder(ctx, sink, monotonic=monotonic, utc_now=wall_clock).finish(code)
        assert failure is not None
        return error_response(failure, ctx)

    @app.post("/api/extract")
    async def api_extract(request: Request) -> JSONResponse:
        return await handle(request, "extraction")

    @app.post("/api/ask")
    async def api_ask(request: Request) -> JSONResponse:
        return await handle(request, "analysis")

    return app
