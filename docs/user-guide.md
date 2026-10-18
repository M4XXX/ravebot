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
- **Two server roles** — `DJ` (members who play; the name is matched case-insensitively) and `Active Booking` (the bot gives it to DJs in the booking pool). Drag the bot's own role **above both** in Server Settings → Roles, otherwise it cannot assign them. The bot also needs the **Manage Roles** permission.

---

## 2. Notion Setup

RaveBot uses a **single Notion database** for both event pages and DJ profile pages. The same database serves both purposes — unused properties are simply left blank per page type.

### 2.1 Create an Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **New integration**
3. Give it a name (e.g. "RaveBot"), select your workspace, set type to **Internal**
4. Click **Submit**, then copy the **Internal Integration Secret** (starts with `secret_`)

### 2.2 Create the Events + DJs Database

Create a new full-page database in Notion for your rave event pages. DJ profile pages can live in the same database (the default) or in a separate DJ database (recommended, see "Option B" in 2.4).

### 2.3 Share the Database with Your Integration

Open the database page in Notion, click the `...` menu (top right), select **Add connections**, and choose your integration. With a separate DJ database, share that one too.

### 2.4 Required Database Properties

Add the following properties to your database:

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | Auto-created on every Notion database — already present |
| `Date` | Date | Event date (ISO format). Populated on event pages. |
| `Slots` | Number | Number of DJ slots of this event. When empty, `/config set slots-per-event` (default 6) applies; an explicit `0` means the event has no DJ slots. The bot writes it on events it creates. Which DJ holds which slot is tracked in PostgreSQL. |
| `DJs` | Relation | Self-relation within this same database. Links event pages to DJ profile pages. When creating, select the same database as the related database. |
| `Discord ID` | Text (Rich Text) | Stored on DJ profile pages to link Discord accounts. |

**How the dual-purpose pattern works:** Event pages populate `Date`, `Slots`, and `DJs`. DJ profile pages populate `Discord ID`. Each page type simply leaves the other properties empty. The bot never treats a page with a `Discord ID` or without a `Date` as an event. At startup it logs a warning recommending a separate DJ database.

**Option B — separate DJ database (recommended):** Create a second database for the DJ profile pages with these properties, share it with the integration, and set `NOTION_DJ_DATABASE_ID` to its ID (see 2.5):

| Property | Type | Notes |
|---|---|---|
| `Name` | Title | The DJ name |
| `Discord ID` | Text (Rich Text) | Links the page to the DJ's Discord account |

The events database then needs only `Name`, `Date`, `Slots` and `DJs`, and `DJs` is a relation **to the DJ database** instead of a self-relation. Both databases are checked at startup. Existing DJ pages in the events database are not moved: move them to the DJ database in Notion (or let the bot create new ones at the next booking) and update the `DJs` relation.

**About `Slots`:** The slot count shown and offered to DJs comes from this property when set, otherwise from the `slots-per-event` setting. Which slot each DJ holds lives in the PostgreSQL `slot_locks` table; Notion only lists the linked DJs. All properties are checked at startup, including their types, and a warning is posted when one is missing or has the wrong type.

**Nothing is ever deleted in Notion by the bot.** It only creates pages and adds or removes its own DJ links on event pages; deleting or archiving is refused in the client itself. Removing pages is left to a human.

**About the `DJs` relation:** Without a separate DJ database this is a self-relation. In the Notion UI, when you create a Relation property and are asked "Select a database to relate to", choose the same database you are editing. This allows event pages to link to DJ profile pages within the same database.

### 2.5 Finding Your Database ID

Open the database in Notion. The URL looks like:

```
https://www.notion.so/yourworkspace/32characterhexid?v=viewid
```

The database ID is the 32-character hex string between the last `/` and the `?v=` query parameter.

### 2.6 Using an Existing Event Plan (`NOTION_LAYOUT=tds`)

If your organizers already keep a planning database with one text column per hour, the bot can work in it directly instead of using the layout above. Set `NOTION_LAYOUT=tds` and point `NOTION_DATABASE_ID` at the plan (share it with the integration as in 2.3).

The plan needs:

| Property | Type | Notes |
|---|---|---|
| any title | Title | The event name; the column may be called anything (e.g. `Event`) |
| `Date` | Date | Event date |
| one column per slot | Text | Named by wall-clock hours, midnight written as `24:00`: with `/config set start-time 21:00`, `slot-duration 60` and `slots-per-event 6` these are `21:00 - 22:00`, `22:00 - 23:00`, `23:00 - 24:00`, `24:00 - 01:00`, `01:00 - 02:00`, `02:00 - 03:00` |
| `Status` | Status or Select | Only needed with `NOTION_BOOKABLE_STATUSES` or `NOTION_CANCELLED_STATUSES` |

How the bot behaves in this layout:

