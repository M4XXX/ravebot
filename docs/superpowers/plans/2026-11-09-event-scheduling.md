# Event scheduling in /setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the organizer pick the first scheduled event date, split the Events popup into one wizard step per setting, turn the schedule off, and create manual events with their own shape that never affect the schedule.

**Architecture:** Pure option lists and parsers go into a new `bot/ui/setup_options.py`. The wizard gets one generic "value step" (a `StringSelect` plus an "Other" button that opens a one-field popup) driven by the step key. `replenish_events` learns the schedule switch, the first date and the manual-event filter, backed by a new `manual_events` table.

**Tech Stack:** Python 3.13, disnake 2.12.0, asyncpg, alembic, pytest, ruff (line length 100), pyright.

**Spec:** `docs/superpowers/specs/2026-11-09-event-scheduling-design.md`

All commands run in `/home/godfather/SoftwareProjects/DiscordRaveBot/output/ravebot`. Work on branch `feat/event-scheduling` (create it from `main` before Task 1). Test command: `uv run pytest -q`. Lint: `uv run ruff check . && uv run pyright`.

## Global Constraints

- English only in code, texts and docs; no em dashes in texts (`tests/test_no_em_dash.py`).
- `STEPS` becomes exactly: `welcome, dj_channel, log_channel, roles, slots, start_time, slot_length, schedule, interval, first_date, lookahead, timezone, invites, summary`.
- Config keys: `event_schedule_enabled` (`"true"`/`"false"`, missing means `"true"`), `first_event_date` (`YYYY-MM-DD`, empty string or missing means none).
- Limits: slots 1 to 100 (`MAX_SLOTS`), slot length 1 to 1440, interval 1 to 365, kept ready 1 to 52, start time `HH:MM` 00:00 to 23:59, first date today or later in the draft's zone.
- Discord limits: select menu 25 options, popup 5 fields (each popup here has one).
- Migration `0015` has `down_revision = "0014"` and creates `manual_events(page_id text primary key, created_at timestamptz default now())`.
- Event ID stays `YYYY-MM-DD Rave Night`; never two events with the same ID.
- Existing stored settings and events keep working unchanged.
- Commits: author and committer `M4XXX <44500247+M4XXX@users.noreply.github.com>`, no AI attribution lines, check the staged diff for secrets first.

## Review Focus

- Leaving "first event date" empty or choosing "As soon as the interval allows": behavior is exactly today's (`max(latest scheduled, today) + interval * i`). Pinned in Task 3.
- A first date that is already past when the poll runs (draft saved days ago): ignored, cleared, today's rule used. Task 3.
- Schedule turned off on a re-run while future scheduled events exist: nothing is deleted, nothing new is created. Task 3.
- A manual event on a scheduled date: that date is skipped, no duplicate ID, `missing` events are still created. Task 3.
- Back and Next with the schedule off never land on `interval`, `first_date` or `lookahead`, and step numbers stay consecutive. Task 6.
- Stored value not in the dropdown (for example interval 10 or slots 30): shown as selected and not lost on Next. Task 5.
- Popup input with surrounding spaces, `9:30`, or a date typed as `14.11.2026`: trimmed, normalized, or rejected with a message naming the field. Task 5.
- Old wizard messages (custom ids `setup:events_change`, `setup:zone`) pressed after the update: routed to nothing harmful (Discord shows "interaction failed"; the old draft step index may point to a different step, so drafts with `step` beyond the new range are clamped by `step_key`). Task 5.

---

### Task 1: manual_events table and repository

**Files:**
- Create: `migrations/versions/0015_manual_events.py`, `bot/db/manual_events.py`
- Test: `tests/test_migration_0015.py`, `tests/db/test_manual_events.py`

**Interfaces:**
- Produces: `async def mark_manual(pool: asyncpg.Pool, page_id: str) -> None` (idempotent insert), `async def get_manual_page_ids(pool: asyncpg.Pool, page_ids: list[str]) -> set[str]` (one query; empty input returns `set()` without a query).

