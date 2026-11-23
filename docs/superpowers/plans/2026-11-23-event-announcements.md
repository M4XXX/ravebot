# Event announcements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two optional announcement types (general, by admin-defined rules; event start, live with the DJ playing now) posted to one announcement channel from editable templates, configured by setup, commands and the admin page.

**Architecture:** Settings live in the `config` table, rules and posted messages in two new tables. A pure template module renders and checks templates. One `AnnouncementService.tick()`, run every minute by a cog loop, decides what to post or edit from the event list; posts are claimed in the database first so a restart or two ticks never double post.

**Tech Stack:** Python 3.13, disnake, asyncpg, aiohttp (admin page, no JavaScript), Alembic, pytest (mock-pool unit tests, opt-in real-SQL tests in `tests/pg`).

**Spec:** `docs/superpowers/specs/2026-11-23-event-announcements-design.md` (read it first; it holds every decision and the placeholder table).

## Global Constraints

- All answers in code, commits and docs in English; no em dash (U+2014) anywhere (`tests/test_no_em_dash.py`); ruff line length 100.
- Commits authored as `M4XXX` / `44500247+M4XXX@users.noreply.github.com`, no AI attribution. Check staged files for secrets before committing.
- Both types default off. Without a channel nothing posts. No rules means no general posts.
- Default rule when the general type is switched on and no rule exists: 3 days before, 18:00 (bot time zone).
- Messages are limited to 2000 characters after filling. Only role mentions may ping: send with `allowed_mentions=disnake.AllowedMentions(roles=True, users=False, everyone=False)`.
- Every migration has a working `downgrade` (the old-backup tests in `tests/pg/test_old_backups_and_migrations.py` walk all revisions).
- Admin page: no JavaScript and no change to `SECURITY_HEADERS`. Commands and routes are admin only; `tests/test_security_guard.py` must pass unchanged.
- Posting and editing never raise into other flows: log, and one admin log entry per cause.
- Tests: `uv run pytest -q` and `uv run ruff check .` green after every task; real-SQL tests with `RAVEBOT_TEST_POSTGRES_URL` (see `tests/pg/__init__.py`).

## Review Focus

1. Rule times across a daylight saving change: "18:00" is wall clock in the bot zone, also when the day before has a different UTC offset.
2. Restart or two overlapping ticks: exactly one post per (event, type, rule); a post deleted by hand is never reposted.
3. A filled template over 2000 characters because of a long lineup or notes: nothing is sent, one admin log entry, retried next tick.
4. Text from DJ names, notes, genre, location containing `@everyone`, `<@&id>` or markdown: shown literally, never pings.
5. An event canceled, rescheduled or started between ticks: canceled posts get the fixed text once, a moved event keeps its post and shows the new time, no edits after the start for general posts.

---

## File structure

- Create `migrations/versions/0023_announcements.py`, `bot/db/announcements.py`: tables and repositories.
- Create `bot/services/announcement_template.py`: placeholders, rendering, checking, default templates (pure).
- Create `bot/services/announcement_settings.py`: config keys, `AnnouncementSettings`, enable/disable, default rule, due-time logic (pure helpers plus config access).
- Create `bot/services/announcements.py`: `AnnouncementService` (tick, post, edit, end, cancel).
- Create `bot/cogs/announcements_cog.py`: commands and the minute loop.
- Modify `bot/context.py` (`announcement_service` field), `bot/main.py` (build service, load cog), `bot/ui/setup_wizard.py` (channel step, summary, write), `bot/cogs/admin.py` (`/config show` line), `bot/web/service.py`, `bot/web/pages.py`, `bot/web/app.py` (page and routes), `bot/ui/texts.py`, `tests/pg/conftest.py`, docs.

---

### Task 1: Tables and repositories

**Files:** Create `migrations/versions/0023_announcements.py`, `bot/db/announcements.py`; Test `tests/test_migration_0023.py` (copy the shape of `tests/test_migration_0022.py`), `tests/db/test_announcements.py` (mock pool, shape of `tests/db/test_dm_menus.py`); Modify `tests/test_migration_chain.py` (head `0023`), `tests/pg/conftest.py` (add both tables to `TABLES`).