- **A slot is free when its column is empty** and no DJ holds it in the bot. A name typed into the column by hand books the slot too, and so does any other text such as a dash or a note: leave a column empty to offer it. Columns outside the slot range (for example open decks after 03:00) are never touched, and `/admin book` accepts only slot numbers 1 to `slots-per-event`. A slot whose column is missing from the plan (say `start-time 20:00` with no `20:00 - 21:00` column) is never offered and is reported at startup.
- **Booking writes the DJ's name into the slot's column** (self-booked or `/admin book`). The column is read again right before writing and must still be empty, even the same name typed in by hand counts as taken; a self-booking also checks that the event is still in a bookable status. If not, the booking is refused and the slot goes back. `/admin book` overwrites only the text the bot wrote for the DJ it displaces.
- **The bot remembers the text it wrote** for every booked slot. Cancelling empties the column only while it still holds exactly that text, so a DJ who renamed their profile meanwhile, or a name an organizer changed, is handled correctly; whatever an organizer changed it to stays.
- **Names typed in by hand stay yours.** `/ravebot-status` shows them as "(typed in the plan)", and `/admin cancel` on such a slot does not clear it: it tells you to clear the column in Notion yourself.
- Notion has no conditional writes: if an organizer types into the very column in the second between the bot reading and writing it, the bot's name wins. The reconcile check does not see this case, so organizers should not type into a column a DJ is booking at that moment.
- **DJ profiles stay in the bot's database.** No DJ pages are created in the plan, and the `Slots`, `DJs` and `Discord ID` properties are not used.
- **`NOTION_BOOKABLE_STATUSES`** (comma-separated) limits self-booking and outreach to events in those statuses, e.g. `Planing-stage,Not started,Requires Host`. Other events still show in the lineup, marked as not open for booking; `/admin book` ignores the status. The first status in the list is set on events the bot creates.
- **`NOTION_CANCELLED_STATUSES`** (comma-separated, e.g. `CANCLED,CANCLED (moved)`) marks nights that were called off: they get no reminders, are left out of the lineup and `/ravebot-status`, do not block `/add-event` for that day, and are skipped when `/admin book` finds two events on one date.
- Status names are compared exactly, including case; names the database's Status property does not offer are reported at startup.
- The reconcile check reports booked slots whose column does not show the text the bot wrote. Names typed in without a booking are how organizers plan and are not reported.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | — | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | Yes | — | Notion integration secret (starts with `secret_`) |
| `NOTION_DATABASE_ID` | Yes | — | 32-character ID from your Notion database URL |
| `NOTION_DJ_DATABASE_ID` | No | — | ID of a separate Notion database for DJ profile pages (classic layout, 2.4 option B). Unset: DJ pages are kept in the events database and a warning is logged at startup |
| `ADMIN_USER_IDS` | Yes | — | Comma-separated Discord user IDs for admin slash commands |
| `GUILD_ID` | No | — | Discord server ID the bot serves. Without it the bot serves the single server it is in, and **none** (with an error in the log) when it is in several. Set it in production. |
| `NOTION_LAYOUT` | No | `classic` | `classic` for the database layout in 2.4, `tds` for an existing event plan with one column per slot (2.6) |
| `NOTION_BOOKABLE_STATUSES` | No | — | Comma-separated status names DJs may book in (2.6); empty means every event is bookable |
| `NOTION_CANCELLED_STATUSES` | No | — | Comma-separated status names that mean a night was called off (2.6) |
| `LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DB_POOL_MIN` | No | `5` | Minimum asyncpg connection pool size |
| `DB_POOL_MAX` | No | `20` | Maximum asyncpg connection pool size |

**Finding your Discord user ID:** Enable Developer Mode in Discord (**Settings** → **Advanced** → **Developer Mode**), then right-click your username and select **Copy User ID**.

