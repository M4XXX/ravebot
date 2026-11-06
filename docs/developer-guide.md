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
# Edit .env: set DISCORD_BOT_TOKEN, NOTION_API_KEY, NOTION_PARENT_PAGE_ID, ADMIN_USER_IDS
# POSTGRES_URL default (postgresql://ravebot:ravebot@localhost:5432/ravebot) matches the compose config

# Run the bot
uv run python -m bot.main
```

> The `docker-compose.yml` in this repo is **postgres-only** — it starts a local PostgreSQL instance for development. The bot itself is run natively with `uv`. Do not `docker compose up` the bot service in production from this file; see the [User Guide](user-guide.md#5-deployment-docker-compose) for a full production compose setup.

---

## 3. Tests, Linting, Type Checking

```bash
# Run the full test suite
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
│   ├── notion/             # Notion integration (API version 2026-03-11)
│   │   ├── api.py          # NotionApi: httpx wrapper, retry/backoff, pagination
│   │   ├── models.py       # Property names, typed EventInfo/SlotInfo/DjInfo, parsers
│   │   ├── schema.py       # ensure_schema(): creates/verifies Events, Slots, DJs
│   │   ├── views.py        # ensure_views(): Overview, Lineup, All DJs
│   │   └── client.py       # NotionClient: reads and writes events, slots, DJs
│   ├── services/           # Business logic
│   │   ├── admin_booking.py
│   │   ├── dj_pages.py     # ensure_dj_page(): a DJ's row in the DJs database
│   │   ├── event_defaults.py # default start, slot length, slots per event (config)
│   │   ├── intervention.py
│   │   ├── lineup.py
│   │   ├── notion_sync.py  # NotionSyncService: applies hand edits from Notion
│   │   ├── notion_sync_plan.py # plan_sync(): pure Notion-vs-Postgres planner
│   │   ├── onboarding.py
│   │   ├── outreach.py
│   │   └── reminders.py
│   ├── ui/
│   │   └── views.py        # All Discord UI: buttons, modals, views
│   └── main.py             # Bot entry point: migrations, Notion setup, services, cogs
├── migrations/
│   └── versions/           # Alembic migration files
├── tests/                  # Tests mirroring bot/ structure
│   └── notion/
│       ├── fake_notion.py  # In-memory fake of the Notion API (httpx transport)
│       └── fixtures/       # Real API responses; NOTES.md records the API facts
├── Dockerfile              # Multi-stage build
├── docker-compose.yml      # Postgres-only (local dev)
├── pyproject.toml          # Dependencies and tool config
└── .env.example            # Canonical env var reference
```

**Architecture notes:**
- Cogs are thin command handlers that delegate to `services/` and `db/` repositories.
- All Discord UI interaction (buttons, modals, select menus) lives in `bot/ui/views.py`.
- Runtime configuration is stored in the PostgreSQL `config(guild_id, key, value)` table, read via `db_config.get_value()`.
- Notion is the source of truth for slot assignments (one Slots row per slot, `DJ` relation). The PostgreSQL `slot_locks` table only guards against two DJs taking one slot at the same moment.

### Notion setup and sync

- The bot uses the Notion API version `2026-03-11` (databases and data sources are separate; views are created via `POST /v1/views`). The only Notion settings are `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`.
- At startup, before Discord connects, `main()` runs `ensure_schema(api, db, 0, parent_page_id)` and then `ensure_views(api, db, 0, ids)`. The data source and database ids are stored in the `config` table under `guild_id = 0` (global). `ensure_schema` creates the Events, DJs and Slots databases under the parent page if none are stored, adds missing properties, starts a fresh set if a stored database is gone, and raises `NotionSchemaError` on a property with the wrong type. `ensure_views` creates only missing views and sets a marker once all three exist.
- If `ensure_schema`/`ensure_views` raise `NotionSchemaError` or `NotionWriteError`, the bot closes the pool and the Notion client and exits with a message that names the parent page sharing and both env vars.
- `EventsCog._poll_notion` (every 5 minutes, first run once the bot is ready) runs `NotionSyncService.run_cycle()` (adopt hand entries, free hand removals, revert conflicts, create/trim Slots rows), then reads the events, auto-creates future events and updates the lineup post. A sync failure is posted to the admin log and does not stop the lineup update.
- Tests use `tests/notion/fake_notion.py`, an in-memory Notion behind an httpx transport, so no test talks to the real API.

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

Migration files live in `migrations/versions/`.

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
