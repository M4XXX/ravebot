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
# Edit .env: set DISCORD_BOT_TOKEN, NOTION_API_KEY, NOTION_PARENT_PAGE_ID, ADMIN_USER_IDS
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
│   │   ├── admin.py        # /config show, /create event, /post lineup update, /book dj slot, /cancel dj slot, /list all djs, /automatic dj booking
│   │   ├── booking.py      # DJ self-service booking (thin shell - logic in views.py)
│   │   ├── events.py       # Lineup post auto-update, event auto-creation
│   │   ├── onboarding.py   # DJ role detection, welcome DM, DM routing
│   │   ├── outreach_cog.py # Proactive outreach background loop
│   │   └── setup.py        # /setup (wizard entry point)
│   ├── db/                 # asyncpg repository layer
│   ├── web/                # Admin page (aiohttp): app.py routes and middlewares, auth.py, pages.py, service.py
│   ├── notion/             # Notion integration (API version 2026-03-11)
│   │   ├── api.py          # NotionApi: httpx wrapper, retry/backoff, pagination
│   │   ├── models.py       # Property names, typed EventInfo/SlotInfo/DjInfo, parsers
│   │   ├── schema.py       # ensure_schema(): creates/verifies Events, Slots, DJs
│   │   ├── views.py        # ensure_views(): Overview, Lineup, All DJs
│   │   └── client.py       # NotionClient: reads and writes events, slots, DJs
│   ├── services/           # Business logic
│   │   ├── activation.py   # is_active(), set_active(), bot_is_active()
│   │   ├── admin_booking.py
│   │   ├── fully_booked.py # check_fully_booked(): once-per-fill announcements
│   │   ├── dj_pages.py     # ensure_dj_page(): a DJ's row in the DJs database
│   │   ├── event_defaults.py # default start, slot length, slots per event (config)
│   │   ├── event_replenish.py # replenish_events(): keeps future open events ready
│   │   ├── lineup.py       # LineupService: posts, snapshot dedupe, upcoming_event
│   │   ├── notion_sync.py  # NotionSyncService: applies hand edits from Notion
│   │   ├── notion_sync_plan.py # plan_sync(): pure Notion-vs-Postgres planner
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
- Notion is the source of truth for slot assignments (one Slots row per slot, `DJ` relation). The PostgreSQL `slot_locks` table only guards against two DJs taking one slot at the same moment.

### Lineup posts, outreach switch and texts

- `LineupService.refresh()` (called by the Notion poll and by booking changes, debounced) computes `event_snapshot()` of the upcoming event (`upcoming_event()`: earliest event with `date >= local_today()` and status not Done or Canceled). If it differs from the stored snapshot it posts a new `Lineup Update` embed (`LineupUpdateEmbed`) with a `Book a slot` button (`LineupPostView`). Posts are never edited or deleted. If `dj_channel` is unset the snapshot is stored without posting.
- The last posted snapshot is stored in the config key `lineup_post_snapshot`. The keys `lineup_snapshot`, `lineup_last_post_date` and `lineup_post_message_id` are obsolete and unused.
- `/post lineup update [event]` calls `LineupService.post_event()`; the `event` option autocompletes `future_events()`.
- Post format (`lineup_text()`): `Event {Weekday} {DD.MM.YYYY}`, then `Slot {n}  {HH:MM}  {DJ name or OPEN SLOT}` with two spaces between parts. DJ names go through `_safe_dj_name`; the description is capped at 4096 characters.
- The setup wizard (Automatic invites step) stores `outreach_enabled` (`true`/`false`, default on). Only the outreach loop reads it (`parse_outreach_enabled`); nothing else changes when it is off.
- Texts: all user-facing strings live in `bot/ui/texts.py`, with the formatters `fmt_date`, `fmt_time` and `slot_phrase`. To add one, define a constant (or a small function for parameterized text) there, import `texts` where it is used, and cover it in `tests/test_texts.py`. Do not inline new user-facing strings in cogs, services or views.
- No em dash (U+2014) in user-facing strings. `tests/test_no_em_dash.py` parses `bot/**/*.py` and fails on any string constant with one, except docstrings and arguments of `debug`/`info`/`warning`/`error`/`exception`/`critical` calls. Docstrings and comments may keep it.