**Interfaces:**
- Produces, in `bot/db/announcements.py`:
  - `@dataclass(frozen=True) AnnouncementRule(id: int, days_before: int, post_time: datetime.time, created_at: datetime.datetime)`
  - `@dataclass(frozen=True) AnnouncementPost(id: int, event_id: str, kind: str, rule_id: int | None, channel_id: int, message_id: int | None, state: str, digest: str)`; `kind` is `"general"` or `"start"`; `state` is one of `claimed`, `posted`, `gone`, `ended`, `canceled`.
  - `async list_rules(db, guild_id) -> list[AnnouncementRule]` (ordered by days_before desc, post_time); `add_rule(db, guild_id, days_before: int, post_time: datetime.time) -> AnnouncementRule`; `update_rule(db, guild_id, rule_id, days_before, post_time) -> AnnouncementRule | None`; `delete_rule(db, guild_id, rule_id) -> bool`.
  - `async claim_post(db, event_id, kind, rule_id: int | None, channel_id) -> AnnouncementPost | None` (insert; `None` when that (event, kind, rule) already has a row); `get_post(db, event_id, kind, rule_id) -> AnnouncementPost | None`; `list_posts(db, event_id: str | None = None) -> list[AnnouncementPost]`; `mark_posted(db, post_id, message_id, digest)`; `set_digest(db, post_id, digest)`; `set_state(db, post_id, state)`; `release_claim(db, post_id)` (delete a row still in `claimed`).

- [ ] **Step 1: Tests first.** Migration test asserts `revision == "0023"`, `down_revision == "0022"`, the SQL creates `announcement_rules` (`id`, `guild_id BIGINT`, `days_before INTEGER`, `post_time TIME`, `created_at`) with a CHECK `days_before BETWEEN 0 AND 365`, and `announcement_posts` with a unique index over `(event_id, kind, COALESCE(rule_id, 0))`, no foreign key from posts to rules (deleting a rule keeps its posts); downgrade drops both. Repository tests assert the SQL and args of each function with a mock pool (`claim_post` uses `ON CONFLICT DO NOTHING ... RETURNING`; returns `None` when `fetchrow` returns `None`).
- [ ] **Step 2: Run them, expect failures** (`uv run pytest tests/test_migration_0023.py tests/db/test_announcements.py -q`).
- [ ] **Step 3: Implement** the migration (chain `0022` -> `0023`) and the repository module as specified.
- [ ] **Step 4: Run the new tests, `tests/test_migration_chain.py`, then the opt-in run** `uv run pytest -q tests/pg/test_postgres.py::test_migrations_go_down_and_up_again` against a test database; all pass.
- [ ] **Step 5: Commit** `feat: announcement rules and posts tables`.

### Task 2: Template engine

**Files:** Create `bot/services/announcement_template.py`; Test `tests/services/test_announcement_template.py`.

**Interfaces:**
- Consumes: `bot.notion.models.EventInfo` (`event_id`, `date`, `start`, `slot_minutes`, `slot_count`, `genre`, `location`, `notes`, `resolved_slots()`, `slot_start(index)`), `bot.ui.texts.escape_text`.
- Produces: `GENERAL = "general"`, `START = "start"`, `MAX_LENGTH = 2000`; `PLACEHOLDERS: dict[str, tuple[str, ...]]` (names without braces, per kind, exactly the spec table; the start kind additionally has `now_playing`, `next_up`, `status`, and the general kind accepts them too); `DEFAULT_TEMPLATES: dict[str, str]`; `@dataclass(frozen=True) TemplateCheck(ok: bool, problems: list[str], warnings: list[str], length: int, preview: str)`; `render(template: str, event: EventInfo, kind: str, now: datetime.datetime) -> str`; `check_template(template: str, kind: str, event: EventInfo | None) -> TemplateCheck`; `sample_event() -> EventInfo`; `event_end(event) -> datetime.datetime`; `current_slot_index(event, now) -> int | None`.

