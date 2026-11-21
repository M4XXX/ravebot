# RaveBot — Multi-stage Dockerfile
#
# Build stage: install uv + resolve all dependencies into a .venv
# Runtime stage: copy .venv + source into a slim Python image
#
# Usage:
#   docker build -t ravebot .
#   docker run --env-file .env ravebot
#
# All configuration is via environment variables; mount /data/backups to keep the
# safety exports (docker run -v ravebot-backups:/data/backups ...).
# [Source: architecture.md#Infrastructure & Deployment]

# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Optimise uv for Docker builds
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install runtime dependencies (no dev deps) into .venv
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source after deps (better layer caching)
COPY bot/ bot/
COPY migrations/ migrations/
COPY alembic.ini LICENSE NOTICE COMMERCIAL-LICENSE.md ./

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
COPY --from=builder /app/alembic.ini /app/LICENSE /app/NOTICE /app/COMMERCIAL-LICENSE.md ./

# Add .venv to PATH; disable stdout buffering for readable logs
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The bot runs as the non-root user ravebot (uid 1000). The entrypoint starts as root
# only to make a mapped backup folder writable for it, then drops privileges (setpriv).
RUN useradd --uid 1000 --user-group --no-create-home --shell /bin/false ravebot
# Safety exports before a restore, rebuild or upgrade import (BACKUP_DIR). Map a host
# folder or a named volume here so they survive a container recreate.
RUN mkdir -p /data/backups && chown ravebot:ravebot /data/backups && chmod 700 /data/backups
VOLUME ["/data/backups"]
COPY docker/entrypoint.sh /usr/local/bin/ravebot-entrypoint
RUN chmod 755 /usr/local/bin/ravebot-entrypoint

# Admin page (default port 8080; change with ADMIN_PAGE_PORT and map it in the container settings)
EXPOSE 8080

ENTRYPOINT ["ravebot-entrypoint"]
CMD ["python", "-m", "bot.main"]