**Finding your Notion database ID:** See [section 2.5](#25-finding-your-database-id) above.

**Run exactly one instance.** Two bookings for the same event are written to Notion one after the other by a lock inside the bot process. A second instance would not share that lock and could drop a DJ from an event's lineup, so the bot enforces this: at startup it takes a lock in PostgreSQL, and a second instance against the same database (for example a copy started for testing with the same `.env`) exits with "Another RaveBot instance is already running against this database". Restarting or updating is safe: Unraid, Docker Compose and Podman stop the old container before starting the new one, which releases the lock. If PostgreSQL restarts or the network drops, the bot keeps running and takes the lock again once the database is back; it only stops (with an error exit code) if another instance got the lock in the meantime. After a crash of the bot's container PostgreSQL may keep the old session for a while; until it notices, a restarted container is refused and the restart policy keeps retrying.

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

**The bot starts in passive mode.** Until an organizer runs `/config set active on` it sends no DMs, runs no outreach or reminders, posts no lineup, creates no Notion events and refuses admin commands that would contact DJs or change bookings. Read-only commands and `/config` work, so everything below can be set up first.

After the bot comes online:

- [ ] Verify the bot appears in your server's member list
- [ ] Read the **RaveBot setup check** the bot posts to the log channel (or the console) on startup — it lists missing roles, a wrong role order and missing channel permissions

**Required:**

- [ ] Roles `DJ` and `Active Booking` exist and the bot's role sits **above** both (Server Settings → Roles)
- [ ] `/config set dj-channel #your-channel` — where the lineup post is published
- [ ] `/config set log-channel #your-channel` — admin alerts (and DM transcripts, see below); use a private channel
- [ ] The bot can **View Channel, Send Messages, Embed Links and Read Message History** in both channels (for private channels add the bot's role to the channel)
- [ ] `/config set timezone Europe/Berlin` (any IANA name) and `/config set start-time 21:00` — the first slot starts at this local time all year round. Slots then follow in real time, each exactly one slot duration long, so on the night the clocks change the later slots of that night show the shifted clock time
- [ ] `/config set slot-duration 60` and `/config set slots-per-event 6` if your nights differ

**Optional (defaults shown):**

- [ ] `/config set lookahead-count 0` — automatic event creation is **off** by default; a positive number keeps that many future events in Notion, spaced `event-interval-days` apart. Leave it at 0 if your dates are irregular and create events with `/add-event`
- [ ] `/config set event-interval-days 7`
- [ ] `/config set outreach-days-before 14`, `/config set outreach-cooldown-hours 12`, `/config set outreach-max` (default: unlimited)
- [ ] `/config set dm-log on` — DJ DMs are mirrored into the log channel; `off` keeps them private. Tell your DJs which one you use
- [ ] `/config set event-schedule` — stored for a future scheduler; **not used yet**
- [ ] `/config show` — all settings plus when each background loop last completed

**Going live:**

- [ ] `/admin onboard-preview` — lists members with the DJ role who are not registered yet (sends nothing)
- [ ] `/config set active on`
- [ ] Existing DJs are not contacted automatically (onboarding starts when a member newly gets the DJ role). Invite them one by one with `/admin onboard dj:@name`

> **Permissions note:** All admin commands (`/config`, `/admin`, `/intervene`, `/ravebot-status`, `/list-djs`, `/add-event`) are only visible to members with **Manage Server**, only work inside the server, and are additionally restricted to the Discord user IDs in `ADMIN_USER_IDS`. If a command shows "You don't have permission", verify your user ID is in the env var.

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

### "Another RaveBot instance is already running against this database"

A second bot uses the same PostgreSQL database. Stop the other container (check for an old copy or a test container with the same `.env`). Right after a crash or network outage this can also mean PostgreSQL has not yet noticed the old connection is gone; with `restart: unless-stopped` the bot retries on its own and starts once the old session has ended.

### "Lost the database connection holding the single-instance lock — retrying until PostgreSQL is back"

The connection that holds the instance lock dropped (PostgreSQL restarted or the network failed). The bot keeps running, retries every few seconds (up to once a minute) and logs "Single-instance lock re-acquired." when the database is back. No action is needed unless the database stays unreachable.

### "Another RaveBot instance took the single-instance lock while the database connection was lost — shutting down"

While the database connection was down, a second instance started and took the lock. This bot stops with an error exit code so it never runs next to the other one. Stop whichever instance should not be running.

### Admin log channel shows a Notion schema warning on startup

The startup validation found a missing database property (`Name`, `Date`, `Slots`, `DJs`, `Discord ID`), a property of the wrong type (for example `Slots` as Text instead of Number), or a `DJs` relation that points to another database. The message names the property, the type it has and the type it needs. Fix it in your Notion database (see [section 2.4](#24-required-database-properties), or [2.6](#26-using-an-existing-event-plan-notion_layouttds) for the plan layout, where every slot column must be Text) and restart the bot.

### "Could not fetch Notion database schema"

Check:
- `NOTION_API_KEY` is correct (starts with `secret_`)
- `NOTION_DATABASE_ID` is the correct 32-character ID from the database URL
- The integration has been shared with the database (database → `...` → **Add connections**)

### The bot does not answer DMs and buttons say it isn't taking bookings

It is in passive mode, the default. Run `/config set active on`.

### Log says "Serving no server" / "Bot is in N servers and GUILD_ID is not set"

Set `GUILD_ID` to the server the bot should serve and restart. The bot never picks one of several servers on its own.

### Admin log shows ERR_DATA_MISMATCH

Notion and the booking database disagree for an event (a DJ booked but not linked in Notion, or linked by hand without a booking). The bot only reports this, once per distinct difference, and never fixes it automatically. Compare the event in Notion with `/ravebot-status` and correct it by hand.

### Admin log channel shows no messages

The log channel may not be configured yet. Run `/config set log-channel #your-channel` as an admin user. Until a log channel is set, errors are written to container stdout only.
