# RaveBot — Developer Guide

Local development setup, testing, linting, Docker builds, and release workflow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local Development Setup](#2-local-development-setup)
3. [Tests, Linting, Type Checking](#3-tests-linting-type-checking)
4. [Project Structure](#4-project-structure)
5. [Manual Docker Build & Release](#5-manual-docker-build--release)
6. [Database Migrations](#6-database-migrations)
7. [Future: Automated Releases](#7-future-automated-releases)

---

## 1. Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager (`pip install uv` or see uv docs)
- **Docker** — for the local PostgreSQL container
- **Git**

---

## 2. Local Development Setup

```bash
# Clone and enter the project
git clone <repo-url>
cd output/ravebot

# Install all dependencies including dev extras (creates .venv automatically)
uv sync

# Start PostgreSQL only — the bot runs natively, NOT via compose
docker compose up -d
# Verify: docker compose ps  →  postgres should show as healthy

# Copy and fill in env vars
cp .env.example .env
# Edit .env: set DISCORD_BOT_TOKEN, NOTION_API_KEY, NOTION_DATABASE_ID, ADMIN_USER_IDS
# POSTGRES_URL default (postgresql://ravebot:ravebot@localhost:5432/ravebot) matches the compose config

# Run the bot
uv run python -m bot.main
```

> The `docker-compose.yml` in this repo is **postgres-only** — it starts a local PostgreSQL instance for development. The bot itself is run natively with `uv`. Do not `docker compose up` the bot service in production from this file; see the [User Guide](user-guide.md#5-deployment-docker-compose) for a full production compose setup.

---

## 3. Tests, Linting, Type Checking

```bash
# Run the full test suite (real-SQL tests in tests/pg are skipped unless enabled, see below)
uv run pytest

# Verbose output with per-test names
uv run pytest -v

# Run a specific test module or directory
uv run pytest tests/cogs/
uv run pytest tests/services/

# Linting (ruff: 100-char line limit, E/F/I/UP rule sets)
uv run ruff check .

# Auto-fix safe linting issues
uv run ruff check . --fix

# Type checking (pyright, basic mode, Python 3.13)
uv run pyright bot/
```

All checks should pass before opening a PR or building a release image.

Markers: `passive` and `real_slots` switch off two convenience fixtures in `tests/conftest.py`
(an always-active bot and slot state taken from the Notion relation count). Tests marked `pg`
run real SQL and migrations against a disposable PostgreSQL and are skipped unless
`RAVEBOT_TEST_POSTGRES_URL` is set:

```bash
docker run --rm -d --name ravebot-testpg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=ravebot -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ravebot_test postgres:16-alpine
RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot:test@127.0.0.1:55432/ravebot_test uv run pytest -m pg
docker stop ravebot-testpg
```

Never point it at real data — the schema is dropped and recreated.

---

## 4. Project Structure

```
output/ravebot/
├── bot/
│   ├── cogs/               # Slash command handlers
│   │   ├── admin.py        # /config, /intervene, /admin, /status, /list-djs, /add-event
│   │   ├── booking.py      # DJ self-service booking (thin shell — logic in views.py)
│   │   ├── events.py       # Lineup post auto-update, event auto-creation
│   │   ├── onboarding.py   # DJ onboarding DM flow
│   │   └── outreach_cog.py # Proactive outreach background loop
│   ├── db/                 # asyncpg repository layer
│   ├── notion/             # Notion REST API client
│   ├── services/           # Business logic
│   │   ├── admin_booking.py
│   │   ├── intervention.py
│   │   ├── lineup.py
│   │   ├── onboarding.py
│   │   ├── outreach.py
│   │   └── reminders.py
│   ├── ui/
│   │   └── views.py        # All Discord UI: buttons, modals, views
│   └── main.py             # Bot entry point, startup validation
├── migrations/
│   └── versions/           # 6 Alembic migration files (Epics 1–7)
├── tests/                  # 461 tests mirroring bot/ structure (+ 5 opt-in real-SQL tests in tests/pg)
├── Dockerfile              # Multi-stage build
├── docker-compose.yml      # Postgres-only (local dev)
├── pyproject.toml          # Dependencies and tool config
└── .env.example            # Canonical env var reference
```

**Architecture notes:**
- Cogs are thin command handlers that delegate to `services/` and `db/` repositories.
- All Discord UI interaction (buttons, modals, select menus) lives in `bot/ui/views.py`.
- Runtime configuration is stored in the PostgreSQL `config(guild_id, key, value)` table, read via `db_config.get_value()`.
- Notion holds event metadata only. Slot assignments are authoritative in the `slot_locks` PostgreSQL table.

---

## 5. Manual Docker Build & Release

The Dockerfile uses a two-stage build:
- **`builder` stage** — installs `uv` and resolves all runtime dependencies into `.venv`
- **`runtime` stage** — `python:3.13-slim` with `.venv` copied in, running as non-root user `ravebot`
- **CMD**: `python -m bot.main`

```bash
# Build the image
cd output/ravebot
docker build -t ravebot:latest .

# Tag for a specific release
docker tag ravebot:latest ravebot:v1.0.0

# --- Option A: save/load (no registry needed) ---
docker save ravebot:v1.0.0 | gzip > ravebot-v1.0.0.tar.gz
# On the target host:
docker load < ravebot-v1.0.0.tar.gz

# --- Option B: push to a registry ---
docker tag ravebot:v1.0.0 ghcr.io/YOUR_USER/ravebot:v1.0.0
docker tag ravebot:v1.0.0 ghcr.io/YOUR_USER/ravebot:latest
docker push ghcr.io/YOUR_USER/ravebot:v1.0.0
docker push ghcr.io/YOUR_USER/ravebot:latest

# On Unraid: Docker → Add Container → Repository: ghcr.io/YOUR_USER/ravebot:v1.0.0
```

---

## 6. Database Migrations

Migrations are managed with **Alembic** and run automatically at container startup (`alembic upgrade head` is called before the bot connects).

```bash
# Apply all pending migrations (runs automatically at startup — manual use for dev)
uv run alembic upgrade head

# Check current migration state
uv run alembic current

# Create a new auto-generated migration after changing SQLAlchemy models
uv run alembic revision --autogenerate -m "add_some_table"

# Review the generated file in migrations/versions/ before applying
uv run alembic upgrade head
```

Migration files live in `migrations/versions/`. There are currently 6 migration files covering the full schema through Epics 1–7.

---

## 7. Future: Automated Releases

> This section is a placeholder for future CI/CD automation. No workflow files exist yet.

When automated, the build and release workflow would typically:

1. **Trigger** on a git tag push (e.g. `v*`)
2. **Run gates:** `uv run pytest` and `uv run ruff check .` must pass
3. **Build** the Docker image: `docker build -t ravebot:$TAG .`
4. **Tag** with both the git tag and `latest`
5. **Push** to the configured container registry (Docker Hub, GHCR, or private)
6. **Optionally notify** the deployment host to pull the new image (webhook, SSH, Watchtower)

Candidate tools: **GitHub Actions**, **Gitea Actions**, or a simple `Makefile` + shell script.