- [ ] **Step 1:** Write `tests/test_migration_0015.py` in the style of `tests/test_migration_0014.py`: `test_chain` (revision `"0015"`, down `"0014"`), `test_upgrade_creates_table` (table name `manual_events`, columns `["page_id", "created_at"]`), `test_downgrade_drops_table`.
- [ ] **Step 2:** Write `tests/db/test_manual_events.py` in the style of `tests/db/test_web_admins.py`: `test_mark_manual_is_idempotent`, `test_get_manual_page_ids_returns_only_marked`, `test_get_manual_page_ids_empty_input_does_no_query`.
- [ ] **Step 3:** Run `uv run pytest tests/test_migration_0015.py tests/db/test_manual_events.py -q`. Expected: FAIL (modules missing).
- [ ] **Step 4:** Implement the migration (copy the structure of `0014_web_admins.py`) and the two repository functions (`INSERT ... ON CONFLICT DO NOTHING`; `SELECT page_id FROM manual_events WHERE page_id = ANY($1::text[])`).
- [ ] **Step 5:** Run the same tests. Expected: PASS.
- [ ] **Step 6:** Commit `feat: manual_events table and repository`.

### Task 2: setup_options (pure helpers)

**Files:**
- Create: `bot/ui/setup_options.py`
- Modify: `bot/ui/setup_wizard.py` (remove `parse_hhmm` and `_HHMM_RE`; add `from bot.ui.setup_options import parse_hhmm  # noqa: F401` re-export so `sw.parse_hhmm` keeps working)
- Test: `tests/ui/test_setup_options.py`

**Interfaces:**
- Produces:
  - `parse_hhmm(text: str) -> str | None` (moved unchanged).
  - `LIMITS: dict[str, int]` with keys `slots_per_event=100`, `slot_duration_minutes=1440`, `event_interval_days=365`, `event_lookahead_count=52`.
  - `dropdown_options(key: str, current: Any, today: datetime.date | None = None) -> list[tuple[str, str]]` returning `(label, value)` pairs, at most 25, for keys `slots_per_event`, `default_start_time`, `slot_duration_minutes`, `event_interval_days`, `first_event_date`, `event_lookahead_count`, `timezone`. Option sets are the spec's table; the zone list is Europe/Berlin, Europe/London, Europe/Paris, Europe/Vienna, Europe/Zurich, Europe/Amsterdam, Europe/Madrid, Europe/Rome, Europe/Warsaw, Europe/Helsinki, Europe/Athens, Europe/Istanbul, UTC, America/New_York, America/Chicago, America/Denver, America/Los_Angeles, America/Sao_Paulo, Asia/Tokyo, Asia/Dubai, Australia/Sydney. The stored `current` value is added as an option if missing (dropping the last option to stay at 25). `first_event_date` options: first `("As soon as the interval allows", "")`, then 24 days from `today` labelled like `Sat 14 Nov` with ISO value.
  - `parse_choice(key: str, raw: str, today: datetime.date | None = None) -> tuple[dict[str, Any], list[str]]`: validates a dropdown value or popup text for one key; returns the draft update and error texts (via `texts.setup_invalid(label, rule)`). Numbers: trimmed int in range. `default_start_time`: `parse_hhmm`. `timezone`: `is_valid_timezone`. `first_event_date`: empty string means "none" and returns `{"first_event_date": ""}`; otherwise `YYYY-MM-DD` that is not before `today` (default `local_today()`).
  - `FIELD_LABELS: dict[str, str]` popup field label per key (for example `"Slots per event"`, `"Start time (HH:MM)"`, `"Slot length (minutes)"`, `"One event every (days)"`, `"First event date (YYYY-MM-DD)"`, `"Future events kept ready"`, `"Time zone (e.g. Europe/Berlin)"`).

- [ ] **Step 1:** Write failing tests: `test_dropdown_sizes_never_exceed_25` (every key, including a stored out-of-range current), `test_stored_value_stays_selectable` (interval 10 appears), `test_first_date_options` (first option value `""`, next ones start at `today`, label `Sat 14 Nov` for `datetime.date(2026, 11, 14)`), `test_parse_choice_numbers` (parametrized: `"14"` ok, `" 14 "` ok, `"0"`, `"366"` for interval, `"x"` rejected with the label in the message), `test_parse_choice_start_time` (`"9:30"` becomes `"09:30"`, `"24:00"` rejected), `test_parse_choice_first_date` (`""` ok, today ok, yesterday rejected, `"14.11.2026"` rejected, valid future ok), `test_parse_choice_timezone` (`"Europe/Berlin"` ok, `"Mars/Base"` rejected).
- [ ] **Step 2:** Run `uv run pytest tests/ui/test_setup_options.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the module. Use `datetime.date.strftime("%a %d %b")` with locale-independent English names (build the weekday and month names from fixed tuples).
- [ ] **Step 4:** Run the new tests plus `uv run pytest tests/ui/test_setup_wizard.py -q -k parse_hhmm`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: setup option lists and value parsers`.

