"""Health endpoint and application factory."""

import os
from typing import Literal

from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError

from rent_navigator.models import SourceCommit, StrictModel


class HealthResponse(StrictModel):
    status: Literal["ok"]
    source_commit: SourceCommit


def create_app(*, source_commit: str | None = None) -> FastAPI:
    """Require a build revision; tests may explicitly inject a synthetic revision."""
    revision = source_commit if source_commit is not None else os.environ.get("SOURCE_COMMIT")
    try:
        checked_revision = TypeAdapter(SourceCommit).validate_python(revision)
    except ValidationError:
        raise RuntimeError("SOURCE_COMMIT must be a full lowercase Git commit SHA") from None

    app = FastAPI(
        title="Rent Navigator",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        debug=False,
    )

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(status="ok", source_commit=checked_revision)

    return app