- [ ] **Step 1: Tests first** (names and assertions):
  - `test_render_fills_every_placeholder`: `{title}` is `event.event_id`; `{date}` is `<t:UNIX:F>` and `{countdown}` `<t:UNIX:R>` of the event start; `{description}` and `{notes}` both equal the notes; `{slots}` and `{booked}` are counts; `{lineup}` has one line per slot as `<t:UNIX:t> DJ name` or `<t:UNIX:t> 🟢 *open slot*`.
  - `test_empty_values_leave_no_blank_label_line`: a template line `Genre: {genre}` with no genre is dropped entirely (a line whose only content besides label text is empty placeholders is removed); a line with other filled placeholders stays.
  - `test_values_are_escaped`: a DJ name `@everyone <@&5> **x**` renders literally (through `escape_text`); a template's own `<@&123>` is kept as typed.
  - `test_doubled_braces_are_literal`: `{{` and `}}` render as `{` and `}` and are not placeholders.
  - `test_now_playing_and_next_up`: at a time inside slot 2 of 3, `{now_playing}` is that DJ, `{next_up}` the third; after the last slot `{next_up}` is empty and `{status}` says the event has ended; before the start `{now_playing}` is empty.
  - `test_check_flags_unknown_placeholder_and_unbalanced_braces` (problems name the placeholder), `test_check_rejects_empty_after_fallback` is not needed (empty means default, see settings), `test_check_over_limit_reports_length` (a template whose filled text is 2001 characters gives `ok False` with the length in `problems`), `test_check_warns_for_everyone_and_here`, `test_check_flags_malformed_role_mention` (`<@&abc>`), `test_check_uses_sample_event_without_a_real_one`.
  - `test_event_end_is_last_slot_start_plus_slot_length`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the module. Placeholders use `{name}`; scan with one regex; unknown names stay as typed in `render`. The default templates are short, sensible announcements using the placeholders (general: title, `{date}` and `{countdown}`, `{description}`, location, genre, `{lineup}`; start: "{title} is live", `{now_playing}`, `{next_up}`, `{lineup}`).
- [ ] **Step 4: Run tests green; ruff clean.**
- [ ] **Step 5: Commit** `feat: announcement template engine`.

### Task 3: Settings, due logic

**Files:** Create `bot/services/announcement_settings.py`; Test `tests/services/test_announcement_settings.py`.

**Interfaces:**
- Consumes: Task 1 repository; Task 2 `check_template`, `DEFAULT_TEMPLATES`; `bot.db.config` (`get_value`, `set_value`, `delete_value`); `bot.timezone.get_timezone`.
- Produces: config key constants `ANNOUNCE_CHANNEL_KEY = "announcement_channel"`, `ANNOUNCE_GENERAL_KEY = "announcement_general_enabled"`, `ANNOUNCE_START_KEY = "announcement_start_enabled"`, `ANNOUNCE_GENERAL_TEMPLATE_KEY = "announcement_general_template"`, `ANNOUNCE_START_TEMPLATE_KEY = "announcement_start_template"`, `ANNOUNCE_GENERAL_SINCE_KEY = "announcement_general_since"`; `DEFAULT_RULE_DAYS = 3`, `DEFAULT_RULE_TIME = datetime.time(18, 0)`;
  - `@dataclass(frozen=True) AnnouncementSettings(channel_id: int | None, general_enabled: bool, start_enabled: bool, general_template: str, start_template: str, general_since: datetime.datetime | None)`; `async load_settings(db, guild_id) -> AnnouncementSettings` (an empty or missing template is replaced by the default);
  - `async set_channel(db, guild_id, channel_id: int | None) -> None`; `async set_enabled(db, guild_id, kind: str, on: bool, now: datetime.datetime | None = None) -> None` (switching general on stores `general_since = now` and adds the default rule when there is none; switching off keeps rules); `async save_template(db, guild_id, kind: str, text: str) -> TemplateCheck` (stores only when the check passes, empty text deletes the key so the default applies);
  - `rule_post_time(event: EventInfo, rule: AnnouncementRule) -> datetime.datetime` (aware, wall clock in the bot zone, `event.date - days_before` at `rule.post_time`); `is_rule_due(event, rule, settings, now) -> bool` (post time `<= now`, `now` before the event start, and post time after `max(rule.created_at, settings.general_since)`).

