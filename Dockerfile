# RaveBot — Multi-stage Dockerfile
#
# Build stage: install uv + resolve all dependencies into a .venv
# Runtime stage: copy .venv + source into a slim Python image
#
# Usage:
#   docker build -t ravebot .
#   docker run --env-file .env ravebot
#
# All configuration is via environment variables — no config files mounted.
# [Source: architecture.md#Infrastructure & Deployment]

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /app

# Install uv — pinned so builds are reproducible; bump together with your local uv
# (`uv --version`) when uv.lock is regenerated
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Optimise uv for Docker builds
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install runtime dependencies (no dev deps) into .venv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source after deps (better layer caching)
COPY bot/ bot/
COPY migrations/ migrations/
COPY alembic.ini .

# Install project itself
RUN uv sync --frozen --no-dev

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /app/.venv .venv

# Copy application source
COPY --from=builder /app/bot bot
COPY --from=builder /app/migrations migrations
COPY --from=builder /app/alembic.ini .

# Add .venv to PATH; disable stdout buffering for readable logs
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false ravebot
USER ravebot

CMD ["python", "-m", "bot.main"]
