# RaveBot — User Guide

Operator reference for deploying and configuring RaveBot.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Notion Setup](#2-notion-setup)
3. [Environment Variables](#3-environment-variables)
4. [Deployment: Unraid (Primary)](#4-deployment-unraid-primary)
5. [Deployment: Docker Compose](#5-deployment-docker-compose)
6. [Deployment: Podman](#6-deployment-podman)
7. [First-Run Checklist](#7-first-run-checklist)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

Before deploying RaveBot you need:

- **Discord Application** — a bot account with a token from [discord.com/developers/applications](https://discord.com/developers/applications).
  > **Critical:** Under **Bot** → **Privileged Gateway Intents**, enable **Server Members Intent**. Without it, `on_member_update` never fires and DJ onboarding never triggers automatically.
- **Notion account** — a Notion workspace with an internal integration. Created at [notion.so/my-integrations](https://www.notion.so/my-integrations).
- **PostgreSQL 16** — a PostgreSQL instance reachable from the bot container. On Unraid this is typically a separate PostgreSQL Docker container.
- **Docker** (or Podman) on your host machine.

---

## 2. Notion Setup

RaveBot uses a **single Notion database** for both event pages and DJ profile pages. The same database serves both purposes — unused properties are simply left blank per page type.

### 2.1 Create an Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**
3. Give it a name (e.g. "RaveBot"), select your workspace, set type to **Internal**
4. Click **Submit**, then copy the **Internal Integration Secret** (starts with `secret_`)

### 2.2 Create the Events + DJs Database

Create a new full-page database in Notion. This single database will hold both your rave event pages and your DJ profile pages.

### 2.3 Share the Database with Your Integration

Open the database page in Notion, click the `...` menu (top right), select **Add connections**, and choose your integration.

### 2.4 Required Database Properties

Add the following properties to your database:

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | Auto-created on every Notion database — already present |
| `Date` | Date | Event date (ISO format). Populated on event pages. |
| `Slots` | Number | Validated at bot startup — bot logs a warning if missing. **Not used for booking logic** (slot assignments tracked in PostgreSQL). Add it as Number type to silence the startup warning. |
| `DJs` | Relation | Self-relation within this same database. Links event pages to DJ profile pages. When creating, select the same database as the related database. |
| `Discord ID` | Text (Rich Text) | Stored on DJ profile pages to link Discord accounts. |

**How the dual-purpose pattern works:** Event pages populate `Date`, `Slots`, and `DJs`. DJ profile pages populate `Discord ID`. Each page type simply leaves the other properties empty.

**About `Slots`:** The property is listed in the bot's startup schema validation (`main.py`). If missing, the bot logs a warning to the admin channel but continues running. Booking slots are tracked in the PostgreSQL `slot_locks` table — the Notion `Slots` value is not read by any booking logic.

**About the `DJs` relation:** This is a self-relation. In the Notion UI, when you create a Relation property and are asked "Select a database to relate to", choose the same database you are editing. This allows event pages to link to DJ profile pages within the same database.

### 2.5 Finding Your Database ID

Open the database in Notion. The URL looks like:

```
https://www.notion.so/yourworkspace/32characterhexid?v=viewid
```

The database ID is the 32-character hex string between the last `/` and the `?v=` query parameter.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | — | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | Yes | — | Notion integration secret (starts with `secret_`) |
| `NOTION_DATABASE_ID` | Yes | — | 32-character ID from your Notion database URL |
| `ADMIN_USER_IDS` | Yes | — | Comma-separated Discord user IDs for admin slash commands |
| `LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DB_POOL_MIN` | No | `5` | Minimum asyncpg connection pool size |
| `DB_POOL_MAX` | No | `20` | Maximum asyncpg connection pool size |

**Finding your Discord user ID:** Enable Developer Mode in Discord (**Settings** → **Advanced** → **Developer Mode**), then right-click your username and select **Copy User ID**.

**Finding your Notion database ID:** See [section 2.5](#25-finding-your-database-id) above.

---

## 4. Deployment: Unraid (Primary)

### Step 1: Build the Docker Image

On a machine with Docker installed:

```bash
git clone <your-ravebot-repo>
cd output/ravebot
docker build -t ravebot:latest .

# Tag with a version for releases:
docker tag ravebot:latest ravebot:v1.0.0
```

### Step 2: Transfer the Image to Unraid

**Option A — save/load (no registry required):**

```bash
docker save ravebot:latest | gzip > ravebot.tar.gz
# Copy ravebot.tar.gz to your Unraid server, then on Unraid:
docker load < ravebot.tar.gz
```

**Option B — push to a registry:**

```bash
docker tag ravebot:latest ghcr.io/YOUR_USER/ravebot:latest
docker push ghcr.io/YOUR_USER/ravebot:latest
# On Unraid, set the repository to ghcr.io/YOUR_USER/ravebot:latest
```

### Step 3: Add the Container in Unraid Docker UI

1. In Unraid, go to **Docker** → **Add Container**
2. Set **Name**: `ravebot`
3. Set **Repository**: `ravebot:latest` (or your registry path)
4. Set **Network Type**: `bridge`
5. Leave **Port Mappings** empty — the bot makes no inbound connections

### Step 4: Configure Environment Variables

In the Add Container form, click **"Add another Path, Port, Variable, Label or Device"** → **Variable** for each:

| Key | Example Value |
|---|---|
| `DISCORD_BOT_TOKEN` | `your_bot_token` |
| `POSTGRES_URL` | `postgresql://ravebot:pass@192.168.1.100:5432/ravebot` |
| `NOTION_API_KEY` | `secret_abc123...` |
| `NOTION_DATABASE_ID` | `your32charnotionid` |
| `ADMIN_USER_IDS` | `123456789012345678` |
| `LOG_LEVEL` | `INFO` |

Use your Unraid server's internal IP (e.g. `192.168.1.x`) for `POSTGRES_URL`, not `localhost` — containers on bridge networks cannot use `localhost` to reach other containers.

### Step 5: Start the Container

Click **Apply**. Check container logs in Unraid (**Docker** → click the container icon → **Logs**) for the startup sequence:

```
Migrations complete.
asyncpg pool opened successfully.
Bot ready
```

If you see these three lines, the bot is running correctly.

---

## 5. Deployment: Docker Compose

For deployments using Docker Compose (e.g. on a VPS or home server), use the following compose file that includes both the bot and its PostgreSQL database:

```yaml
version: "3.9"
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: ravebot
      POSTGRES_PASSWORD: ravebot
      POSTGRES_DB: ravebot
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ravebot"]
      interval: 10s
      timeout: 5s
      retries: 5

  bot:
    image: ravebot:latest
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    env_file:
      - .env
    environment:
      POSTGRES_URL: postgresql://ravebot:ravebot@postgres:5432/ravebot

volumes:
  postgres_data:
```

Create a `.env` file in the same directory (fill in your values):

```env
DISCORD_BOT_TOKEN=your_bot_token_here
NOTION_API_KEY=secret_your_notion_key
NOTION_DATABASE_ID=your_notion_database_id
ADMIN_USER_IDS=your_discord_user_id
LOG_LEVEL=INFO
```

Start:

```bash
docker compose up -d
```

Stop:

```bash
docker compose down
```

> **Note:** The `POSTGRES_URL` in the compose file above uses the service name `postgres` as the hostname — Docker Compose networking resolves this automatically. Do not copy this value into your `.env` when using the Unraid or standalone Docker approach.

---

## 6. Deployment: Podman

Podman is a drop-in Docker replacement. All `docker` commands in this guide work identically with `podman`. Either use:

```bash
alias docker=podman
```

or substitute `podman` for `docker` in every command.

**Rootless Podman note:** When running Podman rootless and your PostgreSQL is on the same host (not in a container), use `host.containers.internal` instead of `localhost` in `POSTGRES_URL`:

```env
POSTGRES_URL=postgresql://ravebot:ravebot@host.containers.internal:5432/ravebot
```

---

## 7. First-Run Checklist

After the bot comes online:

- [ ] Verify the bot appears in your server's member list as online

**Required — run these before the bot is useful:**

- [ ] `/config set dj-channel #your-channel` — where the lineup post is published
- [ ] `/config set log-channel #your-channel` — where admin alerts and DM transcripts go
- [ ] Verify the bot has **Send Messages** and **Embed Links** permissions in both channels
- [ ] Check the admin log channel for any Notion schema warnings (missing properties)

**Optional tuning (defaults shown):**

- [ ] `/config set slots-per-event 6` — number of DJ slots per auto-created event
- [ ] `/config set event-interval-days 7` — days between auto-created events
- [ ] `/config set lookahead-count 6` — how many future events to maintain in Notion
- [ ] `/config set start-time 20:00` — default event start time (HH:MM UTC)
- [ ] `/config set slot-duration 60` — slot length in minutes
- [ ] `/config set outreach-days-before 14` — how far before an event to begin outreach
- [ ] `/config set outreach-cooldown-hours 12` — minimum hours between contacts per DJ
- [ ] `/config set outreach-max` — cap on DJs contacted per outreach cycle (default: unlimited)
- [ ] `/config set event-schedule` — cron expression for future automated event scheduling
- [ ] `/config show` — review all current settings at any time

> **Permissions note:** All `/config`, `/admin`, `/intervene`, `/status`, `/list-djs`, and `/add-event` commands are restricted to the Discord user IDs listed in `ADMIN_USER_IDS`. If a command shows "You don't have permission", verify your user ID is in the env var.

---

## 8. Troubleshooting

### Bot exits immediately with "Missing required environment variables"

One or more required env vars are not set. The error message lists all missing variables. Check your `.env` file or Unraid container variables against the [Environment Variables](#3-environment-variables) table.

### "Database migration failed — is PostgreSQL reachable?"

The bot cannot connect to PostgreSQL at startup. Check:
- `POSTGRES_URL` is correct (host, port, database name, credentials)
- The PostgreSQL service is running
- Network connectivity between the bot container and PostgreSQL (use the internal IP on Unraid, not `localhost`)

### "Failed to connect to PostgreSQL — cannot start bot"

Same as above — the asyncpg pool could not be opened. Fix the connection string and restart.

### Admin log channel shows a Notion schema warning on startup

The startup validation found a missing database property. The most common cause is a missing `Slots` (Number) property. Add the missing property to your Notion database (see [section 2.4](#24-required-database-properties)) and restart the bot.

### "Could not fetch Notion database schema"

Check:
- `NOTION_API_KEY` is correct (starts with `secret_`)
- `NOTION_DATABASE_ID` is the correct 32-character ID from the database URL
- The integration has been shared with the database (database → `...` → **Add connections**)

### Admin log channel shows no messages

The log channel may not be configured yet. Run `/config set log-channel #your-channel` as an admin user. Until a log channel is set, errors are written to container stdout only.