- [ ] **Step 1: Tests first:** `test_enable_general_creates_default_rule_once` (3 days, 18:00; a second enable adds none; existing rules are untouched); `test_enable_stores_since_and_disable_keeps_rules`; `test_empty_template_loads_default`; `test_save_template_refuses_an_invalid_template_and_keeps_the_old_one`; `test_rule_post_time_is_wall_clock_across_dst` (event on 2027-03-29 21:00 Europe/Berlin, rule 2 days before at 18:00 gives 2027-03-27 18:00 local, offset +01:00, event day offset +02:00); `test_rule_due_window` (before post time false, in window true, at or after the event start false); `test_rule_created_after_its_post_time_is_not_due` (the watermark); `test_switch_on_after_post_time_is_not_due`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the module (config values as text, booleans as `"true"`/`"false"` like the other settings).
- [ ] **Step 4: Run tests green.**
- [ ] **Step 5: Commit** `feat: announcement settings and due logic`.

### Task 4: AnnouncementService, general announcements

**Files:** Create `bot/services/announcements.py`; Test `tests/services/test_announcements.py`.

**Interfaces:**
- Consumes: Tasks 1 to 3; `bot.db.events.list_events(db)`; `bot.services.fully_booked.is_fully_booked`; `bot.notion.models.STATUS_CANCELED`/`FINISHED_STATUSES`; `bot.services.activation.is_active`; the channel resolving and one-report-per-cause pattern of `bot/services/management_lineup.py` (`_resolve_channel`, `_report`).
- Produces: `class AnnouncementService` with `__init__(self, bot, db, log_service)` and `async tick(self, now: datetime.datetime | None = None) -> None` (never raises; serialised by an `asyncio.Lock`).

Behaviour of one tick for the general type (start type is Task 5): bot active, settings loaded, type enabled and a channel set, otherwise return. For every upcoming event that is not finished: for each rule that `is_rule_due`: if `not is_fully_booked(event)`, claim the post and set its state to `gone` so the rule never posts later for that event (skipped means skipped); otherwise `claim_post` (None means done already), render, post through one send helper `_send(channel, text, file: disnake.File | None = None)` (the `file` parameter stays unused for now: it is where the later poster attachment goes) with the allowed mentions, `mark_posted(message_id, digest)`; on a failed send `release_claim`. For existing `posted` general posts of events that have not started: re-render, edit when the digest changed. Canceled event: edit existing posts once to the fixed text `texts.ANNOUNCE_CANCELED` and set state `canceled`. A post whose message was deleted (`disnake.NotFound` on edit): `set_state(gone)`. A rendering that exceeds `MAX_LENGTH` or an invalid template: nothing sent, `release_claim`, one admin log entry per (channel/template version, reason) per process.

- [ ] **Step 1: Tests first** (fake bot, channel and in-memory repository; the style of `tests/services/test_management_lineup.py`): `test_posts_once_per_event_and_rule`; `test_two_concurrent_ticks_post_once`; `test_incomplete_lineup_is_skipped_and_not_retried_later`; `test_rule_not_due_before_its_time_and_not_after_the_start`; `test_no_rules_means_no_post`; `test_disabled_or_no_channel_posts_nothing`; `test_edit_when_lineup_changes_until_the_start_only`; `test_canceled_event_gets_the_fixed_text_once`; `test_deleted_message_is_not_reposted`; `test_over_long_fill_sends_nothing_and_logs_once`; `test_failed_send_leaves_no_claim`; `test_roles_only_allowed_mentions`; `test_send_helper_accepts_an_optional_file`; `test_channel_change_keeps_editing_old_posts_in_their_channel`; `test_idle_bot_posts_nothing`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `AnnouncementService` (general part). Texts in `bot/ui/texts.py`: `ANNOUNCE_CANCELED = "This event was canceled."` and the admin log messages for channel missing, forbidden, over long, invalid template.
- [ ] **Step 4: Run tests green; ruff clean.**
- [ ] **Step 5: Commit** `feat: general event announcements`.

### Task 5: Event start announcement

**Files:** Modify `bot/services/announcements.py`; Test `tests/services/test_announcements_start.py`.

**Interfaces:**
- Consumes: Task 2 `event_end`, `current_slot_index`, `render(..., START, now)`; Task 4 service internals.
- Produces: the start part of `tick`.

