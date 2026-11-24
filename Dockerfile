# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.29 AS uv
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY evals ./evals
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 verstak && useradd --uid 10001 --gid verstak --create-home verstak
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY evals ./evals
USER verstak
EXPOSE 8000 8001
CMD ["uvicorn", "verstak.api:app", "--host", "0.0.0.0", "--port", "8000"]
