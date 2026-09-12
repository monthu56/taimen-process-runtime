# Контекст сборки — корень суперпроекта (там же лежит platform-auth-sdk):
#   docker build -f process-runtime/Dockerfile .
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv
WORKDIR /app/process-runtime
COPY platform-auth-sdk /app/platform-auth-sdk
COPY control-plane/client /app/control-plane/client
COPY process-runtime/pyproject.toml process-runtime/uv.lock process-runtime/README.md ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 process
WORKDIR /app/process-runtime
COPY --from=builder /app /app
COPY process-runtime/alembic.ini ./
COPY process-runtime/migrations ./migrations
COPY process-runtime/definitions ./definitions
COPY process-runtime/src ./src
COPY process-runtime/pyproject.toml process-runtime/README.md ./
ENV PATH="/app/process-runtime/.venv/bin:$PATH" \
    PYTHONPATH="/app/process-runtime/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PR_DEFINITIONS_DIR=/app/process-runtime/definitions
USER process
EXPOSE 8030
CMD ["uvicorn", "process_runtime.app:app", "--host", "0.0.0.0", "--port", "8030"]
