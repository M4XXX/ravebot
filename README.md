# RaveBot

Discord DJ booking bot for recurring online rave events. Automates DJ discovery, onboarding, self-service slot booking, proactive outreach for unfilled slots, and admin oversight — all via Discord slash commands and button-driven DM flows, with Notion as the event database.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Environment Variables](#environment-variables)
3. [Notion Setup](#notion-setup)
4. [Production: Unraid Deployment](#production-unraid-deployment)
5. [First-Run Checklist](#first-run-checklist)
6. [Local Development](#local-development)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

Before deploying RaveBot you need:

- **Discord Application** — a bot account with a token from [discord.com/developers/applications](https://discord.com/developers/applications). Enable the `Server Members Intent` under the Bot settings.
- **Notion Integration** — a Notion internal integration with read/write access to your event database. Created at [notion.so/my-integrations](https://www.notion.so/my-integrations).
- **Notion Database** — a Notion database shared with your integration. See [Notion Setup](#notion-setup) for the required schema.
- **PostgreSQL** — a PostgreSQL instance reachable by the bot container. On Unraid this is typically a separate PostgreSQL Docker container or a Community Applications install.
- **Docker host** — Unraid (or any host with Docker). The bot runs as a single outbound-only Docker container with no open ports.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | Yes | Notion integration secret (starts with `secret_`) |
| `NOTION_DATABASE_ID` | Yes | 32-character ID from your Notion event database URL |
| `ADMIN_USER_IDS` | Yes | Comma-separated Discord user IDs for admin slash commands |
| `LOG_LEVEL` | No | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |
| `DB_POOL_MIN` | No | Minimum asyncpg pool connections (default: `5`) |
| `DB_POOL_MAX` | No | Maximum asyncpg pool connections (default: `20`) |

**Finding your Discord user ID:** Enable Developer Mode in Discord (Settings → Advanced → Developer Mode), then right-click your username and select "Copy User ID".

**Finding your Notion database ID:** Open the database in Notion. The URL looks like `https://www.notion.so/yourworkspace/DATABASE_ID?v=VIEW_ID`. The database ID is the 32-character hex string between the last `/` and the `?v=`.

---

## Notion Setup

1. **Create an integration:** Go to [notion.so/my-integrations](https://www.notion.so/my-integrations), create a new internal integration, and copy the "Internal Integration Secret".

2. **Create the event database:** Create a Notion database to store your events and DJ slots.

3. **Share the database with your integration:** Open the database, click the `...` menu → "Add connections" → select your integration.

4. **Required database properties:** RaveBot validates these on startup and alerts the admin log channel if any are missing:

   | Property Name | Type | Purpose |
   |---|---|---|
   | `Name` | Title | Event name |
   | `Date` | Date | Event date and time |
   | `Slots` | Number or Multi-select | Available DJ slot count |

   Additional properties will be required by later features (Stories 3.x). The bot will report missing properties via `ERR_NOTION_WRITE` in the admin log channel.

---

## Production: Unraid Deployment

### Step 1: Build the Docker image

On a machine with Docker:

```bash
git clone <your-ravebot-repo>
cd ravebot
docker build -t ravebot:latest .
```

Transfer the image to your Unraid server:
```bash
docker save ravebot:latest | gzip > ravebot.tar.gz
# Copy ravebot.tar.gz to Unraid, then on Unraid:
docker load < ravebot.tar.gz
```

Alternatively, push to a private registry and pull on Unraid.

### Step 2: Add the container in Unraid Docker UI

1. In Unraid, go to **Docker** → **Add Container**.
2. Set **Name**: `ravebot`
3. Set **Repository**: `ravebot:latest` (or your registry path)
4. Set **Network Type**: `bridge`
5. Leave **Port Mappings** empty — the bot makes no inbound connections.

### Step 3: Configure environment variables

In the Unraid Add Container form, add each variable under **"Add another Path, Port, Variable, Label or Device"** → **Variable**:

| Key | Value |
|---|---|
| `DISCORD_BOT_TOKEN` | Your bot token |
| `POSTGRES_URL` | `postgresql://user:pass@192.168.x.x:5432/ravebot` |
| `NOTION_API_KEY` | `secret_your_notion_key` |
| `NOTION_DATABASE_ID` | Your 32-character database ID |
| `ADMIN_USER_IDS` | Your Discord user ID (e.g. `123456789012345678`) |
| `LOG_LEVEL` | `INFO` |

Use your Unraid server's internal IP for `POSTGRES_URL` (or the Docker container name if PostgreSQL is on the same Docker network).

### Step 4: Start the container

Click **Apply**. The container will:
1. Run Alembic database migrations automatically (`alembic upgrade head`)
2. Open the asyncpg connection pool
3. Connect to Discord and come online
4. Validate the Notion database schema

Check container logs in Unraid (**Docker** → click the container icon → **Logs**) to confirm:
```
Migrations complete.
asyncpg pool opened successfully.
Bot ready
```

---

## First-Run Checklist

After the bot is online, complete these steps in your Discord server:

- [ ] Verify the bot appears online in your server's member list
- [ ] Run `/config set dj-channel #your-dj-channel` to configure the DJ lineup post channel
- [ ] Run `/config set log-channel #your-admin-log-channel` to configure the admin log channel
- [ ] Check the admin log channel — you should see any Notion schema validation messages
- [ ] Confirm the bot has permission to send messages in both configured channels

**Note:** `/config` commands are restricted to user IDs listed in `ADMIN_USER_IDS`. If the command shows "You don't have permission", verify your Discord user ID is in the env var.

---

## Local Development

### Setup

```bash
# Install uv (if not already installed)
pip install uv

# Create venv and install all dependencies (including dev)
cd output/ravebot
uv sync

# Start a local PostgreSQL instance (Docker required)
docker-compose up -d

# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your DISCORD_BOT_TOKEN, NOTION_API_KEY, NOTION_DATABASE_ID, ADMIN_USER_IDS
```

### Running the bot

```bash
uv run python -m bot.main
```

### Running tests

```bash
uv run pytest                    # all tests
uv run pytest -v                 # verbose output
uv run ruff check .              # linting
uv run pyright bot/              # type checking
```

The `docker-compose.yml` provides a PostgreSQL-only container for local development. The bot itself is run natively with `uv` — no Docker build required during development.

---

## Troubleshooting

### Bot exits immediately with "Missing required environment variables"

One or more required env vars are not set. The error message lists all missing variables. Check your `.env` file or Unraid container variables against the [Environment Variables](#environment-variables) table.

### "Database migration failed — is PostgreSQL reachable?"

The bot cannot connect to PostgreSQL at startup. Check:
- `POSTGRES_URL` is correct (host, port, database name, credentials)
- The PostgreSQL container/service is running
- Network connectivity between the bot container and PostgreSQL (use internal IP on Unraid, not `localhost`)

### "Failed to connect to PostgreSQL — cannot start bot"

Same as above — the asyncpg pool could not be opened. Fix the connection string and restart.

### Admin log channel shows ERR_NOTION_WRITE on startup

The Notion schema validation found missing properties. Add the listed properties to your Notion database and restart the bot, or wait for Story 3.x which refines the required property list.

### "Could not fetch Notion database schema"

Check:
- `NOTION_API_KEY` is correct (starts with `secret_`)
- `NOTION_DATABASE_ID` is the correct 32-character ID
- The integration has been shared with the database in Notion (database → `...` → Add connections)

### Admin log channel shows no messages

The log channel may not be configured yet. Run `/config set log-channel #your-channel` as an admin user. The bot posts to the channel configured in the database — if no channel is set, errors are written to container stdout only.
