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
7. [First-Run Setup and Commands](#7-first-run-setup-and-commands)
8. [Troubleshooting](#8-troubleshooting)
9. [The Admin Log Channel](#9-the-admin-log-channel)

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

The bot keeps the configured number of future events itself (the number is set in `/setup`, step Events). To add one by hand, create a row in **Events** (or run `/create event`) and fill in:

- `ID` - `YYYY-MM-DD <name>`, e.g. `2026-12-31 New Year Rave`. Must be unique.
- `Date`
- `Slots` - e.g. `6`
- `Start` - `HH:MM` local time, e.g. `20:00`
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

### 2.8 Time Zone

All start times are local wall-clock times, not UTC. The default zone is `Europe/Berlin`: an event at `21:00` starts at 21:00 CET in winter and 21:00 CEST in summer, so 21:00 stays 21:00 all year. The `Start` column, the slot times in the lineup post, the reminders and the day an event counts as "today" all use this zone. Discord timestamps are not used. The zone is stored in the setting `timezone` and is changed in `/setup` (Events step, button for the time zone; enter a name such as `Europe/Berlin`). Changing it moves the real start of existing events, because their `Start` text is kept as it is.

---

## 3. Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to version control.**

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | - | Bot token from Discord Developer Portal |
| `POSTGRES_URL` | Yes | - | PostgreSQL connection string: `postgresql://user:pass@host:5432/dbname` |
| `NOTION_API_KEY` | Yes | - | Internal Integration Secret of your Notion integration (see [section 2.1](#21-create-an-integration)) |
| `NOTION_PARENT_PAGE_ID` | Yes | - | 32-character ID of an empty Notion page shared with the integration; the bot creates its databases under it |
| `ADMIN_USER_IDS` | Yes | - | Comma-separated Discord user IDs of the owners: they are always admins and cannot be removed on the admin page |
| `ADMIN_PAGE_PASSWORD` | No | - | Password of the admin page. While empty, the page is off. Choose a long random value |
| `ADMIN_PAGE_PORT` | No | `8080` | Port of the admin page inside the container (1 to 65535) |
| `LOG_LEVEL` | No | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DB_POOL_MIN` | No | `5` | Minimum asyncpg connection pool size |
| `DB_POOL_MAX` | No | `20` | Maximum asyncpg connection pool size |

The key `GUILD_ID` is not read by the bot. If it is still in your `.env` or container settings, you can delete it.

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
5. Port mappings: leave them empty if you do not use the admin page. To use it, add a **Port** with container port `8080` and a free host port (for example `8080` or `8095`), and set the variable `ADMIN_PAGE_PASSWORD` in step 4 (see [section 7.7](#77-the-admin-page))

### Step 4: Configure Environment Variables

In the Add Container form, click **"Add another Path, Port, Variable, Label or Device"** → **Variable** for each:

| Key | Example Value |
|---|---|
| `DISCORD_BOT_TOKEN` | `your_bot_token` |
| `POSTGRES_URL` | `postgresql://ravebot:pass@192.168.1.100:5432/ravebot` |
| `NOTION_API_KEY` | `your_notion_integration_secret` |
| `NOTION_PARENT_PAGE_ID` | `your_notion_parent_page_id` |
| `ADMIN_USER_IDS` | `123456789012345678` |
| `ADMIN_PAGE_PASSWORD` | `a_long_random_password` (only for the admin page) |
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

## 7. First-Run Setup and Commands

### 7.1 The idle bot

After a fresh deploy the bot is idle until an admin finishes `/setup`. While idle it does the following:

- It sends no lineup posts, invites, reminders or onboarding DMs, and the Notion poll does not run.
- Buttons and menus under old messages answer privately: "I can't answer right now. Please try again later."
- A DJ who writes the bot a DM gets the same sentence. Nothing is logged.
- Admin commands other than `/setup` and `/config show` answer privately: "The bot is not active yet. Run /setup to finish the setup."
- When the bot starts idle, it sends every admin (`ADMIN_USER_IDS` and added admins) a DM: "RaveBot is not set up yet. Run /setup in the channel where you want to do it." The DM goes out once per start. If an admin has DMs from server members turned off, the bot only writes a warning to the container log.

A server that already has both a DJ channel and a log channel stored (set up before `/setup` existed) counts as active, so an update does not interrupt it.

### 7.2 The `/setup` walkthrough

Run `/setup` in the channel where you want to set up (admins only). The bot posts one wizard message and edits it step by step. The wizard survives a restart; running `/setup` again while it is open links to the open message.

1. **Welcome and checks.** Shows whether the Discord connection, the database and the three Notion databases work. The bot stays idle until you press **Activate** at the end.
2. **DJ channel.** Pick the channel for the Lineup Update and the `Book a slot` button. The bot checks it can send messages and embeds there.
3. **Log channel.** Pick the channel for bookings, new DJs, problems and DJ messages. Only organizers should see it. The same permission check runs.
4. **Roles.** Pick the DJ role (the welcome DM goes to members who get it) and the invites role (the bot gives it to DJs who want automatic invites). The two roles must differ, `@everyone` and integration roles are refused, and the bot's own role must be above both. A button can create the invites role (default name `Active Booking`). Any role selection needs the Manage Roles permission: the bot refuses roles it cannot manage.
5. **Events.** Slots per event, start time (local, `HH:MM`), slot length in minutes, one event every N days, number of future events kept ready. A second button sets the time zone (default `Europe/Berlin`).
6. **Automatic invites.** Turn invites on or off, how many days before an event the bot starts asking, and the hours it waits between two invites to the same DJ.
7. **Summary.** Lists everything. **Activate** writes the settings, switches the bot on, creates the missing future events and posts the first Lineup Update (it reports how many events it created and whether it posted). The channels and roles must be chosen first.

Nothing is stored until Activate. Defaults: 6 slots, 20:00, 60 minutes, every 7 days, 6 events ahead, invites on, 14 days before, 12 hours wait.

**Changing settings later.** Run `/setup` again. An active bot answers with a **Change settings** button. The wizard then starts from the stored values and ends with **Save** instead of Activate. Save only stores the values: it creates no events and posts nothing. Two warnings can appear in the summary: a changed time zone moves the real times of existing events, and a changed invites role does not move members to the new role.

**Roles without the wizard.** If no role was stored (older setup), the bot uses a role named `DJ` for onboarding and `Active Booking` for invites. Once `/setup` stores roles, the stored ones are used.

Finally check that the Events, Slots and DJs databases appeared under your Notion parent page, and use `/config show` to review all settings and the bot status (`active` or `idle`).

### 7.3 Commands

All commands are for admins: the user IDs in `ADMIN_USER_IDS` plus the admins added on the [admin page](#77-the-admin-page). Added admins can use every admin command, including `/setup`. The commands work on the server only, not in DMs. Others see "You don't have permission to use this command."

| Command | What it does |
|---|---|
| `/setup` | Guided setup; re-run it with **Change settings** to edit settings. Works while idle. |
| `/config show` | Shows every setting (stored value or default) and the bot status. Works while idle. |
| `/create event date:<YYYY-MM-DD>` | Creates an event by hand using the default start, slot length and slot count. |
| `/post lineup update [event:<event id>]` | Posts the lineup of the upcoming event, or of a chosen future event, in the DJ channel. |
| `/book dj slot dj:<member> event:<event> slot:<number>` | Books a DJ into a slot without asking for confirmation. |
| `/cancel dj slot dj:<member> event:<event> slot:<number>` | Frees a DJ's slot. |
| `/list all djs` | Lists all registered DJs and whether they get automatic invites. |
| `/automatic dj booking dj:<member> enabled:<on/off>` | Adds a DJ to or removes a DJ from the automatic booking pool and syncs the invites role. |

The old commands `/config set`, `/add-event`, `/lineup post`, `/admin book`, `/admin cancel`, `/admin dj set-active`, `/list-djs`, `/status` and `/intervene` no longer exist. Settings are changed in `/setup`.

### 7.4 Lineup posts and booking button

- Whenever the lineup of the upcoming event changes, the bot posts a new `Lineup Update` message in the DJ channel. Upcoming event means the earliest event whose local date is today or later and whose status is not Done or Canceled. Older posts stay as they are. The first post comes when you press Activate; later ones follow lineup changes. Run `/post lineup update` to post it immediately.
- Post format: line 1 is `Event Saturday 14.11.2026`, followed by one line per slot, for example `Slot 1  20:00  DJ Felix` or `Slot 2  21:00  OPEN SLOT`. Times are local time (CET or CEST).
- Every post has a `Book a slot` button. A DJ who presses it gets the booking flow in DMs. If DMs are blocked, the bot says so privately.
- `/post lineup update` posts the upcoming event again. `/post lineup update event:<event id>` posts any future event.
- Automatic invites can be turned off in `/setup` (Automatic invites step). That stops only the proactive invite DMs. Booking menus, reminders and all other DMs keep working.

### 7.5 Done and canceled events are replaced

The bot keeps the configured number of future, open events. An event counts as open when its local date is today or later and its status is not Done or Canceled. When you set an event to Done or Canceled in Notion, the next poll (within five minutes) creates a new event so the count is met again. New events start one interval after the latest event date or today, whichever is later, and use the default start, slot length and slot count. Past events and events with Done or Canceled status never count.

### 7.6 Joining the Active DJ Booking Group

A member who gets the DJ role receives a welcome DM with two buttons: `Yes, I want to be booked` and `Not now`. The same start is offered from the menu (`Contact me for Booking`) to a member with the DJ role who has no profile yet. There is no popup; the bot asks five questions as normal DM messages and the DJ answers by writing back.

1. DJ name. Required.
2. VRChat name. Can be skipped.
3. Logo. Send one image as a file attachment (PNG or JPEG, including iPhone photos, up to 5 MB). The bot scales it down to at most 1024 px on the longest side. Links and several images are refused with a short explanation. Can be skipped.
4. Stream link (Twitch, YouTube or stream.vrcdn). Required.
5. Backup stream link. Can be skipped.

Every question has a `Cancel` button; questions that can be skipped also have `Skip`. A stream link that does not look like Twitch, YouTube or stream.vrcdn is answered with a hint and a `Use it as it is` button to keep it anyway. At the end the bot shows the profile with `Looks good` and `Start over`. After `Looks good` the DJ is in the Active DJ Booking Group, has a row in the Notion DJs database (the logo is uploaded there as a file) and gets the Active Booking role. If Notion does not accept the logo, the profile is still saved and the DJ can book; the bot sends the logo to Notion again at its next start.

`Not now` ends an unfinished setup. Any later DM opens the menu, where `Contact me for Booking` starts again.

The DJ's answers during the conversation are not copied to the admin log channel. Buttons of older questions answer `This message is out of date. Use the latest message from me, or press Cancel there.` While a setup or a change is open, every DM is taken as the answer to the open question. A change of a profile value that is not answered within 1 hour is dropped and nothing changes; the next DM then opens the menu as usual.

DJs that joined before this change keep their stored profile and stream links. The menu shows `My DJ profile` to every DJ with a profile. It lists the profile (with the logo) and has buttons to change the DJ name, VRChat name, logo, stream link and backup link, and `Leave the booking group` (`Join the Active DJ Booking Group` after leaving). Leaving stops invites and removes the Active Booking role; the profile stays.
### 7.7 The admin page

A small web page to manage the admin list. It is off until you set `ADMIN_PAGE_PASSWORD`.

**Warning:** the page has no HTTPS. Use it only on your local network and never expose it to the internet (no port forwarding, no public reverse proxy).

1. Set `ADMIN_PAGE_PASSWORD` (a long random value). Optionally set `ADMIN_PAGE_PORT` (default `8080`).
2. In Unraid, map container port `8080` to a free host port. With Docker Compose use `ports: ["8080:8080"]`.
3. Open `http://SERVER_IP:HOST_PORT/` in a browser and log in with the password. If the port is wrong or already in use, the container log shows an `Admin page not started` error and the bot keeps running without the page.

What the page shows: the server name, the owners from `ADMIN_USER_IDS` (they cannot be removed) and the added admins with their Discord name when it can be found.

What you can do:
- Add an admin by Discord user ID (17 to 19 digits). The ID does not have to be a server member. An added admin can use every admin command, including `/setup`. Adding an admin writes an entry to the admin log channel; removals do not.
- Remove an added admin.
- Log out. This clears the cookie in your browser only.

Sessions end when the bot restarts; you log in again. Repeated wrong passwords are slowed down.

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
| `Event fully booked` | Every slot of an event has a DJ. Posted once per fill, and again if a slot opens and the event fills up once more. |
| `New DJ` | A DJ joined the pool. |
| `Message from <name>` | A user sent the bot a DM that is not part of a booking flow or a DJ profile conversation. |
| Critical problems | The bot could not DM a DJ, a Notion write failed, a hand edit was reverted, no more DJs are left to ask, the database is unavailable, or something else failed. These are red and have a `What to do` line. |

Button taps, the bot's own replies and routine sync work are not logged.

After an update, the first poll announces every future event that is already fully booked once. This does not repeat.

### Answering a user message

1. Open the `Message from <name>` entry in the log channel.
2. Click `Reply`. Only organizers can use it; everyone else gets a private refusal.
3. Type your answer in the popup and send it. The bot sends it to the user as a DM and confirms privately with `Sent.`
4. If the user has DMs from server members turned off, the bot tells you privately and the entry stays as it is.

The button works on old entries too, also after a restart.