### Commands

Registered by `AdminCog` (`bot/cogs/admin.py`) and `SetupCog` (`bot/cogs/setup.py`); all are admin only (`ADMIN_USER_IDS`).

```
/setup
/config show
/create event date:<YYYY-MM-DD>
/post lineup update [event]
/book dj slot dj event slot
/cancel dj slot dj event slot
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

An event's `Start` is a local wall-clock `HH:MM`; `EventInfo` in `bot/notion/models.py` builds timezone-aware datetimes from it with `get_timezone()`, so 21:00 stays 21:00 across the daylight saving change. Use `local_today()` for "today" (lineup `upcoming_event`, replenishing, `list_events` filters, `/create event` validation) and never `datetime.utcnow()` or a bare `date.today()`. The bot sends no Discord timestamps (`<t:...>`); times are plain text.

### Replenishing events

`replenish_events(db, notion_client, guild_id, events)` in `bot/services/event_replenish.py` creates events until the number of active events equals `event_lookahead_count` (default 6). Active means `date >= local_today()` and status not `Done` or `Canceled`. New dates start at `max(latest date of all events, today) + interval * i` (`event_interval_days`, default 7), named `YYYY-MM-DD Rave Night`, with the shape from `default_event_shape()`. An existing event ID is skipped. A module lock serializes the poll loop (`EventsCog`) and the wizard. Because Done and Canceled events do not count, marking an event Done or Canceled in Notion makes the next poll create a replacement.

### Setup wizard

- `/setup` (`SetupCog`) posts one message with `SetupWizardView`. Steps (`STEPS` in `bot/ui/setup_wizard.py`): `welcome`, `dj_channel`, `log_channel`, `roles`, `events`, `invites`, `summary`. To add a step, insert its key before `summary`, add a branch in `render_step` and its controls to the view.
- The message carries no state. The view is persistent (registered in `main.py` through `registered_views()`), and every press reads the draft row found by the message id.
- Table `setup_drafts` (migration `0011`): `guild_id` (primary key, so one wizard per guild), `message_id`, `channel_id`, `step`, `data` (JSONB with the chosen values), `updated_at`. Access in `bot/db/setup_drafts.py` (`get_draft`, `save_draft`, `delete_draft`). Nothing reaches the `config` table until the last step.
- Activate (`activate_from_draft`): under a per-guild lock it requires both channels and both roles, writes all settings (`dj_channel`, `log_channel`, `dj_role_id`, `invites_role_id`, `slots_per_event`, `default_start_time`, `slot_duration_minutes`, `event_interval_days`, `event_lookahead_count`, `timezone`, `outreach_enabled`, `outreach_days_before`, `outreach_cooldown_hours`), sets `bot_active`, runs `replenish_events` and `LineupService.refresh()` exactly once, then deletes the draft. Save (`save_from_draft`, used on a re-run, `data["rerun"]`) only writes the settings.
- Re-run: with an active bot `/setup` shows a `Change settings` button; the draft is prefilled from the stored values (`prefilled_data`). If the wizard message was deleted, `/setup` posts it again at the saved step.
- Roles: `bot/services/roles.py` `get_dj_role()` and `get_invites_role()` read `dj_role_id` and `invites_role_id`. Only when a key is missing they fall back to the role names `DJ` and `Active Booking`; a stored id whose role is gone returns `None`.

### Notion setup and sync

- The bot uses the Notion API version `2026-03-11` (databases and data sources are separate; views are created via `POST /v1/views`). The only Notion settings are `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID`.
- At startup, before Discord connects, `main()` runs `ensure_schema(api, db, 0, parent_page_id)` and then `ensure_views(api, db, 0, ids)`. The data source and database ids are stored in the `config` table under `guild_id = 0` (global). `ensure_schema` creates the Events, DJs and Slots databases under the parent page if none are stored, adds missing properties, starts a fresh set if a stored database is gone, and raises `NotionSchemaError` on a property with the wrong type. `ensure_views` creates only missing views and sets a marker once all three exist.
- If `ensure_schema`/`ensure_views` raise `NotionSchemaError` or `NotionWriteError`, the bot closes the pool and the Notion client and exits with a message that names the parent page sharing and both env vars.
- `EventsCog._poll_notion` (every 5 minutes, first run once the bot is ready) runs `NotionSyncService.run_cycle()` (adopt hand entries, free hand removals, revert conflicts, create/trim Slots rows), then reads the events, auto-creates future events and updates the lineup post. A sync failure is posted to the admin log and does not stop the lineup update.
- Tests use `tests/notion/fake_notion.py`, an in-memory Notion behind an httpx transport, so no test talks to the real API.

---

### Admin registry and admin page

- `bot/services/admin_registry.py`: `AdminRegistry` holds the owners (`ADMIN_USER_IDS`, fixed) and the added admins (table from migration `0014`, loaded at start). `main` creates it as `ctx.admins`.
- `BotContext.is_admin(user_id)` is the single check for every admin command and gate; `ctx.admin_ids()` lists owners plus added admins (startup DM).
- `bot/web/`: `start_admin_page(ctx, bot)` and `stop_admin_page(runner)` in `__init__.py`; `main` starts the page right before `bot.start(...)` and stops it first in the `finally` block. No `ADMIN_PAGE_PASSWORD` means no page; a bad port or a port in use logs an ERROR and returns `None`, the bot keeps running. It binds `0.0.0.0` (`web.TCPSite`), `Dockerfile` has `EXPOSE 8080`.
- `AdminPageService` (`bot/web/service.py`) wraps the registry, the Discord lookups and the admin log entry for added admins.
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
- Notion: `NotionClient.upsert_dj` uploads `logo_image` through `NotionApi.upload_file` (`POST /file_uploads`, then `/file_uploads/{id}/send`) and sets the DJ's `Logo` files property. `sync_dj` passes `upload_logo=not logo_synced`, so unrelated edits leave `Logo` out of the update; a new row always gets the logo. If the upload fails (or the row write rejects the file), the row is written without `Logo` and `DjLogoUploadError(page_id)` is raised; `sync_dj` and `ensure_dj_page` store that page id and set `logo_synced = false`, so bookings keep working and `retry_unsynced_logos` sends the logo at the next start. `send_reply` sends a message once more without the logo file when Discord rejects it, and still records its id. Stream links are written as URL properties. Existing DJs keep their stored link.
- Per-user work runs under the user lock, so two messages from one DJ are handled one after the other.

### Admin log channel

`bot/log.py` holds `AdminLogService`. Every `log_*` method builds a `LogEntryEmbed` (title, plain text, footer `RaveBot`, optional `What to do` field), escapes user, DJ and Notion text, sends with `AllowedMentions.none()` and never raises. With no log channel configured or reachable the entry goes to the Python logger (warning).

API:

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
| `fully_booked_events` | `0009` | `event_id` (Notion page id, primary key), `announced_at` | Events already announced as fully booked. Restart safe. |
| `log_replies` | `0010` | `message_id` (primary key), `user_id`, `created_at` | Maps a `Message from` entry to the user the Reply button answers. |
| `setup_drafts` | `0011` | `guild_id` (primary key), `message_id`, `channel_id`, `step`, `data` (JSONB), `updated_at` | The open `/setup` wizard of a guild. Deleted on Activate or Save. |

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
| `ci.yml` | push to `main`, pull requests | `uv sync --frozen` and `uv run pytest -q` |
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