Behaviour: when the start type is enabled and a channel is set, for every event that is not canceled and for which `event.slot_start(0) <= now < event_end(event)`: skip (store `gone`) when the lineup is incomplete; otherwise `claim_post(event_id, "start", None, channel_id)` and post; thereafter on every tick edit the post when the digest changed (slot change); at or after `event_end` edit once with `{status}` showing the ended text and set state `ended`; a canceled event gets `ANNOUNCE_CANCELED`. A bot offline at the start posts as soon as it runs again, until the event has ended; after the end nothing is posted.

- [ ] **Step 1: Tests first:** `test_posted_at_the_start_not_before`; `test_edited_at_each_slot_change_with_now_playing_and_next_up`; `test_last_slot_end_edits_once_to_ended_and_stops`; `test_restart_does_not_post_twice`; `test_late_start_after_downtime_posts_until_the_end`; `test_nothing_posted_after_the_event_ended`; `test_incomplete_lineup_at_start_means_no_start_post`; `test_start_time_moved_before_the_post_uses_the_new_time`; `test_start_and_general_posts_are_independent`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the start flow reusing the posting, editing, reporting and `gone` handling of Task 4 (one code path for both types: a small `_Plan(kind, rule_id, template)` list per event).
- [ ] **Step 4: Run tests green.**
- [ ] **Step 5: Commit** `feat: event start announcement`.

### Task 6: Cog, commands, wiring

**Files:** Create `bot/cogs/announcements_cog.py`; Modify `bot/context.py`, `bot/main.py`; Test `tests/cogs/test_announcements_cog.py`.

**Interfaces:**
- Consumes: Task 3 settings functions, Task 1 rule repository, Task 4 service, `AdminCog.cog_slash_command_check` and `cog_slash_command_error` (assign them like `ScheduleCog` does), `bot.cogs.loop_support` (`run_cycle`, `prepare_loop`, `guard_db`).
- Produces: `class AnnouncementsCog(commands.Cog)` with `__init__(self, ctx: BotContext)`; a `tasks.loop(minutes=1)` that calls `ctx.announcement_service.tick()` through `run_cycle`; slash command group `/announcements` (`contexts=SERVER_ONLY`) with sub commands `show`, `channel` (option `channel: disnake.TextChannel | None`, empty clears), `general` and `start` (option `state: str` choices `on`/`off`), and group `rule` with `add` (`days: int` 0 to 365, `time: str` HH:MM), `edit` (`id: int`, `days`, `time`), `remove` (`id: int`). All answers ephemeral. `BotContext.announcement_service: Any | None = None`.

- [ ] **Step 1: Tests first:** non-admin refused for every sub command (the cog check); `test_show_lists_channel_switches_rules_and_templates_state`; `test_general_on_creates_the_default_rule_and_says_so`; `test_start_on_without_channel_warns_that_nothing_posts_yet`; `test_rule_add_edit_remove_validate_time_and_days`; `test_commands_are_ephemeral`; `test_loop_calls_tick`. Then run `uv run pytest tests/test_security_guard.py -q`: it must pass (the new cog has its own check and no user or message commands).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the cog; in `bot/main.py` build `AnnouncementService(bot, db, ctx.log_service)`, set `ctx.announcement_service`, add the cog next to the others.
- [ ] **Step 4: Run the full suite green.**
- [ ] **Step 5: Commit** `feat: announcement commands and loop`.

### Task 7: Setup wizard step and `/config show`

**Files:** Modify `bot/ui/setup_wizard.py`, `bot/cogs/admin.py`, `bot/ui/texts.py`; Test `tests/ui/test_setup_wizard.py`, `tests/cogs/test_admin.py` (follow the file that already tests `/config show`).

**Interfaces:** Consumes `ANNOUNCE_CHANNEL_KEY` and `load_settings`. Produces wizard step key `"announcement_channel"` placed after `"management_channel"` in `STEPS`, controls `ID_ANN_CHANNEL` and `ID_ANN_CLEAR`, draft data key `announcement_channel`.