### Task 3: replenish rules

**Files:**
- Modify: `bot/services/event_replenish.py`
- Test: `tests/services/test_event_replenish.py`

**Interfaces:**
- Consumes: `get_manual_page_ids` (Task 1), `parse_outreach_enabled`-style parsing (write a small local `_schedule_enabled(raw: str | None) -> bool`, missing or anything but `"false"` is on).
- Produces: `replenish_events(db, notion_client, guild_id, events=None) -> int` unchanged signature.

- [ ] **Step 1:** Update the test helper `_cfg` in `test_event_replenish.py` to also supply `event_schedule_enabled` and `first_event_date` (default `None`) and add an autouse fixture patching `bot.services.event_replenish.get_manual_page_ids` (default returns `set()`, tests override). Existing tests must still pass.
- [ ] **Step 2:** Add failing tests (dates relative to `TODAY = 2026-06-01`, interval 14, lookahead 3 unless noted): `test_schedule_off_creates_nothing` (returns 0, `create_event` not awaited), `test_first_date_used_for_first_event_then_interval` (first date 2026-06-06 gives 06-06, 06-20, 07-04), `test_first_date_cleared_after_creation` (`set_value` called with `("first_event_date", "")`), `test_first_date_ignored_when_scheduled_events_ahead` (existing event on 06-10, first date 06-06: new dates follow 06-10, value cleared), `test_past_first_date_ignored_and_cleared`, `test_empty_first_date_keeps_todays_rule`, `test_manual_events_not_counted_and_do_not_shift_base` (manual events on 06-03 and 06-30, none scheduled: dates are `TODAY + 14 * i`, three created), `test_duplicate_date_is_skipped_and_next_date_used` (`event_id_exists` true for the first candidate: still creates 3 events, the skipped date is not created), `test_write_failures_stop_after_missing_attempts` (every create raises `NotionWriteError`: `create_event` awaited exactly `missing` times).
- [ ] **Step 3:** Run `uv run pytest tests/services/test_event_replenish.py -q`. Expected: new tests FAIL.
- [ ] **Step 4:** Rewrite `_replenish` per spec section "Scheduling logic". Candidate dates come from a generator; loop until `attempts == missing` (attempts count creations and write failures, not duplicate skips) or `missing + 52` candidates were examined. Clear `first_event_date` (set to `""`) whenever it was set and either applied-with-a-creation or ignored. Manual filter: `manual = await get_manual_page_ids(db, [e.page_id for e in events])`, `scheduled = [e for e in events if e.page_id not in manual]`.
- [ ] **Step 5:** Run the file again, then `uv run pytest tests/cogs/test_events.py -q`. Expected: PASS.
- [ ] **Step 6:** Commit `feat: first event date, schedule switch and manual-event filter in replenish`.

### Task 4: /create event options and /config show

**Files:**
- Modify: `bot/cogs/admin.py` (`create_event`, `config_show`)
- Test: `tests/cogs/test_admin.py`

**Interfaces:**
- Consumes: `mark_manual` (Task 1), `parse_hhmm`, `LIMITS` (Task 2), `default_event_shape`.
- Produces: `create_event(self, inter, date_str, slots: int | None, start: str | None, slot_minutes: int | None)`; parameters declared with `commands.Param(None, name=..., description=..., ge=1, le=...)`, `start` as `commands.Param(None, name="start", description="Start time HH:MM (default from setup)")`.

