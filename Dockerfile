# ── Stage 1: web ──────────────────────────────────────────────────────────────
FROM node:24.13.0-slim AS web

WORKDIR /build/web

# Install the locked dependencies first so the layer survives source edits
COPY web/package.json web/package-lock.json ./
RUN npm ci

# Type-check and build; vite writes ../app/static, i.e. /build/app/static
COPY web/ ./
RUN npm run build

# ── Stage 2: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv (pinned; never latest)
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

# Install runtime deps into an isolated venv for clean copying
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python python3.12 \
    --link-mode=copy

# ── Stage 3: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# git is needed to shallow-clone the repositories being indexed
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the pre-built venv
COPY --from=builder /build/.venv /opt/venv

# Copy application source
COPY app/ app/

# Copy the page built by the web stage (app/static is kept out of the build context)
COPY --from=web /build/app/static app/static

# Non-root user
RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

USER appuser

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