- [ ] **Step 1: Tests first:** the step exists and is optional (Next enabled with no channel); choosing and clearing a channel updates the draft; a channel the bot cannot post in shows the same check text as the other channel steps; `_write_all` stores the channel or deletes the key when cleared; the draft loads the stored channel; the summary has `Announcement channel: <#id>` or `not set` plus `General announcements: on|off` and `Event start announcements: on|off`; `/config show` lists the same three lines.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** by copying everywhere the `management_channel` step appears (`grep -n "MGMT_KEY\|ID_MGMT_" bot/ui/setup_wizard.py`): `STEPS`, renderer branch, control map, select handler, draft load, `_write_all`, summary lines. The switches are not wizard controls; the summary shows them read only.
- [ ] **Step 4: Run the wizard tests and the full suite green.**
- [ ] **Step 5: Commit** `feat: announcement channel in the setup wizard`.

### Task 8: Admin page "Announcements"

**Files:** Modify `bot/web/service.py`, `bot/web/pages.py`, `bot/web/app.py`; Test `tests/web/test_announcements_routes.py` (shape of `tests/web/test_booking_rules_routes.py`).

**Interfaces:**
- Consumes: Tasks 1 to 3; `check_template`, `sample_event`.
- Produces on `AdminPageService`: `async announcements() -> AnnouncementPageData` (settings, rules, upcoming event for the preview or `None`); `async save_announcement_settings(raw: dict[str, str]) -> ActionResult` (channel id text or empty, two switch fields); `async add_rule(raw)`, `edit_rule(rule_id, raw)`, `remove_rule(rule_id)` returning `ActionResult`; `async check_template(kind: str, text: str) -> TemplateCheck`; `async save_template(kind: str, text: str) -> ActionResult` (always runs the check itself and refuses an invalid template). Pages in `pages.py`: `render_announcements(...)`, `render_edit_announcement_rule(...)`, `render_confirm_remove_announcement_rule(...)`. Routes: `GET/POST /announcements` (settings form posts to `/announcements/settings`), `POST /announcements/rules/add`, `GET/POST /announcements/rules/{id}/edit`, `GET/POST /announcements/rules/{id}/remove`, `POST /announcements/templates/{kind}/check`, `POST /announcements/templates/{kind}/save` with `kind` limited to `general|start`. A "Announcements" button on the dashboard next to "Booking rules".

- [ ] **Step 1: Tests first:** unauthenticated and CSRF behaviour (also covered by the guard test, which must still pass); the page shows channel, both switches, rules, both template editors with the placeholder list; saving settings validates the channel id; rules add, edit, delete, invalid input shows the message and keeps the form values; `test_check_and_preview_shows_ok_length_and_the_filled_preview` (preview HTML escaped); `test_check_shows_unknown_placeholder_problem`; `test_save_runs_the_check_and_refuses_an_invalid_template_even_without_a_prior_check` (the stored template is unchanged); `test_save_valid_template_stores_it_and_empty_resets_to_default`; `test_page_contains_no_script_tag`; the response still carries the unchanged `SECURITY_HEADERS`; the maintenance refusal like the other pages.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** following the booking rules and schedule rules pages (forms with CSRF field, redirects with `?done=`).
- [ ] **Step 4: Run web tests, `tests/test_security_guard.py` and the full suite green.**
- [ ] **Step 5: Commit** `feat: announcements page on the admin page`.

### Task 9: Docs, real-SQL check, final verification

**Files:** Modify `CHANGELOG.md`, `docs/user-guide.md`, `docs/developer-guide.md`, `docs/security-audit-2026-11-23.md` (add the new surfaces to the table); Test `tests/pg/test_announcements_repo.py`.

- [ ] **Step 1: Write `tests/pg/test_announcements_repo.py`** (real SQL): `claim_post` twice returns the post then `None`; the start kind (NULL rule) is unique per event; deleting a rule keeps its posts; rule CHECK refuses `days_before` 366.
- [ ] **Step 2: Docs:** changelog entry, user guide section (setup step, switches, rules, template placeholders table, the check and preview button, skip rule, default 3 days at 18:00), developer guide section (tables, services, tick order, keys).
- [ ] **Step 3: Run** `uv run ruff check .`, `uv run pytest -q`, then `uv run pytest -q tests/pg` against a throwaway database (the old-backup test picks migration 0023 up by itself); all pass.
- [ ] **Step 4: Commit** `docs: event announcements`.
