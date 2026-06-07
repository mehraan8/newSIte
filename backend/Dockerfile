# syntax=docker/dockerfile:1.7
###############################################################################
# Multi-stage build for the FastAPI modular monolith.
#  - builder: resolves wheels into an isolated venv (cacheable layer)
#  - runtime: slim, non-root, no build toolchain in the final image
# The same image serves the API, the dev server, and the Alembic migrate job;
# the command is overridden per service in docker-compose.yml.
###############################################################################

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build tools only live here; they never reach the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Isolated virtualenv keeps the runtime copy trivial.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

# Minimal OS deps: libpq for psycopg, curl is intentionally omitted
# (health checks use the bundled Python interpreter instead).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --no-create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY . .

# Drop privileges — the process never runs as root.
RUN chown -R app:app /app
USER app

EXPOSE 8000

# Container-level health: hit the app's own /health endpoint via stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else sys.exit(1)"]

# Default (production) command; dev & migrate services override this.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
