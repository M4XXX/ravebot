# RaveBot - User Guide

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

- **Discord Application** - a bot account with a token from [discord.com/developers/applications](https://discord.com/developers/applications).
  > **Critical:** Under **Bot** → **Privileged Gateway Intents**, enable **Server Members Intent**. Without it, `on_member_update` never fires and DJ onboarding never triggers automatically.
- **Notion account** - a Notion workspace with an internal integration. Created at [notion.so/my-integrations](https://www.notion.so/my-integrations).
- **PostgreSQL 16** - a PostgreSQL instance reachable from the bot container. On Unraid this is typically a separate PostgreSQL Docker container.
- **Docker** (or Podman) on your host machine.

---

## 2. Notion Setup

RaveBot keeps its events, slots and DJs in three Notion databases. You do not build them yourself: you give the bot one empty page, and on its first start the bot creates the databases and views under that page.

### 2.1 Create an Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and click **New integration**.
2. Name it (e.g. "RaveBot"), select your workspace and set the type to **Internal**.
3. Under **Capabilities**, enable **Read content**, **Insert content** and **Update content**.
4. Save, then copy the **Internal Integration Secret**. This is your `NOTION_API_KEY`.

### 2.2 Create and Share the Parent Page

1. Create one new, empty page in your workspace (e.g. "RaveBot").
2. Open the page menu (`...` top right) → **Connections** → add your integration.

The bot can only see pages that are shared with the integration. If this step is missing, the bot stops at startup with: *"Cannot use the Notion parent page: share the page with your integration (page menu > Connections) and check NOTION_API_KEY and NOTION_PARENT_PAGE_ID."*

### 2.3 Find the Parent Page ID

Open the page in Notion (or **Share** → **Copy link**). The URL looks like:

```
https://www.notion.so/yourworkspace/RaveBot-0123456789abcdef0123456789abcdef
```

The page ID is the 32-character hex string at the end of the URL (after the last `-`, before any `?`). Set it as `NOTION_PARENT_PAGE_ID`.

### 2.4 What the Bot Creates

On the first start the bot creates three databases under the parent page:

**Events** - one row per event

| Property | Type | Notes |
|---|---|---|
| `ID` | Title | Unique event ID, format `YYYY-MM-DD <name>`, e.g. `2026-11-14 Rave Night` |
| `Date` | Date | Two events may share a date |
| `Status` | Select | Planning not started, Planning stage, Ready, Done, Canceled |
| `Genre` | Text | Optional |
| `Notes` | Text | Optional |
| `Slots` | Number | Number of DJ slots |
| `Start` | Text | Start of slot 1, `HH:MM` (24 h) |
| `Slot length` | Number | Minutes per slot |

**Slots** - one row per slot of an event

| Property | Type | Notes |
|---|---|---|
| `Event` | Relation → Events | The event this slot belongs to |
| `Slot number` | Number | 1 to `Slots` |
| `DJ` | Relation → DJs | Empty = open slot |
| `Time` | Formula | Event start + (slot number − 1) × slot length |
| `Slot status` | Formula | Open or Booked |

The Slots database also has a few helper columns (`Name`, `Event start`, `Event slot length`, `Event date`) that the formulas and rollups need. Leave them alone.

**DJs** - one row per DJ

| Property | Type | Notes |
|---|---|---|
| `DJ name` | Title | |
| `Discord name` | Text | Refreshed by the bot when it changes |
| `Discord ID` | Text | Links the row to a Discord account; empty for guest DJs |
| `VRChat name` | Text | Optional |
| `Logo` | Files | Optional |
| `Link` | URL | Primary stream link |
| `Alternative link` | URL | Optional |
| `Events booked` | Rollup | Events where the DJ holds at least one slot |
| `Last booked` | Rollup | Latest event date of the DJ's slots |

It also creates three views:

- **Overview** (Events): sorted by Date, newest first.
- **Lineup** (Slots): grouped by event (newest first), sorted by slot number, showing Slot number, Time and DJ.
- **All DJs** (DJs): sorted by Last booked, showing name, Discord name, VRChat name, links, Events booked and Last booked.

The bot never recreates what already exists. On every start it checks the databases, adds missing properties and leaves your own views, columns and edits in place. The three views are created once; a view you delete later is not re-created, except when the bot creates a fresh set of databases (see 2.7), which gets new views. A DJ gets a row in DJs when they onboard or are booked; if that row was deleted, the bot creates a new one the next time the DJ books or cancels.

### 2.5 Adding an Event by Hand

The bot keeps the configured number of future events itself (see `/config set lookahead-count`). To add one by hand, create a row in **Events** and fill in:

- `ID` - `YYYY-MM-DD <name>`, e.g. `2026-12-31 New Year Rave`. Must be unique.
- `Date`
- `Slots` - e.g. `6`
- `Start` - `HH:MM`, e.g. `20:00`
- `Slot length` - minutes, e.g. `60`

On the next poll (every five minutes) the bot creates the Slots rows for the event. Later changes to `Slots`, `Start` or `Slot length` follow the rules in 2.6.

### 2.6 Editing Bookings in Notion

Notion is the source of truth for who plays which slot. The bot picks up hand edits on its next poll (every five minutes):

- **Book by hand:** pick a DJ in the `DJ` cell of an open slot. The bot treats it as a booking: the slot leaves the booking menu and the DJ gets a DM.
- **Free a slot by hand:** clear the `DJ` cell. The bot frees the slot like a cancellation (on the next poll; a slot booked in the last two minutes is left alone) and the DJ gets a DM.
- **Replace a DJ:** clear the cell first, wait until the slot is freed, then enter the new DJ. Overwriting a booked DJ directly is reverted.
- **Conflicts:** a different DJ entered on a booked slot, or several DJs in one cell, is reverted to the bot's booking and logged in the admin log channel.
- **Guest DJs:** a DJ row without a `Discord ID` can be booked by hand on an open slot. It gets no DM; the slot just shows the name. A guest entered over a booked DJ is reverted like any other conflict.
- **Start and Slot length:** a change is applied while no slot of the event has a DJ. Once a slot has a DJ, the bot writes the previous values back on the next poll and logs it in the admin log channel.
- **Slots:** raising the number adds empty slot rows. Lowering it removes the empty rows above the new number; a row above it that still has a DJ is kept and logged once in the admin log channel.
- **Status:** the event `Status` is yours. The bot only reads it.

### 2.7 If Databases Were Deleted

If one of the bot's databases is deleted or moved to the trash, the bot creates a fresh set of all three databases and their views under the parent page on its next start and logs a warning. Data in the old databases stays where it is; the bot does not copy it over. DJ rows are created again in the new DJs database when DJs book.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | - | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | - | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | Yes | - | Internal Integration Secret of your Notion integration (see [section 2.1](#21-create-an-integration)) |
| `NOTION_PARENT_PAGE_ID` | Yes | - | 32-character ID of an empty Notion page shared with the integration; the bot creates its databases under it |
| `ADMIN_USER_IDS` | Yes | - | Comma-separated Discord user IDs for admin slash commands |
| `LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DB_POOL_MIN` | No | `5` | Minimum asyncpg connection pool size |
| `DB_POOL_MAX` | No | `20` | Maximum asyncpg connection pool size |

**Finding your Discord user ID:** Enable Developer Mode in Discord (**Settings** → **Advanced** → **Developer Mode**), then right-click your username and select **Copy User ID**.

**Finding your Notion parent page ID:** See [section 2.3](#23-find-the-parent-page-id) above.

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

**Option A - save/load (no registry required):**

```bash
docker save ravebot:latest | gzip > ravebot.tar.gz
# Copy ravebot.tar.gz to your Unraid server, then on Unraid:
docker load < ravebot.tar.gz
```

**Option B - push to a registry:**

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
5. Leave **Port Mappings** empty - the bot makes no inbound connections

### Step 4: Configure Environment Variables

In the Add Container form, click **"Add another Path, Port, Variable, Label or Device"** → **Variable** for each:

| Key | Example Value |
|---|---|
| `DISCORD_BOT_TOKEN` | `your_bot_token` |
| `POSTGRES_URL` | `postgresql://ravebot:pass@192.168.1.100:5432/ravebot` |
| `NOTION_API_KEY` | `your_notion_integration_secret` |
| `NOTION_PARENT_PAGE_ID` | `your_notion_parent_page_id` |
| `ADMIN_USER_IDS` | `123456789012345678` |
| `LOG_LEVEL` | `INFO` |

Use your Unraid server's internal IP (e.g. `192.168.1.x`) for `POSTGRES_URL`, not `localhost` - containers on bridge networks cannot use `localhost` to reach other containers.

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
NOTION_API_KEY=your_notion_integration_secret
NOTION_PARENT_PAGE_ID=your_notion_parent_page_id
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

> **Note:** The `POSTGRES_URL` in the compose file above uses the service name `postgres` as the hostname - Docker Compose networking resolves this automatically. Do not copy this value into your `.env` when using the Unraid or standalone Docker approach.

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

**Required - run these before the bot is useful:**

- [ ] `/config set dj-channel #your-channel` - where the lineup post is published
- [ ] `/config set log-channel #your-channel` - where admin alerts and DM transcripts go
- [ ] Verify the bot has **Send Messages** and **Embed Links** permissions in both channels
- [ ] Check that the Events, Slots and DJs databases appeared under your Notion parent page

**Optional tuning (defaults shown):**

- [ ] `/config set slots-per-event 6` - number of DJ slots per auto-created event
- [ ] `/config set event-interval-days 7` - days between auto-created events
- [ ] `/config set lookahead-count 6` - how many future events to maintain in Notion
- [ ] `/config set start-time 20:00` - default event start time (HH:MM UTC)
- [ ] `/config set slot-duration 60` - slot length in minutes
- [ ] `/config set outreach true` - turn automatic invite DMs on or off (default: on)
- [ ] `/config set outreach-days-before 14` - how far before an event to begin outreach
- [ ] `/config set outreach-cooldown-hours 12` - minimum hours between contacts per DJ
- [ ] `/config set outreach-max` - cap on DJs contacted per outreach cycle (default: unlimited)
- [ ] `/config set event-schedule` - cron expression for future automated event scheduling
- [ ] `/config show` - review all current settings at any time, including the `Automatic outreach` row

### 7.1 Lineup posts and booking button

- Whenever the lineup of the upcoming event changes, the bot posts a new `Lineup Update` message in the DJ channel. Upcoming event means the earliest event whose date is today or later and whose status is not Done or Canceled. Older posts stay as they are.
- Post format: line 1 is `Event Saturday 14.11.2026`, followed by one line per slot, for example `Slot 1  20:00  DJ Felix` or `Slot 2  21:00  OPEN SLOT`. Times are UTC.
- Every post has a `Book a slot` button. A DJ who presses it gets the booking flow in DMs. If DMs are blocked, the bot says so privately.
- `/lineup post` posts the upcoming event again. `/lineup post event:<event id>` posts any future event.
- `/config set outreach false` stops only the proactive invite DMs. Booking menus, reminders and all other DMs keep working. Use `true` to resume.

> **Permissions note:** All `/config`, `/admin`, `/intervene`, `/status`, `/list-djs`, and `/add-event` commands are restricted to the Discord user IDs listed in `ADMIN_USER_IDS`. If a command shows "You don't have permission", verify your user ID is in the env var.

---

## 8. Troubleshooting

### Bot exits immediately with "Missing required environment variables"

One or more required env vars are not set. The error message lists all missing variables. Check your `.env` file or Unraid container variables against the [Environment Variables](#3-environment-variables) table.

### "Database migration failed - is PostgreSQL reachable?"

The bot cannot connect to PostgreSQL at startup. Check:
- `POSTGRES_URL` is correct (host, port, database name, credentials)
- The PostgreSQL service is running
- Network connectivity between the bot container and PostgreSQL (use the internal IP on Unraid, not `localhost`)

### "Failed to connect to PostgreSQL - cannot start bot"

Same as above - the asyncpg pool could not be opened. Fix the connection string and restart.

### Bot exits with "Cannot use the Notion parent page"

At startup the bot could not create or read its Notion databases. Check:
- The parent page is shared with the integration (page menu → **Connections**)
- `NOTION_API_KEY` is the Internal Integration Secret of that integration
- `NOTION_PARENT_PAGE_ID` is the 32-character ID at the end of the page URL (see [section 2.3](#23-find-the-parent-page-id))
- The integration has the Read, Insert and Update content capabilities
- The Notion API is reachable from the bot container

The text after "Cause:" names the failing request. Fix the setting and restart the bot.

### Bot exits at startup and the cause names a property with the wrong type

Someone changed the type of a property the bot needs (e.g. `Slots` from Number to Text). The bot never changes a property type back by itself. Change the type back to the one listed in [section 2.4](#24-what-the-bot-creates) and restart the bot.

### A slot I edited in Notion went back to the old DJ

The bot reverts a hand entry on a slot it has booked for another DJ. Clear the `DJ` cell first, wait for the next poll, then enter the new DJ (see [section 2.6](#26-editing-bookings-in-notion)).

### Admin log channel shows no messages

The log channel may not be configured yet. Run `/config set log-channel #your-channel` as an admin user. Until a log channel is set, errors are written to container stdout only.
