# RaveBot - Developer Guide

Local development setup, testing, linting, Docker builds, and release workflow.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local Development Setup](#2-local-development-setup)
3. [Tests, Linting, Type Checking](#3-tests-linting-type-checking)
4. [Project Structure](#4-project-structure)
5. [Manual Docker Build & Release](#5-manual-docker-build--release)
6. [Database Migrations](#6-database-migrations)
7. [Automated Releases](#7-automated-releases)

---

## 1. Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** - fast Python package manager (`pip install uv` or see uv docs)
- **Docker** - for the local PostgreSQL container
- **Git**

---

## 2. Local Development Setup

```bash
# Clone and enter the project
git clone <repo-url>
cd output/ravebot

# Install all dependencies including dev extras (creates .venv automatically)
uv sync

# Start PostgreSQL only - the bot runs natively, NOT via compose
docker compose up -d
# Verify: docker compose ps  →  postgres should show as healthy

# Copy and fill in env vars
cp .env.example .env
# Edit .env: set DISCORD_BOT_TOKEN, ADMIN_USER_IDS (NOTION_API_KEY and NOTION_PARENT_PAGE_ID only for the optional Notion mirror)
# POSTGRES_URL default (postgresql://ravebot:ravebot@localhost:5432/ravebot) matches the compose config

# Run the bot
uv run python -m bot.main
```

> The `docker-compose.yml` in this repo is **postgres-only** - it starts a local PostgreSQL instance for development. The bot itself is run natively with `uv`. Do not `docker compose up` the bot service in production from this file; see the [User Guide](user-guide.md#5-deployment-docker-compose) for a full production compose setup.

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

# Real-SQL tests (tests/pg): need a throwaway PostgreSQL database whose name ends in
# _test, the suite drops and recreates the schema. Without the variable they are skipped.
podman run -d --name ravebot-pg-test -e POSTGRES_USER=ravebot \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=ravebot_test -p 55432:5432 postgres:16-alpine
RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot@localhost:55432/ravebot_test uv run pytest -m pg tests/pg

# Container smoke test: builds the image and starts it like in production (PostgreSQL
# container, root-owned folder mapped to /data/backups, the image's entrypoint). It checks
# uid 1000, the writable backup folder, module imports, migrations, a safety export and the
# start-up up to the Discord login (a fake token is refused, which proves the start got that
# far; set SMOKE_DISCORD_TOKEN to check a real login). Uses podman, else docker.
scripts/smoke-container.sh
# scripts/release.sh runs it before tagging; CI runs it on every push, and the release
# workflow publishes no image unless it (and the real-SQL tests) passed.

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
│   │   ├── admin.py        # /config show, /create event, /post lineup update, /book dj slot, /cancel dj slot, /cancel event, /dj add|edit|remove, /list all djs, /automatic dj booking
│   │   ├── booking.py      # DJ self-service booking (thin shell - logic in views.py)
│   │   ├── events.py       # Lineup post auto-update, event auto-creation
│   │   ├── onboarding.py   # DJ role detection, welcome DM, DM routing
│   │   ├── outreach_cog.py # Proactive outreach background loop
│   │   └── setup.py        # /setup (wizard entry point)
│   ├── backup/             # export.py (database to one gzip JSON file), restore.py (file back into Postgres)
│   ├── db/                 # asyncpg repository layer (events, bookings, djs, mirror, outbox, issues, ...)
│   ├── maintenance.py      # try_claim(): one data-replacing operation at a time
│   ├── results.py          # ActionResult (shared by the admin page and the sync code)
│   ├── sync/               # Notion mirror and sync (see "Postgres as source of truth, Notion as mirror")
│   │   ├── worker.py       # MirrorWorker, changed(): outbox to Notion
│   │   ├── cycle.py        # SyncCycle: the Notion poll (conflicts, warnings, 24 h expiry, resync)
│   │   ├── plan.py         # three-way planner (Postgres vs Notion vs last synced state)
│   │   ├── intake.py       # checks and applies hand edits from Notion
│   │   ├── payloads.py     # compared payloads of events, slots and DJs
│   │   ├── botdata.py      # the read-only "Bot data" mirror
│   │   ├── notion_import.py # upgrade import from Notion
│   │   ├── rebuild.py      # rebuild from Notion, startup recovery cases
│   │   └── mirror_start.py # starts the worker and the cycle
│   ├── web/                # Admin page (aiohttp): app.py routes and middlewares, auth.py, pages.py, service.py
│   ├── notion/             # Notion integration (API version 2026-03-11)
│   │   ├── api.py          # NotionApi: httpx wrapper, retry/backoff, pagination
│   │   ├── models.py       # Property names, typed EventInfo/SlotInfo/DjInfo, parsers
│   │   ├── schema.py       # ensure_schema(): creates/verifies Events, Slots, DJs, Bot data
│   │   ├── views.py        # ensure_views(): Overview, Lineup, All DJs
│   │   └── client.py       # NotionClient: mirror_* writes, read_all(), trash_page()
│   ├── services/           # Business logic
│   │   ├── activation.py   # is_active(), set_active(), bot_is_active()
│   │   ├── admin_booking.py
│   │   ├── fully_booked.py # check_fully_booked(): once-per-fill announcements
│   │   ├── dj_sync.py      # sync_dj(), queue_unsynced_logos(): queue DJ changes for the mirror
│   │   ├── event_cancel.py # EventCancelService: /cancel event (status, bookings, notices)
│   │   ├── event_defaults.py # default start, slot length, slots per event (config)
│   │   ├── event_replenish.py # replenish_events(): keeps future open events ready
│   │   ├── lineup.py       # LineupService: posts, snapshot dedupe, upcoming_event
│   │   ├── onboarding.py
│   │   ├── outreach.py
│   │   ├── reminders.py
│   │   └── roles.py        # get_dj_role(), get_invites_role()
│   ├── timezone.py         # local time: get_timezone(), local_now(), local_today()
│   ├── ui/
│   │   ├── embeds.py       # Embed classes, LogEntryEmbed, LineupUpdateEmbed, lineup_text()
│   │   ├── gates.py        # GatedView and the idle texts
│   │   ├── log_reply.py    # LogReplyView and ReplyModal (log channel Reply button)
│   │   ├── setup_wizard.py # /setup wizard: steps, persistent view, activation effects
│   │   ├── texts.py        # Every user-facing string and date/slot formatters
│   │   └── views.py        # All Discord UI: buttons, modals, views
│   └── main.py             # Bot entry point: migrations, Notion setup, services, cogs (_add_cog)
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
- PostgreSQL is the source of truth for everything the bot holds, including slot assignments (see the next section). Notion is an optional mirror. The `slot_locks` table is the slots table: one row per slot, `SELECT ... FOR UPDATE` on it keeps two DJs from taking one slot.

### Postgres as source of truth, Notion as mirror

Story 10.4 once kept Notion as the source of truth; that decision is reversed. The bot reads and writes only Postgres. Notion is a live backup that organizers may also edit by hand.

- **No Notion call in a user-facing path.** No Discord handler awaits a Notion call, and the Notion poll and the outbox worker never hold a lock or connection a Discord handler needs. A slow or unreachable Notion delays nothing.
- **Notion is optional.** Without `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID` there is no outbox, no poll and no Notion call; `ctx.notion_client` and `ctx.mirror` are `None`. The admin page hides the Notion sync, status and rebuild cards. Export and import still work. Tests of a Postgres-only bot exist next to the mirror tests.
- **Outbox and `MirrorWorker`** (`bot/sync/worker.py`). A write path commits to Postgres and then calls `changed(ctx, entity, key)`. That queues the entity in `notion_outbox` from a background task and wakes the worker. The worker writes each due entity from its current Postgres state (events and DJs before slots, because a slot page needs both page ids), at most 3 Notion requests per second, with backoff, and records the written payload in `notion_mirror`. A queued write that is lost is repaired by the poll. The worker skips an entity with an open conflict. Each write (the Notion request plus the mirror record) holds `MirrorWorker.gate`; the sync cycle holds the same gate for its whole run, so the worker never records a newer state while the cycle compares an older Notion snapshot with the mirror (a stale page would otherwise read as a hand edit). Only the worker waits; Discord handlers never take the gate. A slot whose holder has no `djs` row (a deleted DJ keeps past bookings) is written and recorded without a DJ, as Notion shows it. An outbox key `stray:<page id>` is a Notion page without a bot record, queued only to be trashed. The logo is not part of the compared payload: the worker uploads it from the `djs` row while `logo_synced` is false and marks it with `mark_logo_synced_if_unchanged`. The "Discord name" cell is written from the bot cache, also outside the payload.
- **`SyncCycle` poll** (`bot/sync/cycle.py`, run by `EventsCog._poll_notion` every 5 minutes, only while the bot is active and not in maintenance). Order: close conflicts older than 24 h, `read_all()` (a failed or empty read changes nothing), then events, DJs and slots. Per entity the planner (`bot/sync/plan.py`) decides `push`, `apply`, `new_in_notion`, `conflict`, `deleted_in_notion`, `key_changed`, `mass_change` or `agree`. Applied hand edits (a changed value, an added booking, a new DJ or event) go through `Intake` (`bot/sync/intake.py`), which runs the same checks as the command path and logs the edit to the log channel. A rejected edit becomes a warning. Slots of past events are never changed from Notion. A hand-made slot page that names another DJ than the bot's booking is overwritten (Postgres wins) and reported as a warning.
- **Bot data mirror** (`bot/sync/botdata.py`). A fourth database, "Bot data", holds one row per record of `config`, `dj_conversations`, `outreach_sequences`, `outreach_contacts`, `reminders_sent`, `fully_booked_events`, `manual_events` and `setup_drafts` as JSON. It is read-only: the poll never applies it and overwrites hand edits. `web_admins`, `log_replies`, the `config` rows of guild 0 (Notion database ids, sync state) and binary logos are not mirrored. Admin accounts and secrets never go to Notion.
- **Maintenance and the single-operation lock** (`bot/maintenance.py`). The upgrade import, the rebuild from Notion and the backup import each replace data. `try_claim(name)` never waits: it returns `None` while another of them runs (the caller refuses with `texts.OPERATION_BUSY`), else a context manager that holds the claim. While one runs, `ctx.maintenance` is set and bookings, edits, DJ changes, the admin page DJ routes and the Notion poll answer with a short message or skip.
- **Notion databases missing at startup.** `ensure_schema` raises `NotionDatabasesMissing` (for example a lost share); the bot keeps running without Notion and the admin page offers to create new databases. If the one-time upgrade import has not run yet, `pause_if_import_pending` (`bot/sync/notion_import.py`) also pauses the bot (`ctx.maintenance`, `ctx.sync_problem`) so it never works on an empty database. **Retry the import from Notion** first calls `ctx.notion_connect` (read the stored databases again), then imports; creating new databases also runs the import. Either ends the pause once the import succeeds.
- **Booking a slot that no longer exists.** `acquire_slot_lock` locks the existing `slot_locks` row joined to its event and limited to the slot count (`SELECT ... FOR UPDATE OF s`); it never inserts a row. The confirm step refuses with `texts.SLOT_NO_LONGER_EXISTS` when the event was deleted or the slot removed.
- **Event keys.** `events.uid` is TEXT. Imported events keep their Notion page id as `uid`, new events get `str(uuid.uuid4())`; the uid is written to the Notion property `Bot ID`. `EventInfo.page_id` keeps its name and means "bot event key". A slot is `f"{uid}:{number}"`. Notion page ids live only in `notion_mirror.notion_page_id`.

#### Tables

| Table | Migration | Purpose |
|---|---|---|
| `events` | `0017`, `0019` | The bot's events: `uid` (key), `event_id` (unique title), date, start, slot minutes, slot count, status, genre, notes. `0019` adds `location` and the hand markers `genre_by_hand` and `location_by_hand` (existing genres count as set by hand). |
| `schedule_rules` | `0019` | Rules per guild: `id`, `guild_id`, `field` (`genre` or `location`), `value`, `every` (1 to 52), `anchor` (date of the first event the rule hits), `created_at`. Defaults live in `config` (`default_genre`, `default_location`). Access in `bot/db/schedule_rules.py`. |
| `notion_mirror` | `0017` | Per entity (`event`, `slot`, `dj`, `botdata`) and key: `notion_page_id`, `synced_hash`, `synced_payload`, `synced_at`. The last state both sides agreed on; it is what tells a hand edit from a bot change. |
| `notion_outbox` | `0017` | Pending Notion writes, one row per entity and key (latest state wins): `op` (`upsert` or `delete`), attempts, `next_attempt_at`, `last_error`. |
| `sync_issues` | `0017` | Open and resolved conflicts and warnings: `kind`, entity, key, summary, both payloads, `created_at`, `resolved_at`, `resolution` (`postgres`, `notion`, `resynced` or `expired`). At most one open conflict per entity (partial unique index). |
| `djs.notion_dj_page_id`, `event_shapes` | removed by `0018` | The DJ page id moved into `notion_mirror` (`0017` copied it). `event_shapes` guarded Notion edits of Start, Slot length and Slots; Postgres is the truth now. The downgrade re-creates both empty and copies the page ids back from `notion_mirror`. `djs.logo_synced` stays: the worker needs it. |

`TABLES` in `tests/pg/conftest.py` lists the tables the real-Postgres tests truncate; add every new table there.

#### Sync: conflicts, warnings, expiry, resync

- **Applied** automatically (and logged): Notion changed, Postgres has no unsynced change to that entity.
- **Conflict**: Notion changed an entity that also has an unsynced Postgres change. The log channel and the admin page show what changed on each side; the admin chooses **Keep Postgres** (written to Notion again) or **Keep Notion** (applied after the command-path checks; if they fail the conflict stays open). Only open conflicts stall an entity, and only that entity; warnings never stall. After 24 h without a decision (`CONFLICT_TTL`) Postgres wins, the entity is queued for Notion and the log channel says so (resolution `expired`). While the bot is idle conflicts do not expire.
- **Warning**: a deleted page, a changed key (event title, DJ Discord ID, slot number), a duplicate page, a hand edit that failed the checks, a mass change, or a failed or empty Notion read. Nothing is applied; the admin page offers **Resync Postgres to Notion**. A never-synced entity (nothing in `notion_mirror` for it) is pushed instead of guarded. A status change to Canceled or Done in Notion is applied as a plain field change (no cancel flow); `/cancel event` is the full flow.
- **Resync all** (admin page) queues every event, DJ and slot for Notion; hand edits not yet taken over are overwritten.
- **Notion databases found again.** `ensure_schema` stores the data source ids in `config` under guild 0. When none are stored (for example after losing Postgres), it adopts the databases titled Events, Slots, DJs and Bot data that already sit under `NOTION_PARENT_PAGE_ID` instead of creating new ones. A stored set of 3 databases gets the missing "Bot data" database created. A fresh set of databases clears `notion_mirror`.

#### Backup export and import

`bot/backup/export.py` writes the whole database (every base table of schema public except `alembic_version`, plus the sequences, with the Alembic revision) as one gzip-compressed JSON file, format `ravebot-backup` version 1, including logos and admin accounts. The admin page downloads it as `ravebot-<UTC time>.ravebot.json.gz`. `bot/backup/restore.py` unpacks a file in chunks (refused above `MAX_UNPACKED`, 1 GiB, so a gzip bomb cannot fill the memory), validates it, refuses one from a newer revision (the revision must equal head), writes a safety export, then replaces every table in one transaction. Safety exports go to `BACKUP_DIR` (default `/data/backups`, else `./backups` when that is not writable; the last 10 are kept). The image creates `/data/backups` for the container user (uid 1000) and declares it a volume. Its entrypoint (`docker/entrypoint.sh`) starts as root only to `chown` that folder (or `BACKUP_DIR`) to the container user, because a host folder mapped by Unraid or `docker run -v` is created as root, then drops to uid 1000 with `setpriv`; started with `--user` it runs the bot directly. Map the folder (`-v ravebot-backups:/data/backups`, or the Unraid template's Backup folder path) so the files survive a container recreate. A successful import counts as a full recovery: it clears maintenance and `sync_problem`, starts the mirror if it was not running and queues everything for Notion. A restore into a bot with Notion also marks the upgrade import done and sets `sync_post_restore` (config guild 0, survives a restart): the next sync cycle that reads Notion moves every event, DJ and slot page the restored `notion_mirror` does not know (made after the backup) to the Notion trash and records a warning for each, instead of taking them in; an event page with a known Bot ID is still adopted. The flag is cleared when that cycle finishes, and also by a rebuild from Notion (in the same transaction that writes the rebuilt data), since Notion then matches Postgres. The export runs its JSON and gzip work in a thread so it does not block Discord handling.

#### Rebuild from Notion and startup recovery

`rebuild_from_notion` (`bot/sync/rebuild.py`, admin page button) replaces events, slots, DJs and the Bot data tables with what Notion holds, in one transaction after a safety export, then downloads the DJ logos from Notion (the one exception to "logos go Postgres to Notion only") and marks them synced. `web_admins` is untouched; the owners from `ADMIN_USER_IDS` keep working and other admins return only from a backup file. The outbox and open conflicts are dropped. Unreadable Notion pages are skipped and listed as notes (a slot whose DJ page cannot be read: in the upgrade import the booking already in `slot_locks` is kept, and without one the import rolls back; in a rebuild it is imported as free with a note); the import rolls back on integrity problems (duplicate event titles or DJ names, duplicate Discord IDs, a slot holding an unknown DJ or a number outside 1..slot count).

Startup cases (all need Notion; `main()` runs them after the upgrade import):

| Postgres | Notion | Action |
|---|---|---|
| no events table yet (the upgrade) | filled | Safety export, then one automatic import from Notion. Maintenance stays on and the mirror off until it works; the result goes to the log channel, a failed import can be retried from the admin page. |
| empty (no DJs, no events) | has events | Rebuild from Notion instead of the upgrade import; a successful rebuild marks the upgrade done. |
| filled | empty or new databases | Clear `notion_mirror`, queue everything for Notion. |
| filled, unsynced writes | any | The outbox pushes them. |

A failed startup rebuild pauses the bot (maintenance) like a failed import.

### Lineup posts, outreach switch and texts

- `LineupService.refresh()` (called by the Notion poll and by booking changes, debounced) computes `event_snapshot()` of the upcoming event (`upcoming_event()`: earliest event with `date >= local_today()` and status not Done or Canceled). If it differs from the stored snapshot it posts a new `Lineup Update` embed (`LineupUpdateEmbed`) with a `Book a slot` button (`LineupPostView`). Posts are never edited; the only deletion is the tidy-up in `_send` (see below). If `dj_channel` is unset the snapshot is stored without posting.
- DJ channel tidy-up: table `dj_channel_lineup_posts` (migration `0021`), one row per DJ channel: `channel_id` (primary key), `event_id` (the event key, `event.page_id`), `message_id` (latest lineup post of the bot), `updated_at`. Repository `bot/db/dj_channel_posts.py` (`LastLineupPost`, `get_last_post`, `set_last_post` as upsert). Rule in `LineupService._send`: read the record; right before sending, decide `replace_old` = record exists and its `event_id` equals the event and `channel.last_message_id` equals the record's `message_id` (read before the send, because the send moves `last_message_id`); send; store the new record with `set_last_post`; then delete the old message with `channel.get_partial_message(old_id).delete()`, best effort (errors are logged, never raised). A failed send keeps the old post and record. A failed record read or write is logged and tolerated; if storing fails nothing is deleted. It applies to the automatic path and to `/post lineup update`, since both go through `_send`. No extra permission: the bot deletes its own message, and `get_partial_message` needs no Read Message History.
- DM menu after flows: table `dm_menus` (migration `0022`), one row per user: `user_id` (primary key), `channel_id`, `message_id` (last menu of the bot), `updated_at`. Repository `bot/db/dm_menus.py` (`LastMenu`, `get_menu`, `set_menu` upsert). `MenuService` (`bot/services/menu.py`, `ctx.menu_service`): `schedule(user_id)` is debounced per user and sends after `MENU_DELAY_SECONDS` (2.0); it skips when the user's FlowLock is held, a DJ conversation is open, the bot is idle, or the id is not positive (guest DJ). `post_now(channel, user_id)` cancels a pending menu, reads the old record, sends `texts.MENU` with `FallbackMenuView`, stores the new id, then deletes the old message best effort (NotFound is ignored); a failed send keeps the old record, a failed store skips the delete. The free-text menu in `OnboardingCog.on_message` uses `post_now`. Triggers: `FlowLock.on_release` (set by `BotContext.get_user_lock`) fires when a held lock is freed by `release()` or `release_if()`, which covers booking, cancel and invite flows; `release_if(token, notify=False)` is used when a flow expired untouched, so no unprompted menu. `Reply.ends_conversation` (kind `done` or `cancelled`, or `menu_after`, set for `Not now`) makes `send_reply` schedule the menu. `MenuService` sends one menu per user at a time (an asyncio lock) and a scheduled menu re-checks the skip rules right before sending. The admin DJ deletion frees its lock with `notify=False`. Show lineup takes no lock, so it never triggers one.
- Importing a backup of an older revision (`bot/backup/restore.py`, `_load_old_revision`): when the file's `alembic_revision` is one of ours and older than the running one, the import writes the safety export, runs `alembic downgrade <file revision>` (one transaction), checks the schema and writes the rows, then `alembic upgrade head` (one transaction), which migrates the rows. A failure before the data is in goes up again and writes the safety export back (`IMPORT_MIGRATE_FAILED`); if even that fails the bot stays paused (`IMPORT_MIGRATE_STUCK`) and a restart runs the migrations. Newer or unknown revisions are still refused. Alembic runs through `bot.db.pool.run_alembic`. Every migration must therefore keep a working `downgrade`. `tests/pg/test_old_backups_and_migrations.py` covers this for every revision (the list comes from the migration files, so a new migration is included automatically): data added at an old revision survives the upgrade to head, every old backup imports and migrates, each migration steps down and up on its own, and the failure paths leave a usable database. It needs the real-SQL database and takes about 10 minutes.
- Accepted limits of the tidy-up: `last_message_id` is cache-only, so it can lag only for messages written while the bot was disconnected, until it reconnected (then the old post is deleted although someone wrote); a person writing in the few milliseconds between the check and the send is not noticed. Rows of a DJ channel that is no longer configured stay (orphaned, harmless). The DJ Management channel is not affected (its lineup message is edited, see below).
- The last posted snapshot is stored in the config key `lineup_post_snapshot`. The keys `lineup_snapshot`, `lineup_last_post_date` and `lineup_post_message_id` are obsolete and unused.
- `/post lineup update [event]` calls `LineupService.post_event()`; the `event` option autocompletes `future_events()`.
- Post format (`lineup_title()`, `lineup_text()`): the embed title is `🎶 {event name} · {Weekday} {DD.MM.YYYY}` (the event ID without its leading date, cut to fit 256 characters). The description starts with a summary (`4 of 6 slots booked · 2 open`, or `All 6 slots booked`), a blank line, then one line per slot: `**{HH:MM}**  🎧 {DJ name}` or `**{HH:MM}**  ▫️ *open slot*`. DJ names go through `_safe_dj_name`; the description is capped at 4096 characters. The footer is the event ID.
- `Show lineup` in a DM (`FallbackMenuView.show_lineup`) answers with the upcoming event and, when there are several upcoming events, `LineupEventPickView`: one button per event (at most 5, labelled with the date), the shown one highlighted. A button edits the message it sits on, so the buttons stay. Custom ids are positions (`lineup_pick:0` to `lineup_pick:4`), registered once in `bot/main.py` with `all_buttons=True`; a press reloads the events and takes the one at that position, so the buttons also work after a restart.
- The setup wizard (Automatic invites step) stores `outreach_enabled` (`true`/`false`, default on). Only the outreach loop reads it (`parse_outreach_enabled`); nothing else changes when it is off.
- Texts: all user-facing strings live in `bot/ui/texts.py`, with the formatters `fmt_date`, `fmt_time` and `slot_phrase`. To add one, define a constant (or a small function for parameterized text) there, import `texts` where it is used, and cover it in `tests/test_texts.py`. Do not inline new user-facing strings in cogs, services or views.
- No em dash (U+2014) in user-facing strings. `tests/test_no_em_dash.py` parses `bot/**/*.py` and fails on any string constant with one, except docstrings and arguments of `debug`/`info`/`warning`/`error`/`exception`/`critical` calls. Docstrings and comments may keep it.

### Commands

Registered by `AdminCog` (`bot/cogs/admin.py`) and `SetupCog` (`bot/cogs/setup.py`); all are admin only (`ADMIN_USER_IDS` plus added admins). `AdminsCog` (`bot/cogs/admins_cog.py`) adds `/admins add user`, `/admins remove user` and `/admins list`, owners only (`ADMIN_USER_IDS`).

```
/setup
/config show
/create event date:<YYYY-MM-DD> [slots] [start] [slot_minutes] [genre] [location] [note]
/edit event event [genre] [location] [note] [clear]
/schedule show
/schedule default field [value]
/schedule apply
/schedule rule add field value every start
/schedule rule edit rule [value] [every] [start]
/schedule rule remove rule
/post lineup update [event]
/book dj slot event slot [dj | guest]
/cancel dj slot event slot [dj | guest]
/cancel event event
/dj add name link [backup_link] [vrchat_name] [logo] [discord_user]
/dj edit dj [name] [link] [backup_link] [vrchat_name] [logo]
/dj remove dj
/list all djs
/automatic dj booking dj enabled
```

Removed: `/status`, `/intervene` (start, send, end), `/admin ...`, `/config set ...`, `/add-event`, `/lineup post`, `/list-djs`. There is no command to change a single setting; `/setup` is the only way, and `/config show` is read only. The config keys `outreach_max_contacts` and `event_recurrence_cron` were never read and are gone from the code and from `/config show`; stored values stay in the table and are ignored.

### Cog wiring (`_add_cog`)

disnake 2.12 does not set `cog.bot`. `main.py` adds every cog through `_add_cog(bot, cog)`, which sets `cog.bot = bot` before `bot.add_cog(cog)`, because the cog loops start in `cog_load` and read `self.bot`. Register new cogs the same way, never with a bare `bot.add_cog`. `tests/test_cog_bot.py` covers it.

### Activation gate

- `bot_active` (config key, per guild) is `"true"` once `/setup` was activated. `bot/services/activation.py`: `is_active(db, guild_id)` returns the flag when stored; with no flag it returns true when both `dj_channel` and `log_channel` are stored (servers set up before the wizard keep working). `set_active()` writes the flag. `bot_is_active(bot, db)` checks the first guild and is for code that runs in DMs.
- Idle behaviour: the poll loop, reminder loop, outreach loop and `LineupService.refresh()` return early. `OnboardingCog` sends no onboarding DM and answers a DM with `IDLE_REPLY`, without logging it. DJ-facing views subclass `GatedView` (`bot/ui/gates.py`), whose `interaction_check` answers with `IDLE_REPLY` privately while idle. `AdminCog.cog_slash_command_check` answers `ADMIN_IDLE_REPLY` for every command except those in `IDLE_EXEMPT_COMMANDS` (`setup`, `config show`); autocompletes return nothing while idle. `SetupCog` has no gate.
- On the first `on_ready` of a process, `notify_admins_if_idle()` in `main.py` DMs every admin (`STARTUP_ADMIN_DM`) when the bot is idle. Failures are logged as warnings.

### Time zone

`bot/timezone.py` holds one process-wide zone (default `Europe/Berlin`, config key `timezone`). `get_timezone()`, `set_timezone(name)` (raises `ValueError` for unknown names), `local_now()`, `local_today()`, `is_valid_timezone()`, `load_stored_timezone(db, guild_id)` (called in `on_ready`, once guilds are known; an invalid stored value keeps the default). The wizard calls `set_timezone` when it saves.

An event's `Start` is a local wall-clock `HH:MM`; `EventInfo` in `bot/notion/models.py` builds timezone-aware datetimes from it with `get_timezone()`, so 21:00 stays 21:00 across the daylight saving change. Use `local_today()` for "today" (lineup `upcoming_event`, replenishing, event read filters, `/create event` validation) and never `datetime.utcnow()` or a bare `date.today()`. The bot sends no Discord timestamps (`<t:...>`); times are plain text.

### Replenishing events

`replenish_events(db, notion_client, guild_id, events)` in `bot/services/event_replenish.py` creates scheduled events until the number of active scheduled events equals `event_lookahead_count` (default 6). It returns 0 when `event_schedule_enabled` is `"false"` (missing means on). Manual events (table `manual_events`, written by `/create event`; helpers in `bot/db/manual_events.py`) are removed from the list first: they neither count nor move the base date. Everything else, including events added directly in Notion, counts as scheduled. Active means `date >= local_today()` and status not `Done` or `Canceled`. With a valid `first_event_date` (`YYYY-MM-DD`, today or later) and no scheduled event dated today or later, the first new event gets that date and the next ones follow at `+ event_interval_days` (default 7); otherwise new dates are `max(latest scheduled date, today) + interval * i`. The key is cleared once used or found unusable. Events are named `YYYY-MM-DD Rave Night`, with the shape from `default_event_shape()`. A date held by a known manual event is skipped and the next date is used (at most `missing + 52` dates are tried); any other existing event ID uses up its date without a creation, which keeps a stale event list from over-creating. A module lock serializes the poll loop (`EventsCog`) and the wizard. Because Done and Canceled events do not count, marking an event Done or Canceled in Notion makes the next poll create a replacement.

### Schedule rules, hand markers and local times

- **Pure logic** (`bot/services/schedule_rules.py`): `Rule`, `rule_hits`, `resolve` (a rule value for a date, else the default), `conflicts` (two rules for the same field that can hit the same schedule date; off-grid anchors never collide), `reanchor`. Rules count schedule dates, so `anchor` and `every` are in units of `event_interval_days`; changing the interval re-anchors the rules (`ScheduleService.check_reanchor`, `reanchor_rules`).
- **`ScheduleService`** (`bot/services/schedule.py`, `ctx.schedule_service`): `upcoming_dates` (existing scheduled events plus projected dates), `add_rule` / `edit_rule` / `remove_rule` (validation, `RuleConflict`, under `replenish_guard`), `set_default`, `resolved`, `pending_changes`, `apply(guild_id, picks)` (recomputes at press time and locks each event row; `picks=None` means all), `set_event_values` (the one write path for genre, location and note; `UNSET` leaves a field alone). Value limits are in `_LIMITS`. `load_resolver` is used by `replenish_events`; code without a bot (Notion intake, import, rebuild) uses `schedule_guild_id` and `resolver_without_bot`.
- **Hand markers.** `events.genre_by_hand` and `location_by_hand` mean "a rule or default never overwrites this". They are set by `set_event_values`, `/create event` and a Notion edit; clearing a value removes the marker and gives a scheduled event the resolved value. A Notion value that equals what the bot mirrored is not a hand edit (no marker). After a backup import or rebuild `recompute_hand_markers` sets the markers from the stored values. Rules and defaults never touch events in `manual_events`.
- **Changes** go through `changed(ctx, "event", uid)`; rule writes are mirrored by the Bot-data poll. Notion has a new `Location` property on the Events database (added by `ensure_schema`). Admin page: `bot/web/schedule_page.py` (`SchedulePageService`, `ctx.schedule_service` plus the first guild) and the `/schedule*` routes in `bot/web/app.py`.
- **Timestamps.** `texts.ts(moment, style)` builds Discord timestamps (`<t:epoch:style>`); `texts.slot_time`, `slot_phrase` and the lineup use it so each reader sees their own time zone. They are built only in `bot/ui/texts.py`; `tests/test_timezone.py::test_discord_timestamps_are_built_only_in_texts` fails when other modules build one. Places Discord does not render (embed titles, the admin page, Notion, logs) use `texts.plain_time` (local `HH:MM`) or the plain date. Button labels contain only numbers (`texts.slot_button`, `event_button`, booking number), never a time or date.
- Migration `0019` adds `events.location`, the hand markers and `schedule_rules`.

### Setup wizard

- `/setup` (`SetupCog`) posts one message with `SetupWizardView`. Steps (`STEPS` in `bot/ui/setup_wizard.py`): `welcome`, `dj_channel`, `log_channel`, `roles`, `slots`, `start_time`, `slot_length`, `schedule`, `interval`, `first_date`, `lookahead`, `default_genre`, `default_location`, `rules`, `timezone`, `invites`, `management_channel`, `booking_rules`, `summary`. `VALUE_STEPS` maps each value step to its config key (`booking_rules` is not a value step: it has its own toggle buttons and popup); all of them share one dropdown (`setup:value`) and one "Other" button (`setup:other`) with the options and parsing in `bot/ui/setup_options.py` (`dropdown_options`, `parse_choice`). While `event_schedule_enabled` is false, `scheduled_step_keys()` (`interval`, `first_date`, `lookahead`) are skipped by `next_step` and not counted. To add a step, insert its key before `summary`, add a branch in `render_step` and its controls to the view.
- The message carries no state. The view is persistent (registered in `main.py` through `registered_views()`), and every press reads the draft row found by the message id.
- Table `setup_drafts` (migration `0011`): `guild_id` (primary key, so one wizard per guild), `message_id`, `channel_id`, `step`, `data` (JSONB with the chosen values), `updated_at`. Access in `bot/db/setup_drafts.py` (`get_draft`, `save_draft`, `delete_draft`). Nothing reaches the `config` table until the last step.
- Activate (`activate_from_draft`): under a per-guild lock it requires both channels and both roles, writes all settings (`dj_channel`, `log_channel`, `dj_role_id`, `invites_role_id`, `slots_per_event`, `default_start_time`, `slot_duration_minutes`, `event_interval_days`, `event_lookahead_count`, `event_schedule_enabled`, `first_event_date`, `timezone`, `outreach_enabled`, `outreach_days_before`, `outreach_cooldown_hours`, `slot_limit_enabled`, `slot_limit_max`, `cancel_cutoff_enabled`, `cancel_cutoff_minutes`), sets `bot_active`, runs `replenish_events` and `LineupService.refresh()` exactly once, then deletes the draft. Save (`save_from_draft`, used on a re-run, `data["rerun"]`) only writes the settings.
- Re-run: with an active bot `/setup` shows a `Change settings` button; the draft is prefilled from the stored values (`prefilled_data`). If the wizard message was deleted, `/setup` posts it again at the saved step.
- Roles: `bot/services/roles.py` `get_dj_role()` and `get_invites_role()` read `dj_role_id` and `invites_role_id`. Only when a key is missing they fall back to the role names `DJ` and `Active Booking`; a stored id whose role is gone returns `None`.

### Booking rules

`bot/policy.py` holds both rules (slot limit per DJ and event, cancellation cutoff). They live in the `config` table under four keys per guild: `slot_limit_enabled` (default on), `slot_limit_max` (default 2, range 1 to `MAX_SLOTS`), `cancel_cutoff_enabled` (default on), `cancel_cutoff_minutes` (default 60, range 0 to 10080). A missing, unreadable or out-of-range value falls back to the default of that field, so an existing install enforces both rules after the update.

- `BookingRules` (frozen dataclass), `load_rules(db, guild_id)` (never raises), `current_rules(db, bot)` (first guild of the bot, defaults without one), `save_rules`, `parse_rules_form` (admin page validation).
- Pure checks: `slots_held`, `at_slot_limit(event, user_id, rules)`, `cancellation_closed(event, now, rules)` (measured from `event.slot_start(0)`), plus the texts `slot_limit_text`, `no_event_below_limit_text`, `cancellation_closed_text` and `rules_summary` (used by the wizard summary and `/config show`).
- `utc_now()` is the clock hook: tests monkeypatch `bot.policy.utc_now`; code calls `policy.utc_now()` through the module.

Enforcement (rules are read again at each action, nothing is cached):
- `acquire_slot_lock(pool, event_id, slot_index, user_id, max_per_dj=None)` in `bot/db/bookings.py` is the authoritative slot check. After the slot row lock it takes a transaction-scoped advisory lock on (event, DJ) (`pg_advisory_xact_lock`), counts the DJ's other slots of that event and raises `DjSlotLimitReached` at or above `max_per_dj`, before anything is written. The advisory lock serializes two parallel claims of different slots by the same DJ. `None` runs no count.
- `BookingConfirmView._handle_confirm` passes `max_per_dj` (only when the rule is on) and answers `DjSlotLimitReached` with `slot_limit_text`. `prepare_booking` drops events at the limit from the picker and returns `no_event_below_limit_text` when none are left. The outreach accept path (`bot/ui/views.py`) and the invite candidate filter in `bot/services/outreach.py` use `at_slot_limit`.
- `CancellationView` and `CancelConfirmView` check `cancellation_closed` on choose and again on confirm.

Admin paths pass no limit on purpose: `AdminBookingService.book_slot_override` (uses `force_slot_holder`), the Notion intake call `acquire_slot_lock(...)` in `bot/sync/intake.py` (no `max_per_dj`), and `cancel_slot_override` (no cutoff check). Admins and hand edits must be able to correct a lineup.

Wizard step: `booking_rules` (before `summary`). Ids `setup:slot_limit_toggle`, `setup:cutoff_toggle`, `setup:rules_change` (popup `setup_rules:<message id>`, parser `parse_rules_fields`). The four rule keys are kept in the draft data; `draft_rules` fills missing keys from the stored values. `STEPS_V5_MARKER` (`steps_v5`) in the draft data moves a draft that was open at the old summary step to the new summary once. `_write_all` stores the rules on Activate and Save.

Admin page: `GET/POST /booking-rules` in `bot/web/app.py`, `render_booking_rules` in `bot/web/pages.py`, `AdminPageService.booking_rules()` and `set_booking_rules(raw)` in `bot/web/service.py`. Checkboxes count as on when their key is present in the form; invalid input answers 400 with the typed values.

Real-SQL test: `tests/pg/test_slot_limit.py` covers the limit with concurrent claims. Run it with `RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot@localhost:55432/ravebot_test uv run pytest -m pg tests/pg/test_slot_limit.py` (database setup as in section 3).

### DJ management lineup message

- Service: `ManagementLineupService` in `bot/services/management_lineup.py`, held in `ctx.management_lineup_service`. Rendering (pure functions) and the button view are in `bot/ui/management_lineup.py`; the repository is `bot/db/management_lineup.py`. Config key `management_channel` (`MANAGEMENT_CHANNEL_KEY`), optional; unset means no new posts.
- Table `management_lineup_messages` (migration `0020`): `event_id` (primary key, the Events row id), `channel_id`, `message_ids BIGINT[]`, `content_hash`, `updated_at`. One row per event; the channel is stored with the row, so a changed `management_channel` affects only events without a row.
- Hook: `notify_management(ctx)` (module function, never raises) calls `notify_changed()`. It is called next to the existing lineup rechecks: booking, cancel and admin booking paths, DJ conversation and profile edits (`dj_admin`, `dj_conversation`, `/dj` paths in `admin.py`), event cancel, schedule changes, and the Notion intake (`bot/sync/intake.py`). `notify_changed()` merges calls and runs `refresh()` after `MANAGEMENT_DEBOUNCE_SECONDS` (10 s). No-op while the bot is idle.
- `refresh(events=None)` runs under a lock and never raises. Per event: no record posts when the channel is set, the event is not finished, its date is today or later and it is fully booked. A record with ids is re-rendered (DJ data read at render time) and edited only when `content_hash` differs; Done events are left alone, Canceled ones are edited to the canceled text. The view is on the first message only.
- Suppressed record: a row with empty `message_ids` and hash `""` means "fully booked when the admin chose Only new". Nothing is posted for it. The row is deleted when the event is not finished and not fully booked, so the next time it fills, it posts. `eligible_backfill_events()` excludes events with any row.
- Message count changes: more chunks are posted into the stored channel; fewer chunks edit the surplus messages to `MGMT_LINEUP_SURPLUS` and keep their ids (reused on growth). Edits go through `channel.get_partial_message(id).edit(...)`, so the bot needs no Read Message History permission. Any missing lineup message (`NotFound`, first or continuation) re-posts the whole lineup in the configured channel and replaces all ids; ids sent earlier in the same pass are kept, and every surviving old message is edited (best effort) to `MGMT_LINEUP_MOVED` with `view=None`, so only one lineup stays live. Old messages are never deleted. Without a configured channel nothing is re-posted and the ids are kept. A hand delete is noticed at once: `on_raw_message_delete` and `on_raw_bulk_message_delete` (registered with `bot.add_listener` in `bot/main.py`) look the ids up with `get_record_by_message`, set the record's hash to `""` (ids and channel kept) and call `notify_changed()`, so the next refresh re-posts even when the content did not change; untracked ids are ignored and the listeners never raise. A channel counts as deleted only when it is missing from the cache, the bot is ready and `fetch_channel` answers `NotFound` (`_resolve_channel`); before the bot is ready, or on any other lookup error, the state is unknown and nothing is edited, re-posted or reported until a later refresh. If the stored channel is deleted and a different channel is configured, the lineup is re-posted there (the lost channel is still reported once); with no other channel configured it is only reported. After a partial failure the ids sent so far are stored with hash `""`, so the next refresh completes by editing and never posts a second lineup. An unusable channel (missing, `Forbidden`) is reported once per channel and reason per process through `log_attention`.
- Button: `ManagementInfoView` has a static custom id `mgmt:info`, registered once in `bot/main.py`. The event is resolved from the clicked message id with `get_record_by_message` (`$1 = ANY(message_ids)`), so it works after a restart. Order: maintenance gate, `ctx.is_admin` (a non-admin gets an ephemeral refusal and no data is loaded), record lookup, DMs via `inter.author.send` (header, then one per DJ, logo attached; guests are included, their Discord name is skipped).
- Backfill: `backfill(mode, channel_id=None)` with `"all"` (posts eligible events in date order) or `"new"` (writes suppressed records). Ordering rule in the setup wizard: `"new"` runs before `management_channel` is stored (and, on activation, while `bot_active` is false) so no refresh can post those events first; `"all"` runs after the channel is stored (on activation after `set_active`, on save after the settings are written). Backfill errors are logged and never fail activation or save. The answer is a draft key (`management_backfill`) and is never stored in config.

### Event announcements

- Tables (migration `0023`): `announcement_rules` (`id`, `guild_id`, `days_before` with CHECK 0 to 365, `post_time`, `created_at`) and `announcement_posts` (`id`, `event_id`, `kind` `general|start`, `rule_id` nullable, `channel_id`, `message_id`, `state`, `digest`, `created_at`). The unique index `announcement_posts_unique` is on `(event_id, kind, COALESCE(rule_id, 0))`, so the start post (no rule) is unique per event. There is no foreign key to the rules: deleting a rule keeps its posts. States: `claimed`, `posted`, `gone` (skipped or deleted by hand, never retried), `ended` (start post finished). Repository: `bot/db/announcements.py` (`claim_post` is an `INSERT ... ON CONFLICT DO NOTHING RETURNING`, so only one caller gets the post; `get_post` matches a NULL rule with `COALESCE`; `release_claim` deletes a `claimed` row).
- Config keys (`bot/services/announcement_settings.py`): `announcement_channel`, `announcement_general_enabled`, `announcement_start_enabled`, `announcement_general_template`, `announcement_start_template` (empty or missing means the default template), `announcement_general_since` (moment the general kind was switched on; `set_enabled` writes it only on a change). `is_rule_due` decides a rule per event: post time after the later of rule creation and `since`, and the event not started.
- Templates: `bot/services/announcement_template.py` (pure): `PLACEHOLDERS`, `DEFAULT_TEMPLATES`, `render`, `check_template` (used by the admin page for Check and Save), `event_end`.
- Service: `AnnouncementService` in `bot/services/announcements.py` (`ctx.announcement_service`, built in `bot/main.py`). `tick()` never raises and is serialised by a lock. Cog `bot/cogs/announcements_cog.py` runs `tick()` in a one-minute `tasks.loop` (skipped in maintenance and while idle) and holds the `/announcements` commands.
- Tick order: (1) release leftover `claimed` rows (stale after a crash); (2) load settings, rules, events; (3) posts of events that no longer exist are canceled (edited to the canceled text); (4) running start posts of events that left the upcoming list are added so they get their ended rendering; (5) per event: canceled events cancel their posted rows and stop; otherwise, unless finished, every plan without a post row (each due rule, plus the start kind) is claimed, rendered and sent, only when a channel is set; then every `posted` row is re-rendered and edited when its digest changed, and a start post is set to `ended` once the event has ended.
- Claiming: a post is claimed before sending (`claim_post`), then `mark_posted` stores message id and digest. A failed send calls `release_claim`, so the next tick retries. A restart cannot post twice because the row exists. A skipped plan (lineup incomplete) is stored as `gone`. A message deleted by hand (`NotFound` on edit) becomes `gone`.
- Poster hook: all sends go through `AnnouncementService._send(channel, text, file=None)`; a later poster feature passes a `disnake.File` there. Nothing else is needed on the posting path. Mentions use `ALLOWED_MENTIONS` (roles only, never `@everyone`).
- Wizard: step `announcement_channel` (before `summary`); `STEPS_V6_MARKER` (`steps_v6`) in the draft data moves a draft that was open at or after `booking_rules` one step on, once, like the earlier markers. The channel is stored on Activate and Save.
- Admin page: `GET /announcements`, `POST /announcements/settings`, `/announcements/rules/add`, `/announcements/rules/{id}/edit|remove`, and `POST /announcements/templates/{kind}/check|save` (kinds from `ANN_KINDS`; Reset to default posts an empty text to `save`). Service methods in `bot/web/service.py` (`announcements`, `save_announcement_settings`, `add_rule`, `edit_rule`, `remove_rule`, `check_template`, `save_template`). The four template routes (check, save per kind) get a body limit of `MAX_TEMPLATE` (64 KiB) in `bot/web/app.py`: a BMP character of 3 UTF-8 bytes url-encodes to 9 bytes, and the textarea `maxlength` (`TEMPLATE_MAXLENGTH`, 6000) keeps the worst case below the limit. The settings save skips the permission check when the posted channel equals the stored one.
- Tests: `tests/test_announcement*.py`, `tests/cogs/test_announcements_cog.py`, `tests/web/test_announcements_routes.py`; real SQL in `tests/pg/test_announcements_repo.py` (database setup as in section 3).

### Notion setup

- The bot uses the Notion API version `2026-03-11` (databases and data sources are separate; views are created via `POST /v1/views`). The only Notion settings are `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`; both or neither.
- At startup, before Discord connects, `main()` runs `ensure_schema(api, db, 0, parent_page_id)` and then `ensure_views(api, db, 0, ids)`. The data source and database ids are stored in the `config` table under `guild_id = 0` (global). `ensure_schema` creates the Events, DJs, Slots and Bot data databases under the parent page if none are stored (adopting existing ones first, see above), adds missing properties, raises `NotionDatabasesMissing` if a stored database is gone (unless an admin approved a new set: config key `notion_recreate_approved`, set by the admin page through `ctx.notion_recover`, used up once; `main()` keeps running without Notion until then and `BotContext.attach_notion` hands the new client to the services), and raises `NotionSchemaError` on a property with the wrong type. `ensure_views` creates only missing views and sets a marker once all three exist.
- If `ensure_schema`/`ensure_views` raise `NotionSchemaError` or `NotionWriteError`, the bot closes the pool and the Notion client and exits with a message that names the parent page sharing and both env vars.
- To find the Notion databases again after the stored ids are gone, open the parent page named by `NOTION_PARENT_PAGE_ID`: the databases Events, Slots, DJs and Bot data are its children, and `ensure_schema` re-adopts them by title.
- Tests use `tests/notion/fake_notion.py`, an in-memory Notion behind an httpx transport, so no test talks to the real API.

---

### Admin registry and admin page

- `bot/services/admin_registry.py`: `AdminRegistry` holds the owners (`ADMIN_USER_IDS`, fixed) and the added admins (table from migration `0014`, loaded at start). `main` creates it as `ctx.admins`.
- `BotContext.is_admin(user_id)` is the single check for every admin command and gate; `ctx.admin_ids()` lists owners plus added admins (startup DM).
- `bot/web/`: `start_admin_page(ctx, bot)` and `stop_admin_page(runner)` in `__init__.py`; `main` starts the page right before `bot.start(...)` and stops it first in the `finally` block. No `ADMIN_PAGE_PASSWORD` means no page; a bad port or a port in use logs an ERROR and returns `None`, the bot keeps running. It binds `0.0.0.0` (`web.TCPSite`), `Dockerfile` has `EXPOSE 8080`.
- Schedule page: `service.schedule` (`SchedulePageService`, `bot/web/schedule_page.py`) backs `GET /schedule`, `POST /schedule/defaults`, `/schedule/rules/add`, `/schedule/rules/{id}/edit|remove`, `/schedule/events/{uid}` and `/schedule/apply`. Every POST is refused in maintenance. "Apply to all" sends the pairs the page showed (hidden `shown` fields), so no change the admin did not see is written (the service recomputes and writes only those still pending).
- `AdminPageService` (`bot/web/service.py`) parses the typed id and delegates `add_admin`/`remove_admin` to `AdminManagementService` with the actor `"the admin page"`.
- `AdminManagementService` (`bot/services/admin_management.py`) is the single code path for adding, removing and listing admins; the `/admins` cog and the admin page both use it. It applies the rules (owners cannot be removed, bot accounts cannot be added), saves, and writes `log_admin_added`/`log_admin_removed` with the `actor`. Results are `AdminChange` values.
- `AdminsCog` (`bot/cogs/admins_cog.py`, `/admins add|remove|list`): `cog_slash_command_check` runs the shared `AdminCog` gate (server only, admin, idle rule) and then `ctx.admins.is_owner`; a non-owner raises `NotOwner` (a `CheckFailure`). Its own `cog_slash_command_error` answers `NotOwner` with `OWNER_ONLY`, other check failures with the normal permission refusal, and anything else with a generic error; all replies are ephemeral. It fails closed when `ctx.admins` is missing.
- Security measures: login throttle (`LoginThrottle`, also closes the parallel attempt bypass) plus a fixed delay on a wrong password; stateless signed session cookies (the signing key is random per process, so sessions end at restart; logout clears the cookie only); CSRF token on every POST; security headers on every response (CSP, `X-Frame-Options`, `nosniff`, no-store); all user-controlled text is HTML-escaped. There is no HTTPS, so the page is for the local network only.

## 5. Manual Docker Build & Release

Releases are built by GitHub Actions (see [section 7](#7-automated-releases)). Build by hand only for a fork or a quick test.

The Dockerfile uses a two-stage build:
- **`builder` stage** - installs `uv` and resolves all runtime dependencies into `.venv`
- **`runtime` stage** - `python:3.13-slim` with `.venv` copied in, running as non-root user `ravebot`
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

### DJ onboarding conversation

`DjConversationService` (`bot/services/dj_conversation.py`) runs the onboarding and the profile edits as DM conversations. State lives in `dj_conversations` (one row per user: `kind`, `step`, `data`, `logo_image`, `message_id`; migration `0013`). Handlers return a `Reply` (text, kind, optional logo bytes); `bot/ui/dj_profile.py` turns it into a message with `send_reply` and the shared persistent views registered in `main.py`.

- Routing: `OnboardingCog.on_message` checks `get_conversation` first. With an open conversation it calls `handle_message`, sends the `Reply` and returns: no log entry, no menu. Without one, behaviour is unchanged (log entry, menu). A service exception is logged and answered with `GENERIC_ERROR` (a failing send is only logged). A failing `get_conversation` and `handle_message` returning `None` are logged and fall back to the menu and log entry. `Not now` on the welcome DM calls `decline`, which deletes an open onboarding conversation (edit conversations stay).
- Stale-button rule: every conversation button is checked against `message_id`, the id of the last question message. A button of an older message gets `STALE_BUTTON` and changes nothing. `record_message_id` stores the id after the message is sent.
- Logo: `bot/services/logo.py` accepts one attachment from `cdn.discordapp.com` or `media.discordapp.net`, PNG, JPEG or MPO (first frame), up to 5 MB, 4096 px per side, scales it to at most 1024 px on the longest side (LANCZOS, aspect ratio kept) and re-encodes it with Pillow (a new dependency) to a metadata-free PNG. A result above 4 MB is refused (`LOGO_BAD`). A module-level semaphore lets only one logo be decoded at a time. The bytes are stored in `djs.logo_image` (and `dj_conversations.logo_image` while the conversation is open). Discord attachment URLs expire, so the stored bytes are the source.
- Notion: the DJ write only queues the DJ (`sync_dj` calls `changed(ctx, "dj", key)` and never touches Notion). The `MirrorWorker` writes the DJ row with `NotionClient.mirror_dj` and uploads `logo_image` through `NotionApi.upload_file` (`POST /file_uploads`, then `/file_uploads/{id}/send`) into the `Logo` files property while `logo_synced` is false, and marks it with `mark_logo_synced_if_unchanged` (a logo changed in the meantime stays unsynced). A new row always gets the logo. If the upload fails (or the row write rejects the file), the row is written without `Logo` and `DjLogoUploadError(page_id)` is raised; the worker records the page id and leaves `logo_synced` false, and `queue_unsynced_logos` queues those DJs again at the next start. `send_reply` sends a message once more without the logo file when Discord rejects it, and still records its id. Stream links are written as URL properties.
- Per-user work runs under the user lock, so two messages from one DJ are handled one after the other.

### Admin log channel

`bot/log.py` holds `AdminLogService`. Every `log_*` method builds a `LogEntryEmbed` (title, plain text, footer `RaveBot`, optional `What to do` field), escapes user, DJ and Notion text, sends with `AllowedMentions.none()` and never raises. With no log channel configured or reachable the entry goes to the Python logger (warning).

API:

- `log_admin_added(name, user_id, actor=None)`, `log_admin_removed(name, user_id, actor=None)`: admin list changes. `actor` is the owner's display name for `/admins`, or `the admin page`; both are escaped.
- `log_booked(dj_name, event, slot_index, via)`, `log_cancelled(...)`, `log_changed_by_organizer(...)`, `log_changed_in_notion(...)`: booking changes. Organizer commands emit only `log_changed_by_organizer`.
- `log_fully_booked(event)`, `log_new_dj(dj_name, vrchat_name)`.
- `log_user_message(user, text, attachment_names, dj_name, view)`: returns the posted message so the caller can store its id.
- `log_attention(title, what, todo=None)`: a red critical entry.
- `post_error(error_code, trigger, action_taken, manual_action)`: maps the codes in `_ERROR_TITLES` to `log_attention`. `ERR_SLOT_CONFLICT` and `ERR_BUTTON_TIMEOUT` only reach the logger.

| Entry | Title | Colour |
|---|---|---|
| Booked | `Booked` | `SUCCESS_GREEN` |
| Cancelled | `Cancelled` | `MISSION_GOLD` |
| Changed by an organizer | `Changed by an organizer` | `MISSION_GOLD` |
| Changed in Notion | `Changed in Notion` | `MISSION_GOLD` |
| Event fully booked | `Event fully booked` | `SUCCESS_GREEN` |
| New DJ | `New DJ` | `SUCCESS_GREEN` |
| User message | `Message from <name>` (plus the DJ name if it differs) | `DEEP_SPACE_BLUE` |
| Critical | the title passed to `log_attention` | `IMPERIAL_RED` |

Adding an entry:

1. Add the title constant and the text function to `bot/ui/texts.py` and cover them in `tests/test_texts.py`.
2. Add a `log_*` method to `AdminLogService` that builds a `LogEntryEmbed` inside `_post_built(lambda: ...)`, with names passed through `_safe_dj_name` and free text through `texts.escape_text`.
3. Call it from the code path. Do not call it for button taps, bot replies or routine sync.
4. Test the exact wording and that mentions and markdown stay literal.

Fully booked: `bot/services/fully_booked.py` (`check_fully_booked`, `recheck_fully_booked`) runs in the poll cycle and after every booking or cancellation. `mark_announced` is an atomic insert, so two paths filling at once announce once; `clear_announced` runs when a slot opens.

Reply button: `LogReplyView` and `ReplyModal` in `bot/ui/log_reply.py`. The view is persistent (`bot.add_view` in `main.py`), checks that the clicker is an admin, looks up the user in `log_replies` and sends the DM. A missing row gives a private "cannot find the user" note.

Tables (created by Alembic migrations):

| Table | Migration | Columns | Purpose |
|---|---|---|---|
| `fully_booked_events` | `0009` | `event_id` (bot event key, primary key), `announced_at` | Events already announced as fully booked. Restart safe. |
| `log_replies` | `0010` | `message_id` (primary key), `user_id`, `created_at` | Maps a `Message from` entry to the user the Reply button answers. |
| `setup_drafts` | `0011` | `guild_id` (primary key), `message_id`, `channel_id`, `step`, `data` (JSONB), `updated_at` | The open `/setup` wizard of a guild. Deleted on Activate or Save. |
| `manual_events` | `0015` | `page_id` (bot event key, primary key), `created_at` | Events created with `/create event`. The event scheduler ignores them. |

Migration `0012` drops the `interventions` table. The intervention flow (`/intervene`, `bot/services/intervention.py`) no longer exists: an organizer who wants to talk to a DJ uses the `Reply` button on a `Message from` entry or writes to the DJ directly.

---

## 6. Database Migrations

Migrations are managed with **Alembic** and run automatically at container startup (`alembic upgrade head` is called before the bot connects).

```bash
# Apply all pending migrations (runs automatically at startup - manual use for dev)
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

## 7. Automated Releases

Two GitHub Actions workflows live in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push to `main`, pull requests | `uv sync --frozen` and `uv run pytest -q`; a second job runs `tests/pg` against a PostgreSQL service |
| `release.yml` | push of a tag `vMAJOR.MINOR.PATCH` (or with a `-suffix`, a pre-release) | tests, builds the Docker image, pushes it to `ghcr.io/<owner>/ravebot`, creates the GitHub release |

All third-party actions are pinned to full commit SHAs (the version is in a comment), because the release job can write packages. `.github/dependabot.yml` opens a weekly pull request when a pinned action has a new release; review it like any dependency update. The release workflow refuses a tag that is not on `main`. The image gets the tags `X.Y.Z`, `X.Y` and `latest` (and `X` from major version 1 on); a pre-release tag (`v1.2.0-rc1`) only gets its own tag and no `latest`. The image is built for `linux/amd64`. The GitHub release text is the matching `## [X.Y.Z]` section of `CHANGELOG.md` plus the image name; without such a section it falls back to a pointer to the changelog.

### Making a release

1. Write the changes under `## [Unreleased]` in `CHANGELOG.md` while you work.
2. From a clean `main`: `scripts/release.sh 1.2.0`. It runs the tests, renames `Unreleased` to `[1.2.0] - <date>` (and adds a new empty `Unreleased`), sets the version in `pyproject.toml`, commits `Release v1.2.0` and creates the tag `v1.2.0`. Nothing is pushed. `--no-tests` skips the test run.
3. Push the commit and the tag: `git push origin main v1.2.0`. The workflow takes over.
4. Check the *Actions* tab. After it is green the release and the image exist. Unraid users update from the Docker tab.

### First time

- The first push creates the package `ravebot` as **private**. Make it public once: *GitHub → profile → Packages → ravebot → Package settings → Change visibility*. Without that, Unraid cannot pull the image without a login.
- The workflow needs no secrets: it uses the automatic `GITHUB_TOKEN` (permissions `packages: write` and `contents: write` are set in the workflow file).
- The first release of the rebuilt bot is `v1.1.0`: it continues the version line of the old repository `ravebot-legacy` (last release `v1.0.0`). Later versions: patch for fixes, minor for new features, major for changes that need action from the admin.

### README images

The images in `docs/assets` are rendered from `docs/assets/src` (and the admin page from the real page code) with `docs/assets/render.sh`. Set `CHROME` to a headless Chromium binary and run it from the repository root. Run it again after you change wording or the look of a screen.

### Unraid template

`unraid/ravebot.xml` is the Unraid template: repository `ghcr.io/m4xxx/ravebot:latest`, all variables, the admin page port and the icon. Keep it in line with the variables in `bot/config.py`.


## Guest DJs and the DJ list

`bot/services/dj_admin.py` (`DjAdminService`) adds, edits and deletes DJs for the admin page (`/djs` routes in `bot/web/app.py`) and the `/dj` commands. A guest has no Discord account, so the `djs` table (keyed by Discord user ID) stores them under a negative ID from `next_guest_id`; Discord IDs are positive. The Notion `Discord ID` text (the mirror) holds the same negative number. Every place that contacts DJs skips negative IDs: `dm_user` returns True without sending, the reminders skip them, and the outreach pool (`get_active_booking_djs`) only returns IDs above 0. Deleting a DJ cancels their bookings in upcoming events through `AdminBookingService.cancel_slot_override`, deletes the row and queues the Notion page for removal (the worker trashes it).
