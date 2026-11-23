# RaveBot - User Guide

Operator reference for deploying and configuring RaveBot.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Notion Setup (optional)](#2-notion-setup-optional)
3. [Environment Variables](#3-environment-variables)
4. [Deployment: Unraid (Primary)](#4-deployment-unraid-primary)
5. [Deployment: Docker Compose](#5-deployment-docker-compose)
6. [Deployment: Podman](#6-deployment-podman)
7. [First-Run Setup and Commands](#7-first-run-setup-and-commands)
8. [Troubleshooting](#8-troubleshooting)
9. [The Admin Log Channel](#9-the-admin-log-channel)

---

## 1. Prerequisites

Before deploying RaveBot you need:

- **Discord Application** - a bot account with a token from [discord.com/developers/applications](https://discord.com/developers/applications).
  > **Critical:** Under **Bot** → **Privileged Gateway Intents**, enable **Server Members Intent**. Without it, `on_member_update` never fires and DJ onboarding never triggers automatically.
- **Notion account (optional)** - a Notion workspace with an internal integration, created at [notion.so/my-integrations](https://www.notion.so/my-integrations). Without it the bot runs on PostgreSQL alone.
- **PostgreSQL 16** - a PostgreSQL instance reachable from the bot container. It holds all of the bot's data (events, slots, DJs, settings). On Unraid this is typically a separate PostgreSQL Docker container.
- **Docker** (or Podman) on your host machine.

---

## 2. Notion Setup (optional)

PostgreSQL is the source of truth: the bot reads and writes events, slots, DJs and settings only there. Notion is an optional mirror and backup. When you give the bot a Notion integration, it writes every change to Notion in the background and picks up hand edits from there. A slow or unreachable Notion never delays a booking or any other reply.

**Without Notion:** leave `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID` empty. The bot then makes no Notion calls, starts normally, and the admin page hides the Notion cards. Backup and restore on the admin page still work. If you add both variables later, the bot creates the Notion databases and fills them from PostgreSQL.

You do not build the databases yourself: you give the bot one empty page, and on its first start the bot creates the databases and views under that page.

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

On the first start the bot creates four databases under the parent page (Events, Slots and DJs below, plus Bot data, see 2.10):

**Events** - one row per event

| Property | Type | Notes |
|---|---|---|
| `ID` | Title | Unique event ID, format `YYYY-MM-DD <name>`, e.g. `2026-11-14 Rave Night` |
| `Date` | Date | Two events may share a date |
| `Status` | Select | Planning not started, Planning stage, Ready, Done, Canceled |
| `Genre` | Text | Optional, up to 40 characters. A hand edit counts as set by hand |
| `Location` | Text | Optional, up to 80 characters; added automatically to an existing database. A hand edit counts as set by hand |
| `Notes` | Text | Optional, the event note, up to 300 characters |
| `Slots` | Number | Number of DJ slots |
| `Start` | Text | Start of slot 1, `HH:MM` (24 h), local time (see 2.8) |
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
| `Discord name` | Text | Refreshed by the bot when it changes; edits are overwritten |
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

The bot never recreates what already exists. On every start it checks the databases, adds missing properties and leaves your own views, columns and edits in place. The three views are created once; a view you delete later is not re-created, except when the bot creates a fresh set of databases (see 2.7), which gets new views. A DJ gets a row in DJs when they onboard or the DJ is changed; if that row was deleted, the bot reports a warning (see 2.9) and writes it again on a resync.

### 2.5 Adding an Event by Hand

The bot keeps the configured number of future events itself (the number is set in `/setup`, step Events kept ready; scheduling can be turned off there). To add one by hand, create a row in **Events** (or run `/create event`) and fill in:

- `ID` - `YYYY-MM-DD <name>`, e.g. `2026-12-31 New Year Rave`. Must be unique.
- `Date`
- `Slots` - e.g. `6`
- `Start` - `HH:MM` local time, e.g. `20:00`
- `Slot length` - minutes, e.g. `60`

On the next poll (every five minutes) the bot creates the Slots rows for the event. Later changes to `Slots`, `Start` or `Slot length` follow the rules in 2.6.

### 2.6 Editing in Notion by Hand

The bot picks up hand edits on its next poll (every five minutes, only while the bot is active). It checks every edit the same way as the equivalent command, applies it to PostgreSQL and posts an `Applied from Notion` entry in the log channel. What the bot applies:

- **Book by hand:** pick a DJ in the `DJ` cell of an open slot. The slot must be free and the DJ must be in the bot's DJ list. The DJ gets a DM, the lineup updates and the log channel shows the booking.
- **Free a slot by hand:** clear the `DJ` cell. The bot frees the slot like a cancellation and the DJ gets a DM.
- **Replace a DJ:** enter another DJ over a booked one. The bot removes the old DJ (DM) and books the new one.
- **Guest DJs:** a DJ row without a `Discord ID` can be booked by hand. It gets no DM; the slot just shows the name.
- **New DJ:** a new row in DJs passes the same checks as `/dj add` (name, links, unique name). A row that fails them is not applied (see 2.9).
- **New event:** a new row in Events passes the same checks as `/create event` (valid date, unique `ID`, slots, start, slot length). The bot creates its Slots rows and writes a `Bot ID` into the row. Do not change `Bot ID`.
- **Changed values:** `Date`, `Start`, `Slot length`, `Slots`, `Genre`, `Notes`, `Status`, DJ name, links and VRChat name. `Start`, `Slot length` and lowering `Slots` below a booked slot are refused while a slot of the event has a DJ (see 2.9). Setting `Status` to `Canceled` or `Done` in Notion is applied as a plain status change: the bot stops using the event, but it sends no DMs and frees no slots. Use `/cancel event` for the full cancellation.

The DJ logo is written from the bot to Notion only. Changing `Logo` in Notion changes nothing in the bot.

### 2.7 If Databases Were Deleted

If one of the bot's databases is deleted or moved to the trash, or the integration lost access to it, the bot does **not** replace it on its own. It keeps running with booking and the lineup paused, and posts "Notion databases not found" to the admin log channel. Then:

- If the integration only lost access: share the Notion page with it again and restart the bot.
- If the databases were deleted on purpose: open the [admin page](#77-the-admin-page) and press **Create new databases**. After a confirmation the bot creates a fresh set of all four databases and their views right away, no restart needed, and writes everything from PostgreSQL into them. Data in the old databases stays where it is.
- If PostgreSQL itself was lost and the Notion databases still exist: start the bot with the same `NOTION_PARENT_PAGE_ID`. It finds the databases Events, Slots, DJs and Bot data under that page again and rebuilds PostgreSQL from them (see 2.11).

Do not rename or move the databases away from the parent page: the bot finds them again by title.

### 2.8 Time Zone

All start times are local wall-clock times, not UTC. The default zone is `Europe/Berlin`: an event at `21:00` starts at 21:00 CET in winter and 21:00 CEST in summer, so 21:00 stays 21:00 all year. The `Start` column and the day an event counts as "today" use this zone. The lineup post, booking menus and messages show times as Discord timestamps, so every reader sees the time in their own time zone; Notion and the admin page show the time of this zone as plain text. The zone is stored in the setting `timezone` and is changed in `/setup` (Time zone step: pick a zone or press **Other** and enter a name such as `Europe/Berlin`). Changing it moves the real start of existing events, because their `Start` text is kept as it is.

### 2.9 Conflicts and Warnings

The bot never applies a risky hand edit silently. It posts an entry in the log channel and lists the case on the [admin page](#77-the-admin-page) in the **Notion sync** card.

**Conflict** (`Changed in the bot and in Notion`): you edited something in Notion while the bot had its own change to the same booking, DJ or event that Notion did not have yet. The entry shows both versions. On the admin page choose:

- **Keep Postgres** - the bot's version wins and is written to Notion; your Notion edit is overwritten.
- **Keep Notion** - your Notion edit is applied to the bot after the same checks as a command. If the checks fail, the conflict stays open.

Only that one booking, DJ or event waits; everything else keeps syncing. If nobody decides within 24 hours, the bot's version wins automatically and the log channel says `Postgres kept after 24 h`. While the bot is idle (before `/setup` is finished) conflicts do not expire.

**Warning** (`Notion edit not applied`): the bot did not apply the edit and changed nothing. Typical cases are a hand-made slot page that names another DJ than the one booked in the bot (the bot keeps its booking and overwrites the page), a change to a slot of a past event, a deleted page, a changed event `ID`, `Discord ID` or `Slot number`, a duplicate page, a new row that fails the checks, a `Start` or `Slot length` change while a slot is booked, many records missing at once, or an empty or unreadable Notion. Fix it in Notion (the next poll tries again), or choose **Resync Postgres to Notion** on the admin page to write the bot's version back. A warning does not stop that record from syncing the bot's changes to Notion.

### 2.10 Bot Data Is Read-Only

The fourth database, **Bot data**, is a backup of the bot's own state: settings, open DJ conversations, invite and reminder history, setup drafts and similar records, one row per record with a JSON payload. It is for restoring only. Hand edits there are ignored and overwritten at the next sync, and a row you add is moved to the trash. Admin accounts, secrets and logs are never written to Notion.

### 2.11 Backup, Restore, Rebuild and Resync

All four live on the [admin page](#77-the-admin-page). Each destructive action asks for a confirmation first.

- **Download a backup:** one file (`ravebot-<date>.ravebot.json.gz`) with the whole database: events, slots, DJs with logos, settings and the admins added on the page. It works with or without Notion.
- **Restore a backup:** upload such a file. The bot saves a safety backup of the current data first (in the folder `BACKUP_DIR`, default `/data/backups`, the newest 10 are kept; map that folder to a host folder or volume, or the safety backups are lost when the container is recreated), then replaces the whole database in one step. Bookings and edits are refused while it runs, and a file from a newer bot version, or one that unpacks to more than 1 GB, is refused. A backup made with an older bot version is accepted: the bot moves the database to that version, loads the file and migrates the data to the current version (the message says so). If the migration fails, the data from before the restore is put back; in the rare case that this fails too, the bot stays paused, the safety backup is in the backup folder, and a restart repeats the migration. With Notion set up, everything is written to Notion again afterwards. Changes made since the backup are lost, also in Notion: on the first sync after the restore, events, DJs and slots that were added in Notion after the backup are moved to the Notion trash and listed as warnings (`... was made in Notion after the restored backup`). To keep one, restore its page from the Notion trash; the next sync takes it in as a hand edit.
- **Rebuild from Notion:** for a lost PostgreSQL. The bot saves a safety backup, then replaces events, slots, DJs with logos, settings and Bot data with what Notion holds. Changes still waiting for Notion and open conflicts are dropped. Admins added on the page are not in Notion: the owners from `ADMIN_USER_IDS` keep working, the others come back only from a backup file. If the rebuild finds unusable rows (for example two events with the same `ID`), it changes nothing and lists them.
- **Resync all to Notion:** writes every event, DJ and slot to Notion again. Use it after fixing a Notion problem; hand edits not yet taken over are overwritten.

**First start after updating from a version that kept its data in Notion:** the bot saves a safety backup, imports events, slots and DJs from Notion once, and posts the result in the log channel (`Data imported from Notion`). Until the import works, booking and edits are paused and the Notion sync is off. If it fails, fix the listed rows in Notion and press **Retry the import from Notion** on the admin page, or restart the bot. If the bot cannot find its Notion databases on this first start (for example the page is no longer shared with the integration), it stays paused too: share the page again and press **Retry the import from Notion**, or create new databases on the admin page. If PostgreSQL is empty and Notion holds events, the bot rebuilds from Notion instead. A booked slot whose DJ page cannot be read keeps the booking the bot already had; if there is none, the import stops and lists the slot.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | - | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | - | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | No | - | Internal Integration Secret of your Notion integration (see [section 2.1](#21-create-an-integration)). Set together with `NOTION_PARENT_PAGE_ID` to turn the Notion mirror on; without both the bot runs on PostgreSQL alone |
| `NOTION_PARENT_PAGE_ID` | No | - | 32-character ID of an empty Notion page shared with the integration; the bot creates its databases under it |
| `ADMIN_USER_IDS` | Yes | - | Comma-separated Discord user IDs of the owners: they are always admins and cannot be removed on the admin page |
| `ADMIN_PAGE_PASSWORD` | No | - | Password of the admin page. While empty, the page is off. Choose a long random value |
| `ADMIN_PAGE_PORT` | No | `8080` | Port of the admin page inside the container (1 to 65535) |
| `LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `BACKUP_DIR` | No | `/data/backups` | Folder for the safety backups saved before a restore, a rebuild or the one-time import from Notion (the newest 10 are kept). Map it to a host folder or volume so the files survive a container recreate. The container makes the folder writable for its own user (ID 1000) at every start; started with `--user 1000` the folder must already be writable for that user |
| `DB_POOL_MIN` | No | `5` | Minimum asyncpg connection pool size |
| `DB_POOL_MAX` | No | `20` | Maximum asyncpg connection pool size |

The key `GUILD_ID` is not read by the bot. If it is still in your `.env` or container settings, you can delete it.

**Finding your Discord user ID:** Enable Developer Mode in Discord (**Settings** → **Advanced** → **Developer Mode**), then right-click your username and select **Copy User ID**.

**Finding your Notion parent page ID (only with Notion):** See [section 2.3](#23-find-the-parent-page-id) above.

---

## 4. Deployment: Unraid (Primary)

### Step 1: The Docker image

Every release is published as a ready-made image on GitHub Container Registry: `ghcr.io/m4xxx/ravebot`. You do not build anything yourself.

| Tag | Meaning |
|---|---|
| `latest` | The newest release (not pre-releases) |
| `1.1.0` | Exactly that release |
| `1.1` | The newest patch release of that minor version |

> The package must be **public** so Unraid can pull it without a login. The repository owner sets this once: *GitHub → profile → Packages → ravebot → Package settings → Change visibility → Public*.

To build the image yourself instead (for example from a fork), see [Docker build in the developer guide](developer-guide.md#5-manual-docker-build--release).

### Step 2: Install the template (optional shortcut)

The repository contains an Unraid template with all variables. On the Unraid terminal:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/ravebot.xml \
  https://raw.githubusercontent.com/M4XXX/ravebot/main/unraid/ravebot.xml
```

Then **Docker** → **Add Container** → **Template** → **RaveBot** (under *User templates*) and continue with step 4. Skip this step if you prefer to add the container by hand.

### Step 3: Add the Container in Unraid Docker UI

1. In Unraid, go to **Docker** → **Add Container**
2. Set **Name**: `ravebot`
3. Set **Repository**: `ghcr.io/m4xxx/ravebot:latest` (or a fixed version such as `ghcr.io/m4xxx/ravebot:1.1.0`)
4. Set **Network Type**: `bridge`
5. Port mappings: leave them empty if you do not use the admin page. To use it, add a **Port** with container port `8080` (the value of `ADMIN_PAGE_PORT`; change it only together with that variable) and a free host port (for example `8080` or `8095`), and set the variable `ADMIN_PAGE_PASSWORD` in step 4 (see [section 7.7](#77-the-admin-page))

### Step 4: Configure Environment Variables

In the Add Container form, click **"Add another Path, Port, Variable, Label or Device"** → **Variable** for each:

| Key | Example Value |
|---|---|
| `DISCORD_BOT_TOKEN` | `your_bot_token` |
| `POSTGRES_URL` | `postgresql://ravebot:pass@192.168.1.100:5432/ravebot` |
| `NOTION_API_KEY` | `your_notion_integration_secret` (optional, with the next row) |
| `NOTION_PARENT_PAGE_ID` | `your_notion_parent_page_id` (optional) |
| `ADMIN_USER_IDS` | `123456789012345678` |
| `ADMIN_PAGE_PASSWORD` | `a_long_random_password` (only for the admin page) |
| `LOG_LEVEL` | `INFO` |

Use your Unraid server's internal IP (e.g. `192.168.1.x`) for `POSTGRES_URL`, not `localhost` - containers on bridge networks cannot use `localhost` to reach other containers.

### Step 5: Start the Container

Click **Apply**. Unraid pulls the image and starts the container. Check container logs in Unraid (**Docker** → click the container icon → **Logs**) for the startup sequence:

```
Migrations complete.
asyncpg pool opened successfully.
Bot ready
```

If you see these three lines, the bot is running correctly.

### Updating the container

- **Repository `...:latest`:** on the **Docker** tab click **Check for Updates**, then apply the update of `ravebot`. Unraid pulls the newest release and restarts the container. The database updates itself at the start.
- **Repository with a version (`...:1.1.0`):** change the version in the container settings and click **Apply**.
- Read the [release notes](https://github.com/M4XXX/ravebot/releases) first. Your variables and the data in PostgreSQL and Notion stay as they are. The first start after the update to PostgreSQL as the source of truth imports your Notion data once (see 2.11).

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
    image: ghcr.io/m4xxx/ravebot:latest
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

## 7. First-Run Setup and Commands

### 7.1 The idle bot

After a fresh deploy the bot is idle until an admin finishes `/setup`. While idle it does the following:

- It sends no lineup posts, invites, reminders or onboarding DMs, and the Notion poll and sync do not run.
- Buttons and menus under old messages answer privately: "I can't answer right now. Please try again later."
- A DJ who writes the bot a DM gets the same sentence. Nothing is logged.
- Admin commands other than `/setup` and `/config show` answer privately: "The bot is not active yet. Run /setup to finish the setup."
- When the bot starts idle, it sends every admin (`ADMIN_USER_IDS` and added admins) a DM: "RaveBot is not set up yet. Run /setup in the channel where you want to do it." The DM goes out once per start. If an admin has DMs from server members turned off, the bot only writes a warning to the container log.

A server that already has both a DJ channel and a log channel stored (set up before `/setup` existed) counts as active, so an update does not interrupt it.

### 7.2 The `/setup` walkthrough

Run `/setup` in the channel where you want to set up (admins only). The bot posts one wizard message and edits it step by step. The wizard survives a restart; running `/setup` again while it is open links to the open message.

1. **Welcome and checks.** Shows whether the Discord connection and the database work and, when Notion is set up, the four Notion databases. The bot stays idle until you press **Activate** at the end.
2. **DJ channel.** Pick the channel for the Lineup Update and the `Book a slot` button. The bot checks it can send messages and embeds there.
3. **Log channel.** Pick the channel for bookings, new DJs, problems and DJ messages. Only organizers should see it. The same permission check runs.
4. **Roles.** Pick the DJ role (the welcome DM goes to members who get it) and the invites role (the bot gives it to DJs who want automatic invites). The two roles must differ, `@everyone` and integration roles are refused, and the bot's own role must be above both. A button can create the invites role (default name `Active Booking`). Any role selection needs the Manage Roles permission: the bot refuses roles it cannot manage.
5. **Event settings, one step each.** Every step has a dropdown with common values and an **Other** button for your own value.
   - **Slots per event** (1 to 100), **Start time** (local, `HH:MM`) and **Slot length** (minutes, 1 to 1440). They are the defaults for every event the bot creates, scheduled or by hand.
   - **Scheduled events** (On or Off). Turn it off if you only create events by hand with `/create event`. The bot is then fully set up and creates no events by itself, and the next three steps are skipped.
   - **Event interval** (days between two scheduled events, 1 to 365).
   - **First event.** Pick the date of the first scheduled event, for example next Saturday; the next ones follow every interval days. Choose "As soon as the interval allows" to start one interval after the day of setup. The date must be today or later.
   - **Events kept ready** (future scheduled events the bot keeps, 1 to 52).
   - **Default genre** and **Default location** (optional). Scheduled events get them when no rule sets a value.
   - **Rules** (optional). Add rules such as "every 2nd event: location Hall B", see [7.3](#73-commands) and [7.8](#78-genre-location-notes-and-rules).
   - **Time zone** (default `Europe/Berlin`).
6. **Automatic invites.** Turn invites on or off, how many days before an event the bot starts asking, and the hours it waits between two invites to the same DJ.
7. **DJ Management channel (optional).** Pick a channel for the lineup of each fully booked event. Use a staff-only channel. Skip it or press **Clear** to keep the feature off. If fully booked events already exist, the wizard asks whether to post all of them or only new ones, see [7.9](#79-dj-management-lineup-message).
8. **Booking rules.** Two switches and two numbers: the slot limit per DJ and event, and the cancellation cutoff. Press a toggle button to switch a rule on or off and **Change** to set the numbers. See [7.10](#710-booking-rules).
9. **Summary.** Lists everything (including the DJ Management channel and the booking rules). **Activate** writes the settings, switches the bot on, creates the missing future events and posts the first Lineup Update (it reports how many events it created and whether it posted). The channels and roles must be chosen first.

Nothing is stored until Activate. Defaults: 6 slots, 20:00, 60 minutes, scheduling on, every 7 days, first event as soon as the interval allows, 6 events ahead, invites on, 14 days before, 12 hours wait, booking rules on (2 slots per DJ and event, cancellation closes 60 minutes before the start).

**Changing settings later.** Run `/setup` again. An active bot answers with a **Change settings** button. The wizard then starts from the stored values and ends with **Save** instead of Activate. Save only stores the values: it creates no events and posts nothing. Two warnings can appear in the summary: a changed time zone moves the real times of existing events, and a changed invites role does not move members to the new role.

**Roles without the wizard.** If no role was stored (older setup), the bot uses a role named `DJ` for onboarding and `Active Booking` for invites. Once `/setup` stores roles, the stored ones are used.

Finally, with Notion set up, check that the Events, Slots, DJs and Bot data databases appeared under your Notion parent page, and use `/config show` to review all settings and the bot status (`active` or `idle`).

### 7.3 Commands

All commands are for admins: the user IDs in `ADMIN_USER_IDS` plus the admins added on the [admin page](#77-the-admin-page). Added admins can use every admin command, including `/setup`. The commands work on the server only, not in DMs. Others see "You don't have permission to use this command."

| Command | What it does |
|---|---|
| `/setup` | Guided setup; re-run it with **Change settings** to edit settings. Works while idle. |
| `/config show` | Shows every setting (stored value or default) and the bot status. Works while idle. |
| `/create event date:<YYYY-MM-DD> [slots] [start] [slot_minutes] [genre] [location] [note]` | Creates an event by hand. `slots`, `start` (`HH:MM`) and `slot_minutes` are optional and default to the setup values, so each event can differ. `genre`, `location` and `note` are optional; rules never apply to hand-created events. Works with scheduling on or off; scheduled events ignore it: it neither counts toward the events kept ready nor moves the schedule. |
| `/edit event event:<event> [genre] [location] [note] [clear]` | Sets the genre, location or note of an event. `clear` removes one value: a scheduled event then goes back to the rules, a manual event gets an empty field. |
| `/schedule show` | Shows the defaults, the rules and the next 10 events with their genre and location (`set by hand` marks hand values). |
| `/schedule default field:<genre/location> [value]` | Sets the default; no value clears it. |
| `/schedule rule add field value every start` | Adds a rule: every `every`-th scheduled event (1 to 52), counted from the event picked in `start`, gets the value. |
| `/schedule rule edit rule [value] [every] [start]`, `/schedule rule remove rule` | Changes or removes a rule. |
| `/schedule apply` | Lists upcoming events that differ from the rules and lets you choose (see 7.8). |
| `/post lineup update [event:<event id>]` | Posts the lineup of the upcoming event, or of a chosen future event, in the DJ channel. |
| `/book dj slot event:<event> slot:<number> dj:<member>` or `guest:<name>` | Books a DJ into a slot without asking for confirmation. Use `dj` for a server member, or `guest` to pick a DJ from the DJ list by name (also DJs without Discord). A slot that no longer exists (the event was deleted or has fewer slots now) is refused. |
| `/cancel dj slot event:<event> slot:<number> dj:<member>` or `guest:<name>` | Frees a DJ's slot. |
| `/cancel event event:<event>` | Cancels an upcoming event after you confirm. The event gets the status `Canceled` and its bookings are removed (Notion follows in the background). Booked DJs are told by DM; DJs the bot cannot reach (closed DMs, guests without Discord) are listed in the log channel so you can tell them yourself. If at least one DJ was booked, the DJ channel gets a notice. With scheduling on, one new event is added at the end of the schedule; the canceled date stays empty. |
| `/dj add name:<name> link:<stream link> [backup_link] [vrchat_name] [logo] [discord_user]` | Adds a DJ to the DJ list. Leave `discord_user` empty for a guest who has no Discord account. `logo` is an optional PNG or JPEG up to 5 MB. |
| `/dj edit dj:<name> [name] [link] [backup_link] [vrchat_name] [logo]` | Changes the given fields of a DJ. Send `-` for `backup_link` or `vrchat_name` to remove it. Changes are mirrored to Notion in the background. |
| `/dj remove dj:<name>` | Deletes a DJ after you confirm. Their bookings in upcoming events are cancelled. Past bookings stay. |
| `/list all djs` | Lists all registered DJs and whether they get automatic invites. Guests are marked. |
| `/automatic dj booking dj:<member> enabled:<on/off>` | Adds a DJ to or removes a DJ from the automatic booking pool and syncs the invites role. |
| `/admins add user:<user>`, `/admins remove user:<user>`, `/admins list` | Adds an admin, removes an added admin, or lists all admins. Owners only (see below). |

**Managing admins with `/admins`.** Only the owners (the Discord IDs in `ADMIN_USER_IDS`) can use `/admins add`, `/admins remove` and `/admins list`. The answers are visible only to you. Owners cannot be removed with the command, and bot accounts cannot be added. An admin who is not an owner is told that only owners can manage the admin list; everyone else gets the normal permission message. Like the other admin commands, they work on the server only and need the bot to be set up (not idle). Any Discord user can be chosen; the person does not have to be on the server. While the bot is active, adding and removing are written to the admin log channel with the name of the owner who did it, the same as on the admin page.

The old commands `/config set`, `/add-event`, `/lineup post`, `/admin book`, `/admin cancel`, `/admin dj set-active`, `/list-djs`, `/status` and `/intervene` no longer exist. Settings are changed in `/setup`.

### 7.4 Lineup posts and booking button

- Whenever the lineup of the upcoming event changes, the bot posts a new `Lineup Update` message in the DJ channel. Upcoming event means the earliest event whose local date is today or later and whose status is not Done or Canceled. Older posts stay as they are, except for the tidy-up described below. The first post comes when you press Activate; later ones follow lineup changes. Run `/post lineup update` to post it immediately.
- Tidy-up: when the bot posts a lineup update for an event and the last message in the DJ channel is the bot's own previous lineup post for the same event, the bot removes that old post, so the channel shows one current post instead of a stack. It removes only its own lineup posts, only for the same event, and only when nobody wrote after it: if anyone posted a message in between, or the last post was for another event, both posts stay. The same applies to `/post lineup update`. If the new post cannot be sent, the old one is not removed.
- Post format: the title is the event name and date, for example `🎶 Rave Night · Saturday 14.11.2026` (the title keeps the plain date: Discord shows no timestamps there). The text starts with the genre, the location and the note, each only when set, then how many slots are booked (`4 of 6 slots booked · 2 open`), then the slots as two aligned columns, `Slot` (slot number and time) and `DJ`, one row per slot, for example `Slot 1 · 20:00 | DJ Felix` and `Slot 2 · 21:00 | 🟢 *open slot*`. DJ names longer than 24 characters are cut with an ellipsis so a long name cannot wrap and shift the rows. Times are Discord timestamps: each reader sees them in their own time zone. Buttons show only numbers (`Slot 2`, `Event 1`, `Booking 1`), never a time or date.
- **Show lineup** in a DM shows the upcoming event. With more than one upcoming event, date buttons sit under the lineup; pressing one switches the same message to that event and keeps the buttons.
- After a booking, a cancellation, an invite answer or the DJ setup ends (also when it failed or was cancelled), the bot sends the menu again about 2 seconds later, so a DJ never has to type to get the buttons back. Show lineup is the exception: no menu follows it. The bot removes its own previous menu in the DM. A flow that is left open until it times out sends no menu.
- Every post has a `Book a slot` button. A DJ who presses it gets the booking flow in DMs. If DMs are blocked, the bot says so privately.
- A member who has the DJ role but no DJ profile yet (the onboarding was never finished) cannot book. When they press `Book a slot` on the post, or `Book a slot` in the DMs, the bot starts the onboarding conversation in their DMs instead, and they press `Book a slot` again once the profile is saved. Members without the DJ role are told to ask an organizer for it.
- `/post lineup update` posts the upcoming event again. `/post lineup update event:<event id>` posts any future event.
- Automatic invites can be turned off in `/setup` (Automatic invites step). That stops only the proactive invite DMs. Booking menus, reminders and all other DMs keep working.

### 7.5 Done and canceled events are replaced

The bot keeps the configured number of future, open events. An event counts as open when its local date is today or later and its status is not Done or Canceled. When you set an event to Done or Canceled (in Notion by hand, or with `/cancel event`), the next poll (within five minutes) creates a new event so the count is met again. New events follow the schedule: with a first event date set and no scheduled event ahead, the first one gets that date and the next ones follow every interval days; otherwise they start one interval after the latest scheduled event date or today, whichever is later. They use the default start, slot length and slot count. Events created by hand with `/create event` are not scheduled events: they never count toward the number and never move the schedule. A date that already has a manual event is skipped. With scheduling off in `/setup` the bot creates no events. Past events and events with Done or Canceled status never count.

### 7.6 Joining the Active DJ Booking Group

A member who gets the DJ role receives a welcome DM with two buttons: `Yes, I want to be booked` and `Not now`. The same start is offered from the menu (`Contact me for Booking`) to a member with the DJ role who has no profile yet. There is no popup; the bot asks five questions as normal DM messages and the DJ answers by writing back.

1. DJ name. Required.
2. VRChat name. Can be skipped.
3. Logo. Send one image as a file attachment (PNG or JPEG, including iPhone photos, up to 5 MB). The bot scales it down to at most 1024 px on the longest side. Links and several images are refused with a short explanation. Can be skipped.
4. Stream link (Twitch, YouTube or stream.vrcdn). Required.
5. Backup stream link. Can be skipped.

Every question has a `Cancel` button; questions that can be skipped also have `Skip`. A stream link that does not look like Twitch, YouTube or stream.vrcdn is answered with a hint and a `Use it as it is` button to keep it anyway. At the end the bot shows the profile with `Looks good` and `Start over`. After `Looks good` the DJ is in the Active DJ Booking Group, is saved in the database and gets the Active Booking role. With Notion set up, the DJ also gets a row in the Notion DJs database in the background (the logo is uploaded there as a file). If Notion does not accept the logo, the profile is still saved and the DJ can book; the bot sends the logo to Notion again at its next start.

`Not now` ends an unfinished setup. Any later DM opens the menu, where `Contact me for Booking` starts again.

The DJ's answers during the conversation are not copied to the admin log channel. Buttons of older questions answer `This message is out of date. Use the latest message from me, or press Cancel there.` While a setup or a change is open, every DM is taken as the answer to the open question. A change of a profile value that is not answered within 1 hour is dropped and nothing changes; the next DM then opens the menu as usual.

DJs that joined before this change keep their stored profile and stream links. The menu shows `My DJ profile` to every DJ with a profile. It lists the profile (with the logo) and has buttons to change the DJ name, VRChat name, logo, stream link and backup link, and `Leave the booking group` (`Join the Active DJ Booking Group` after leaving). Leaving stops invites and removes the Active Booking role; the profile stays.

### 7.7 The admin page

A small web page to manage the admin list and the DJ list. It is off until you set `ADMIN_PAGE_PASSWORD`.

**Warning:** the page has no HTTPS. Use it only on your local network and never expose it to the internet (no port forwarding, no public reverse proxy).

1. Set `ADMIN_PAGE_PASSWORD` (a long random value). Optionally set `ADMIN_PAGE_PORT` (default `8080`).
2. Map the container port to a host port. The container port must equal `ADMIN_PAGE_PORT` (`8080` unless you changed it); the host port is any free port. In Unraid, add a **Port** with that container port. With Docker Compose use `ports: ["8080:8080"]` (host port first, container port second). If you changed `ADMIN_PAGE_PORT`, use that value as the container port, for example `"8095:9000"` for `ADMIN_PAGE_PORT=9000`.
3. Open `http://SERVER_IP:HOST_PORT/` in a browser and log in with the password. If the port is wrong or already in use, the container log shows an `Admin page not started` error and the bot keeps running without the page.

What the page shows: the server name, the owners from `ADMIN_USER_IDS` (they cannot be removed), the added admins with their Discord name when it can be found, and the cards **Data and backup** and (only with Notion set up) **Notion sync**.

What you can do:
- When the bot cannot find its Notion databases, the page shows a red card with **Create new databases** (see 2.7). This is the only way the bot replaces its Notion databases.
- Open **Schedule** to see the schedule settings, set the defaults, manage rules and edit genre, location and note of upcoming events (see 7.8).
- Open **Manage DJs** to add, edit and delete DJs, with an optional logo upload. The same works with the `/dj` commands.
- Add an admin by Discord user ID (17 to 20 digits). The ID does not have to be a server member. Bot accounts are refused ("Bots cannot be admins."). An added admin can use every admin command, including `/setup`. While the bot is active, adding or removing an admin writes an entry to the admin log channel. The entry names who did it; for changes made on the page it says "the admin page". The owners from `ADMIN_USER_IDS` are the only ones who can manage admins with the `/admins` commands (see 7.3).
- Remove an added admin (also written to the admin log channel).
- **Download a backup** and **Restore a backup** (the whole database in one file, see 2.11). While a restore or rebuild runs, the page shows that the bot is busy and bookings are refused.
- In the **Notion sync** card (only with Notion): see when the last sync ran, how many changes wait for Notion and since when, the last error, and the open conflicts and warnings. Per conflict choose **Keep Postgres** or **Keep Notion**; per warning choose **Resync Postgres to Notion** (see 2.9). **Resync all to Notion** and **Rebuild from Notion** are at the bottom of the card (see 2.11). **Retry the import from Notion** appears there when the import after an update failed.
- Log out. This clears the cookie in your browser only.

A DJ added without a Discord user ID is a **guest**. Admins can book guests and they appear in the lineup, but the bot never sends them anything (no invites, reminders or booking notices) and they are not part of the automatic invites.

Sessions end when the bot restarts; you log in again. Every wrong password is answered after a short delay (0.5 seconds), and after 5 wrong attempts from one address within 5 minutes, that address is blocked for 5 minutes.

### 7.8 Genre, location, notes and rules

Every event can have a **genre** (up to 40 characters), a **location** (up to 80 characters, one line) and a **note** (up to 300 characters, line breaks allowed). Values are trimmed; an empty value means none. The lineup shows each of them only when it is set.

Where to set them: `/create event` (options), `/edit event`, the **Schedule** page of the admin page, or the Notion columns `Genre`, `Location` and `Notes`.

**Defaults and rules** fill scheduled events (not events made with `/create event`):
- The **default genre** and **default location** apply when no rule matches.
- A **rule** gives every n-th scheduled event a value, counted from a start event you pick from the upcoming dates. Example: location "Hall B", every 2, from the next event gives Hall B to the next event, the one after the next, and so on. `every` is 1 to 52.
- Two rules for the same field that can hit the same date cannot both exist; the bot names the colliding rule and refuses the new one.
- A value set by hand (command, admin page or a Notion edit) always beats rules and defaults, and stays when rules change. Clearing a hand value hands the event back to the rules.

**Apply flow.** Changing a default or a rule never rewrites upcoming events by itself. Afterwards the bot lists the events whose value would change and you choose **Apply to all**, **Pick events** (**Apply selected** on the admin page) or **Keep all as they are**. You can run `/schedule apply` or open the apply list on the admin page later. The bot checks again when you press the button and only writes events that are still affected. New scheduled events always get the current rule values.

**Admin page.** The **Schedule** page shows the settings (read only: interval, first date, kept ready, on or off; change them with `/setup`), the defaults, the rules with **Edit** and **Remove**, an **Add rule** form, and the upcoming events with their genre, location and note. A banner links to the apply list when events differ from the rules.

### 7.9 DJ Management lineup message

An optional message for the DJ management: one lineup per fully booked event, in a channel you choose. It is off until you pick a channel in `/setup` (the optional DJ Management channel step, right before the summary). Pick a channel only staff can see: the message contains DJ names and stream links. Skip the step or press **Clear** to turn it off. Clearing the channel later stops new posts only; messages already posted keep being edited in the channel they were posted in. If that channel is deleted, the lineups move to the channel currently set in `/setup`. The bot checks that it can send messages and embeds there.

**Existing events.** When you choose a channel and fully booked upcoming events already exist, the wizard asks what to do and blocks **Next** until you answer:
- **Post all existing (N)**: posts a message for each of them, in date order, when you activate or save.
- **Only new**: posts nothing for them. They get a message only after they open up and are fully booked again.

**What the message shows.** The event name and date, start time, slot length, genre, location and note (each only when set), then per slot: number and time, the DJ name, the stream link and the backup stream link (`none` when missing). Long lineups are split over several messages. Discord names, VRChat names and logos are not in the channel message.

**Staying current.** The bot posts the message once, when the event is fully booked, and then edits it whenever the content changes: a swapped DJ, a changed stream link or DJ name, a changed event detail. It never posts a second message for the same event. If a booking is cancelled, the same message stays and shows the open slots with the note `No longer fully booked: open slots are marked.`; when the slots are filled again the note disappears. A canceled event is edited to say it was canceled. The bot never deletes lineup messages: if the lineup gets shorter, surplus messages are edited to a short placeholder. If a message was deleted by hand, the bot posts the lineup again shortly after, and the remaining old messages of that lineup are edited to point to the new one.

**Send me more information.** The first message has this button. It works for admins only (see [7.3](#73-commands)), also after a restart. An admin who presses it gets DMs with the complete current lineup: one header DM, then one DM per DJ with the slot and time, DJ name, Discord name, VRChat name, stream link, backup link and the logo as an attached image. The data is read at the moment of the click. The admin also gets a private confirmation. If the DMs are closed the bot says so privately; if a DM fails midway it says how many DJ DMs were sent. Anyone else gets a private refusal and receives nothing.

If the bot cannot use the channel (deleted or no permission), it writes one entry to the admin log channel and keeps running.

### 7.10 Booking rules

Two rules apply to DJs who book or cancel through the bot. Both are on by default.

- **Slot limit.** A DJ can hold at most N slots in one event. Default: 2 (allowed 1 to 100).
- **Cancellation cutoff.** A DJ cannot cancel a slot through the bot within M minutes before the event starts. The time is measured from the start of the event, not of the DJ's own slot. Default: 60 (allowed 0 to 10080; 0 means cancelling stays open until the event starts).

**What a DJ sees.**
- Events where the DJ already holds N slots are not offered in the booking menu. If that is true for every open event, the DJ is told so and asked to cancel a slot first.
- A booking that would exceed the limit is refused with a message that names the limit. This also covers an invite acceptance and two bookings pressed at the same time.
- Cancelling inside the cutoff is refused with a message that names the cutoff and points to an admin.

**Changing the rules.** Run `/setup` again (the Booking rules step shows the stored values) or use the **Booking rules** page of the [admin page](#77-the-admin-page). Both rules can be switched off, and the numbers can be changed. A change applies to the next action. `/config show` lists both rules.

**What the rules do not limit.**
- Admins can always book a DJ by hand over the limit and cancel a slot inside the cutoff (see [7.3](#73-commands)).
- Hand edits in Notion that book a DJ are not limited either.
- Existing bookings stay when the limit is lowered. Only new bookings are refused; a DJ over the limit can still cancel, subject to the cutoff.
- Only slots the DJ holds count. Open invites, extra DJs listed on a slot and other events do not.

**Existing installs.** After the update both rules are on with the defaults, because nothing is stored yet. Switch them off in `/setup` or on the admin page if you do not want them.

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

This only happens when both Notion variables are set. To run without Notion, remove both. Otherwise the bot could not create or read its Notion databases at startup. Check:
- The parent page is shared with the integration (page menu → **Connections**)
- `NOTION_API_KEY` is the Internal Integration Secret of that integration
- `NOTION_PARENT_PAGE_ID` is the 32-character ID at the end of the page URL (see [section 2.3](#23-find-the-parent-page-id))
- The integration has the Read, Insert and Update content capabilities
- The Notion API is reachable from the bot container

The text after "Cause:" names the failing request. Fix the setting and restart the bot.

### Bot exits at startup and the cause names a property with the wrong type

Someone changed the type of a property the bot needs (e.g. `Slots` from Number to Text). The bot never changes a property type back by itself. Change the type back to the one listed in [section 2.4](#24-what-the-bot-creates) and restart the bot.

### A slot I edited in Notion went back to the old DJ

The bot changed that slot too before it saw your edit (a conflict), or your edit was not applied (a warning). Open the admin page, look in the **Notion sync** card and choose **Keep Notion** or **Keep Postgres**; without a decision the bot's version wins after 24 hours (see [section 2.9](#29-conflicts-and-warnings)).

### A hand edit in Notion did nothing

The bot applies an edit only after the same checks as the matching command, and only on its next poll (every five minutes, while the bot is active). A rejected edit is listed as a warning on the admin page and in the log channel (`Notion edit not applied`) with the reason. Edits in the Bot data database are always ignored (see [section 2.10](#210-bot-data-is-read-only)).

### Booking and edits are paused after an update

The one-time import from Notion failed, or the bot could not find its Notion databases before that import ran. The log channel (`Import from Notion failed`) and the admin page list the rows to fix. Fix them in Notion, then press **Retry the import from Notion** on the admin page, or restart the bot (see [section 2.11](#211-backup-restore-rebuild-and-resync)).

### The bot does not answer

The bot is probably idle. Run `/config show`: it shows the `Bot status` row (followed by a blank line and `To change a setting, run /setup again.`). If it says `idle`, run `/setup` and press Activate on the last step. While idle, DJs get "I can't answer right now. Please try again later." and admins get "The bot is not active yet. Run /setup to finish the setup." for every command except `/setup` and `/config show`. If the bot does not respond to any command, check that it is online, the container log shows `Bot ready`, and your user ID is in `ADMIN_USER_IDS`.

### Admin log channel shows no messages

The log channel may not be configured yet. Run `/setup` as an admin user and pick the log channel (step 3). Until a log channel is set, entries are written to container stdout only. See [section 9](#9-the-admin-log-channel) for what the channel shows.

---

## 9. The Admin Log Channel

Set it in `/setup` (step 3, Log channel); `/config show` shows the current channel. The channel shows only what an organizer needs to know. Each entry is a short embed with the footer `RaveBot`.

| Entry | When it appears |
|---|---|
| `Booked` | A DJ booked a slot by DM, or you added one in Notion. |
| `Cancelled` | A DJ cancelled a slot by DM. |
| `Changed by an organizer` | An organizer booked or removed a DJ with `/book dj slot` or `/cancel dj slot`. |
| `Changed in Notion` | A DJ was removed from a slot by hand in Notion. |
| `Applied from Notion` | The bot took over hand edits made in Notion (changed values, bookings, new DJs or events). One entry per poll. |
| `Changed in the bot and in Notion` | A conflict: choose Keep Postgres or Keep Notion on the admin page. After 24 hours the bot's version wins (`Postgres kept after 24 h`). |
| `Notion edit not applied` | A warning: the bot did not apply a hand edit. Fix it in Notion or resync on the admin page. |
| `Data imported from Notion` | The one-time import after the update finished. |
| `Event canceled` | An organizer canceled an event with `/cancel event`. Lists the DJs who were booked. |
| `Event fully booked` | Every slot of an event has a DJ. Posted once per fill, and again if a slot opens and the event fills up once more. |
| `New DJ` | A DJ joined the pool. |
| `Message from <name>` | A user sent the bot a DM that is not part of a booking flow or a DJ profile conversation. |
| Critical problems | The bot could not DM a DJ, a Notion write failed, Notion could not be read, no more DJs are left to ask, the database is unavailable, or something else failed. These are red and have a `What to do` line. |

Button taps, the bot's own replies and routine sync work are not logged.

After an update, the first poll announces every future event that is already fully booked once. This does not repeat.

### Answering a user message

1. Open the `Message from <name>` entry in the log channel.
2. Click `Reply`. Only organizers can use it; everyone else gets a private refusal.
3. Type your answer in the popup and send it. The bot sends it to the user as a DM and confirms privately with `Sent.`
4. If the user has DMs from server members turned off, the bot tells you privately and the entry stays as it is.

The button works on old entries too, also after a restart.
