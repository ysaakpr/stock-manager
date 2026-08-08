# syntax=docker/dockerfile:1
#
# The app image. One image for the whole monolith — status API today, scheduler and CLI later
# (decision #13: one deployable). Built on python:3.12-slim because the host's 3.9 is never
# used and 3.13+ is unverified for this dep set (B3); dependencies come from uv.lock, so the
# container runs the exact resolution `make check` runs on the host.

FROM python:3.12-slim

# uv from its own published image rather than a pip install into the system interpreter, so
# the version is pinned and the base image's site-packages stays empty.
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies in their own layer: they are re-resolved only when uv.lock changes, so editing
# a module does not re-download the world. CLAUDE.md is here because pyproject names it as the
# project readme and the build backend reads it.
COPY pyproject.toml uv.lock CLAUDE.md ./
RUN uv sync --frozen --no-dev --no-install-project

# The §8.2 packages. tests/ and orchestrator/ are deliberately absent: the image is the
# product, not the build machinery.
COPY dataplatform ./dataplatform
COPY analyst ./analyst
COPY execution ./execution
COPY backtest ./backtest
COPY accounting ./accounting
RUN uv sync --frozen --no-dev

# Non-root. The only path the process writes is DATA_ROOT, a bind mount from the host; uid
# 1000 is fixed so a Linux host can chown the lake to a known owner (see ops/README.md).
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

# uvicorn from the project venv, which is already first on PATH — no `uv run` at runtime, so
# the container never tries to re-resolve dependencies while starting.
CMD ["uvicorn", "dataplatform.status.api:app", "--host", "0.0.0.0", "--port", "8000"]