- [ ] **Step 1:** Update existing `test_add_event_*` calls to pass the new keyword arguments as `None`. Add failing tests: `test_create_event_uses_given_shape` (slots 4, start `"22:00"`, slot_minutes 45: `create_event` awaited with `time(22, 0)`, 4, 45; `init_event_slots` with 4), `test_create_event_falls_back_to_defaults_per_field` (only slots given), `test_create_event_rejects_bad_start` (reply names the start field, `create_event` not called), `test_create_event_marks_manual` (`mark_manual` awaited with the created `page_id`), `test_create_event_still_rejects_duplicate_date`, `test_config_show_lists_schedule_keys` (output contains `Scheduled events: on` and `First event date: not set` when nothing is stored).
- [ ] **Step 2:** Run `uv run pytest tests/cogs/test_admin.py -q`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. Validate `start` before deferring; call `mark_manual` inside the existing try-block style used for `init_event_slots` (failure to mark is logged, not shown, and the event stays). `config_show` gets two lines after "One event every (days)": `Scheduled events: on|off` and `First event date: <value or not set>`.
- [ ] **Step 4:** Run the file again. Expected: PASS.
- [ ] **Step 5:** Commit `feat: per-event shape for /create event and new /config show lines`.

### Task 5: wizard value steps (schedule on)

**Files:**
- Modify: `bot/ui/setup_wizard.py`, `bot/ui/texts.py`
- Test: `tests/ui/test_setup_wizard.py`, `tests/ui/test_setup_options.py`

**Interfaces:**
- Consumes: `dropdown_options`, `parse_choice`, `FIELD_LABELS` (Task 2).
- Produces:
  - `STEPS` as in Global Constraints; `VALUE_STEPS: dict[str, str]` mapping step key to config key (`slots` to `slots_per_event`, `start_time` to `default_start_time`, `slot_length` to `slot_duration_minutes`, `interval` to `event_interval_days`, `first_date` to `first_event_date`, `lookahead` to `event_lookahead_count`, `timezone` to `timezone`).
  - Control ids `ID_VALUE = "setup:value"` (select) and `ID_OTHER = "setup:other"` (button "Other"). `StepView.options: list[tuple[str, str]]` and `StepView.selected: str | None` carry the dropdown for rendering.
  - `texts.setup_value_step(number: int, total: int, title: str, question: str, current: str, note: str | None = None) -> str`.
  - Handlers `SetupWizardView.on_value_select(inter)` (reads `inter.values[0]`, key from `step_key(draft.step)`, parses with `parse_choice`, stores in the draft under the lock, re-renders) and `on_other(inter)` (opens `ValuesModal` with one field, parser wraps `parse_choice`).
  - Removed: `ID_EVENTS_CHANGE`, `ID_ZONE`, `on_events_change`, `on_zone`, `parse_events_fields`, `parse_zone_field`, `texts.setup_events_step`, the `events` step, and their tests.
  - `DEFAULT_VALUES` gains `"event_schedule_enabled": True` and `"first_event_date": ""`; `values()` stays as is.

- [ ] **Step 1:** Update `tests/ui/test_setup_wizard.py`: delete the tests that exercise the removed popup and functions (`test_events_popup_*`, `test_zone_popup_valid_and_invalid`, the `parse_events_fields` tests, the `setup_events_step` use at line 761 and the ids in `test_every_control_refuses_non_admin` at line 186), replace with the new ids. Add failing tests: `test_total_steps_follows_step_list` still holds with 13 numbered steps, `test_slots_step_renders_dropdown_with_current_selected`, `test_value_select_stores_in_draft_only_and_rerenders` (parametrized over all seven steps), `test_stored_out_of_range_value_stays_selected_and_survives_next` (interval 10), `test_other_opens_one_field_popup_with_current_value`, `test_other_popup_rejects_invalid_naming_field` (parametrized: slots `"0"`, start `"24:00"`, slot length `"1441"`, interval `"366"`, first date `"2020-01-01"` and `"14.11.2026"`, lookahead `"53"`, zone `"Mars/Base"`), `test_other_popup_trims_and_normalizes` (`" 9:30 "` stored as `"09:30"`), `test_value_controls_refuse_non_admin`, `test_old_step_index_beyond_range_is_clamped` (draft with `step=99` renders the summary), and the existing `test_restart_rebuilds_view_from_stored_draft` extended to the new ids (persistence through `registered_views`).
- [ ] **Step 2:** Run `uv run pytest tests/ui -q`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement in `setup_wizard.py` and `texts.py`: new `STEPS`, `render_step` branch for `key in VALUE_STEPS` (title and question text per key, current value formatted with the zone abbreviation for the start step, a note on the zone step reusing the existing "Start times are local time" wording), `_make_item` entries for `ID_VALUE` (`disnake.ui.StringSelect`, `custom_id=ID_VALUE`, options from `step.options` with the selected one `default=True`) and `ID_OTHER`, `_layout` entries `[ID_VALUE, ID_OTHER, ID_BACK, ID_NEXT]` for every value step, `registered_views` unchanged in shape. The `ValuesModal` class stays and is reused with `fields=[("v", FIELD_LABELS[key], current_text)]`.
- [ ] **Step 4:** Run `uv run pytest tests/ui tests/test_no_em_dash.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: one wizard step per event setting`.

