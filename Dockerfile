FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

COPY --from=ghcr.io/astral-sh/uv:0.11.21@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

ARG SOURCE_COMMIT
RUN python -c 'import re, sys; assert re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]), "SOURCE_COMMIT must be a full Git SHA"' "$SOURCE_COMMIT"
ENV SOURCE_COMMIT=$SOURCE_COMMIT
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT

USER 65532:65532
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c 'import json, os, urllib.request; result=json.load(urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3)); assert result == {"status": "ok", "source_commit": os.environ["SOURCE_COMMIT"]}'
CMD ["uvicorn", "rent_navigator.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
