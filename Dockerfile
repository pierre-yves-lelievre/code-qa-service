# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install runtime deps into an isolated venv for clean copying
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python python3.12 \
    --link-mode=copy \
    && uv pip install --python /build/.venv/bin/python -e . --no-deps

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy the pre-built venv
COPY --from=builder /build/.venv /opt/venv

# Copy application source
COPY app/ app/

# Non-root user
RUN adduser --disabled-password --no-create-home appuser \
    && mkdir -p /app/models \
    && chown -R appuser:appuser /app

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV MODELS_DIR=/app/models

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

USER appuser

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