### Task 6: schedule toggle, skipping, summary, persistence

**Files:**
- Modify: `bot/ui/setup_wizard.py`, `bot/ui/texts.py`
- Test: `tests/ui/test_setup_wizard.py`

**Interfaces:**
- Consumes: Task 5 steps and ids.
- Produces:
  - `ID_SCHEDULE_TOGGLE = "setup:schedule_toggle"`; `SetupWizardView.on_schedule_toggle(inter)` flips `event_schedule_enabled` in the draft.
  - `def scheduled_step_keys() -> tuple[str, ...]` returning `("interval", "first_date", "lookahead")`; `def next_step(data: dict[str, Any], step: int, delta: int) -> int` returning the next step index in direction `delta` (+1 or -1), skipping scheduled steps while `event_schedule_enabled` is false, clamped to `0..len(STEPS)-1`. `_navigate_locked` uses it.
  - `step_number(key)` and `total_steps()` become `step_number(key, data)` and `total_steps(data)` and skip the hidden steps when scheduling is off (callers updated, `data` optional and defaulting to schedule on).
  - `texts.setup_schedule_step(number, total, enabled: bool) -> str` and the summary line replaced by `Events: N slots, HH:MM ZONE, M min` plus `Scheduled events: off` or `Scheduled events: every N days from DATE, K ahead` (the `from DATE` part only when `first_event_date` is set).
  - `prefilled_data` loads `event_schedule_enabled` (missing is on) and `first_event_date` (valid and today or later, else `""`); `_write_all` already writes every `DEFAULT_VALUES` key.

- [ ] **Step 1:** Add failing tests: `test_schedule_step_toggle_flips_draft_value`, `test_next_skips_scheduled_steps_when_off` and `test_back_skips_them_in_reverse` (walk the whole path from `schedule` to `timezone` and back), `test_step_numbers_are_consecutive_when_off` (labels "Step n of 10" style, no gaps), `test_summary_shows_scheduled_off`, `test_summary_shows_first_date_when_set`, `test_prefill_defaults_schedule_on_and_drops_past_first_date`, `test_activate_with_schedule_off_creates_no_events_and_posts_lineup` (stored `event_schedule_enabled == "false"`, `create_event` never awaited), `test_save_writes_new_keys`, `test_toggle_control_refuses_non_admin`.
- [ ] **Step 2:** Run `uv run pytest tests/ui/test_setup_wizard.py -q`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. `render_step` for `schedule` returns `[ID_SCHEDULE_TOGGLE, ID_BACK, ID_NEXT]` with `toggle_label` "Turn off" or "Turn on"; add the same list to `_layout`.
- [ ] **Step 4:** Run `uv run pytest -q`. Expected: all PASS.
- [ ] **Step 5:** Commit `feat: schedule switch and step skipping in the wizard`.

### Task 7: docs and final verification

**Files:**
- Modify: `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md`

- [ ] **Step 1:** In the user guide, describe the new setup steps, the first event date, turning the schedule off, and `/create event` with `slots`, `start`, `slot_minutes`. In the developer guide, document the `manual_events` table, the two config keys and the replenish rules. Add a CHANGELOG entry in the file's existing format.
- [ ] **Step 2:** Run `uv run ruff check . && uv run pyright && uv run pytest -q`. Expected: no lint errors, no pyright errors, all tests PASS.
- [ ] **Step 3:** Commit `docs: event scheduling and manual events`. Do not push or open a PR until asked.
