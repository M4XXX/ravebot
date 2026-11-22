# Booking Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring back the two booking policies (slot limit per DJ and event, cancellation cutoff) on `main`, each switchable and with a configurable number, set in the setup wizard and on a new "Booking rules" admin webpage.

**Architecture:** A small `bot/policy.py` holds the rules (`BookingRules`, defaults, load/save, pure checks, refusal texts). The slot limit is enforced inside the slot-claim transaction (`acquire_slot_lock(..., max_per_dj=...)`) and mirrored in the pickers so a DJ is never offered what would be refused. The cutoff is checked in the two DJ cancellation views. Admin bookings, Notion hand edits and admin cancellations never pass the limits. Four config-table keys store the rules; the wizard step and the web page both read and write them through `bot/policy.py`.

**Tech Stack:** Python, disnake, asyncpg, aiohttp (server-rendered admin page), pytest (asyncio, mocks, real-SQL tests in `tests/pg`), ruff.

**Spec:** `docs/superpowers/specs/2026-11-22-booking-rules-design.md`

## Global Constraints

- All text, code, comments and docs in English. No em dash (U+2014) in user-facing strings under `bot/` (`tests/test_no_em_dash.py`).
- Config keys (config table, per guild): `slot_limit_enabled` (`"true"`/`"false"`), `slot_limit_max`, `cancel_cutoff_enabled` (`"true"`/`"false"`), `cancel_cutoff_minutes`.
- Defaults when a key is missing or unreadable: slot limit on with 2, cutoff on with 60 minutes.
- Ranges: slot limit whole number from 1 to `MAX_SLOTS` (100, `bot/notion/models.py`); cutoff whole number from 0 to 10080 minutes (0 = DJs may cancel until the event starts).
- Only slots the DJ holds count toward the limit (the slot's holder in `slot_locks.discord_user_id`; in `EventInfo` the resolved slot's `discord_id`). Invites, extra DJs on a slot and other events do not count.
- Slot limit applies to DJ-facing booking only. It never applies to `/admin book` (`AdminBookingService.book_slot_override`), Notion hand edits (`bot/sync/intake.py`) or any other admin path. The cutoff applies to DJ-facing cancellation only (never to `cancel_slot_override`, event cancellation, DJ deletion or Notion edits).
- Cutoff reference: the event start (`event.slot_start(0)`), not the DJ's own slot.
- Refusal texts use the current numbers from the settings, never a hard-coded 2 or 60.
- Lowering the limit never removes bookings; a DJ above the limit may still cancel (subject to the cutoff).
- Rules are read at action time (no caching across requests), so a change applies immediately.
- Commits authored as M4XXX <44500247+M4XXX@users.noreply.github.com>, no AI attribution or co-author lines. Check staged files for secrets before each commit.
- Verification from `output/ravebot`: `uv run pytest -q`, `uv run ruff check .`.

## Review Focus

- Two parallel confirms by the same DJ for different slots with limit 1: exactly one wins (Task 2 real-SQL test).
- Limit disabled: nothing refuses and no extra query matters (Tasks 1, 2 tests).
- A DJ at the limit in every event with open slots: a clear message, not an empty picker (Task 3 test).
- Stored value that is not a number, or out of range (hand-edited config): falls back to the default, never crashes (Task 1 test).
- Cutoff boundary: exactly at `start - M` is closed; one second earlier is open; M = 0 closes at the start; event start unreadable stays cancellable (Task 4 test).
- Slow DJ opens the cancel confirm before the cutoff and confirms after it: refused on confirm (Task 4 test).
- Admin override: booking by `book_slot_override` and a Notion hand edit succeed at the limit; admin cancel succeeds inside the cutoff (Task 5 tests).
- Wizard re-run and in-flight drafts saved before this step existed keep working (Task 6 test).
- Web form with blank, negative, non-numeric or huge numbers keeps the typed values and shows an error, nothing saved (Task 7 test).

---

## File Structure

- Create `bot/policy.py`: rules, load/save, pure checks, refusal texts.
- Modify `bot/db/bookings.py`: `acquire_slot_lock(..., max_per_dj)` and `DjSlotLimitReached`.
- Modify `bot/ui/views.py`: confirm refusal, pickers, outreach accept, cancel views.
- Modify `bot/services/outreach.py`: skip DJs at the limit.
- Modify `bot/cogs/admin.py`: `/config show` lines.
- Modify `bot/ui/setup_wizard.py`, `bot/ui/texts.py`: the "Booking rules" step.
- Modify `bot/web/app.py`, `bot/web/pages.py`, `bot/web/service.py`: the "Booking rules" page.
- Modify docs: `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md`.
- Tests: `tests/test_booking_policy.py` (new), `tests/pg/test_postgres.py` or a new `tests/pg/test_slot_limit.py`, plus additions to existing view, wizard, web and cog tests.

---

### Task 1: Policy core (`bot/policy.py`)

**Files:**
- Create: `bot/policy.py`
- Test: `tests/test_booking_policy.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class BookingRules: slot_limit_enabled: bool = True; slot_limit_max: int = 2; cancel_cutoff_enabled: bool = True; cancel_cutoff_minutes: int = 60`
  - `DEFAULT_RULES: BookingRules`, `MAX_CUTOFF_MINUTES = 10080`, `RULE_KEYS: tuple[str, ...]` (the four config keys).
  - `async load_rules(db, guild_id: int | None) -> BookingRules` (missing, unreadable or out-of-range value falls back to that field's default; `db` or `guild_id` None returns `DEFAULT_RULES`; never raises).
  - `async current_rules(db, bot) -> BookingRules`: `load_rules` with the guild id of `bot.guilds[0]` (bot None or no guild: defaults).
  - `async save_rules(db, guild_id: int, rules: BookingRules) -> None` (writes the four keys with `set_value`).
  - `parse_rules_form(raw: dict[str, str]) -> tuple[BookingRules | None, list[str]]` for the web form and the wizard popup: keys `slot_limit_enabled` (checkbox: present means on), `slot_limit_max`, `cancel_cutoff_enabled`, `cancel_cutoff_minutes`; returns error texts for a blank, non-numeric or out-of-range number (numbers are validated even when their rule is off); no errors gives the rules.
  - `slots_held(event: EventInfo, user_id: int) -> int` (resolved slots whose `discord_id == user_id` and `dj_page_id` is set).
  - `at_slot_limit(event, user_id, rules) -> bool` (False when the limit is off).
  - `cancellation_closed(event, now: datetime.datetime, rules) -> bool` (False when the cutoff is off or the start is unreadable; closed when `now >= start - M minutes`).
  - Texts: `slot_limit_text(limit: int) -> str`, `no_event_below_limit_text(limit: int) -> str`, `cancellation_closed_text(minutes: int) -> str`, `describe_minutes(minutes: int) -> str` ("1 hour", "90 minutes", "the start of the event" for 0).
  - `rules_summary(rules) -> list[str]`: the two lines used by `/config show` and the wizard summary (`Slot limit: on, 2 per event` / `Slot limit: off`, `Cancellation cutoff: on, 60 minutes before the start` / `off`).

- [ ] **Step 1: Write failing tests** in `tests/test_booking_policy.py`: defaults when keys are missing; `load_rules` with stored `"false"`, `"5"`, `"0"`, `"abc"` (falls back), `"0"` for the limit (out of range, falls back to 2), cutoff `"10081"` (falls back to 60); `db=None` and `guild_id=None` give `DEFAULT_RULES`; `save_rules` then `load_rules` round trip with a fake config store; `parse_rules_form` for valid input, unchecked boxes, blank, `-1`, `1.5`, `101`, `10081`, and number errors reported even when the box is unchecked; `slots_held` counts only the user's slots (use `tests/notion/factories.py` `make_event`/`make_slot`); `at_slot_limit` at N-1, N, N+1 and when disabled; `cancellation_closed` exactly at `start - 60 min` (closed), one second before (open), M = 0 at the start (closed) and one second before (open), disabled (open); `describe_minutes(0|60|120|90|1)`; every text contains the number passed in and no U+2014.
- [ ] **Step 2: Run** `uv run pytest tests/test_booking_policy.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `bot/policy.py` with the signatures above (config access through `bot.db.config.get_value`/`set_value`).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/test_no_em_dash.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: booking rules core with defaults, loading and checks`).

---

### Task 2: Slot limit inside the slot claim

**Files:**
- Modify: `bot/db/bookings.py`, `bot/ui/views.py` (`BookingConfirmView._handle_confirm`, around line 1250)
- Test: `tests/test_booking_policy.py` (mocked), `tests/pg/test_slot_limit.py` (real SQL; follow `tests/pg/conftest.py` and `tests/pg/test_events_repo.py`)

**Interfaces:**
- Consumes: `BookingRules`, `current_rules`, `slot_limit_text` from Task 1.
- Produces: `class DjSlotLimitReached(Exception)` in `bot/db/bookings.py`; `acquire_slot_lock(pool, event_id, slot_index, discord_user_id, max_per_dj: int | None = None) -> bool`. With `max_per_dj` set, inside the same transaction and after the slot row lock, it counts the user's other slots in this event (same event id, `slot_index <> $slot`, limited to the event's current slot count like the existing row lock) and raises `DjSlotLimitReached` when the count is at or above `max_per_dj`, before anything is written. `None` keeps today's behavior (used by `bot/sync/intake.py`).

- [ ] **Step 1: Write failing tests:** mocked-pool test that the count query runs in the same transaction and the claim UPDATE is not executed when at the limit; `max_per_dj=None` runs no count; real-SQL tests: limit 2, DJ holds two slots, third `acquire_slot_lock(..., max_per_dj=2)` raises and the slot stays free; limit 1 with two concurrent `asyncio.gather` claims of different slots by the same DJ: exactly one returns True and one raises; a different DJ is unaffected; a slot of a removed event index does not count. View test: confirm at the limit sends `slot_limit_text(limit)` privately, releases the flow lock and does not call `changed`; confirm with the limit disabled passes `max_per_dj=None`; the passed limit equals the stored number (use 3).
- [ ] **Step 2: Run** `uv run pytest tests/test_booking_policy.py tests/pg/test_slot_limit.py -q` (pg tests may be skipped without a database: say so in the report). Expected: FAIL.
- [ ] **Step 3: Implement** the count and exception in `acquire_slot_lock`; in `_handle_confirm` load `current_rules(db, inter.bot)` before the claim, pass `max_per_dj = rules.slot_limit_max if rules.slot_limit_enabled else None`, catch `DjSlotLimitReached` next to the existing exception handling and answer with `slot_limit_text`.
- [ ] **Step 4: Run** the same tests plus `uv run pytest tests/ui -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: enforce the slot limit when a DJ claims a slot`).

---

### Task 3: Do not offer what would be refused

**Files:**
- Modify: `bot/ui/views.py` (`prepare_booking`, `_filter_events_with_open_slots`, `EventSelectionView._handle_event`, `LineupEventPickView`, `OutreachView.accept`), `bot/services/outreach.py` (`contact_next_dj` and the DJ selection it uses)
- Test: `tests/ui/test_views.py`, `tests/services/test_outreach.py`, `tests/test_booking_policy.py`

**Interfaces:**
- Consumes: `current_rules`, `at_slot_limit`, `no_event_below_limit_text`, `slot_limit_text`.
- Produces: `_filter_events_with_open_slots(events, user_id: int | None = None, rules: BookingRules | None = None)` (additionally drops events where the user is at the limit when both are given). The event picker, the slot picker of a chosen event, the lineup post's "Book a slot" path and the outreach accept path all use it. When events with open slots exist but the DJ is at the limit in every one of them, the DJ gets `no_event_below_limit_text(limit)` (new text, ephemeral) instead of the picker. Opening the slot picker of one specific event at the limit answers with `slot_limit_text(limit)`.
- Outreach: `contact_next_dj` skips a candidate DJ who is at the limit for the invite's event (try the next candidate, as it does for other unreachable cases), and `OutreachView.accept` refuses an accepted invite with `slot_limit_text` when the DJ reached the limit meanwhile, without recording "accepted" as a booking intent beyond the existing status handling.

- [ ] **Step 1: Write failing tests:** `prepare_booking` with a DJ holding 2 of 3 slots in event A and 0 in event B (limit 2): the picker offers only B; at the limit in all events with open slots: error text equals `no_event_below_limit_text(2)` and the flow lock is released; limit disabled: both events offered; limit 3 stored: A offered; `prepare_booking(event_id=A)` at the limit answers `slot_limit_text(2)` with no view; outreach: candidate at the limit is skipped and the next DJ gets the invite, all candidates at the limit gives the existing "no DJ reachable" outcome; accept after reaching the limit answers `slot_limit_text`.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** the filter, the messages and the outreach skip (rules loaded once per flow start, not per event).
- [ ] **Step 4: Run** `uv run pytest tests/ui tests/services/test_outreach.py tests/test_booking_policy.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: hide events and invites a DJ could not book`).

---

### Task 4: Cancellation cutoff

**Files:**
- Modify: `bot/ui/views.py` (`CancellationView._handle_booking`, `CancelConfirmView._handle_confirm`)
- Test: `tests/ui/test_views.py`, `tests/test_booking_policy.py`

**Interfaces:**
- Consumes: `current_rules`, `cancellation_closed`, `cancellation_closed_text`.
- Produces: in `CancellationView._handle_booking`, a closed cutoff answers `cancellation_closed_text(rules.cancel_cutoff_minutes)` privately, releases the flow lock and shows no confirm view; `CancelConfirmView._handle_confirm` checks again before `release_slot_lock` (same text, lock released, slot still held). Time comes from `datetime.datetime.now(datetime.UTC)`; tests patch the clock through the module attribute the code uses (declare one, for example `bot.policy.utc_now`).

- [ ] **Step 1: Write failing tests** for: choose inside the cutoff (refused, no confirm view, flow lock released); choose before the cutoff then confirm after it (refused, `release_slot_lock` not called); before the cutoff everything works as today; cutoff off: always works; M = 0 boundary; text names the stored minutes (use 90: "90 minutes"); an admin cancel (`AdminBookingService.cancel_slot_override`) inside the cutoff still works (assert in Task 5 if easier).
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement** both checks.
- [ ] **Step 4: Run** `uv run pytest tests/ui tests/test_booking_policy.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: close DJ cancellations shortly before the event`).

---

### Task 5: Admin overrides and the settings overview

**Files:**
- Modify: `bot/cogs/admin.py` (`/config show`, around line 198)
- Test: `tests/services/test_admin_booking.py`, `tests/sync` (intake), `tests/cogs/test_admin.py`

**Interfaces:**
- Consumes: `load_rules`, `rules_summary`.
- Produces: `/config show` appends the two `rules_summary` lines after the existing slot lines.

- [ ] **Step 1: Write failing tests:** `book_slot_override` books a DJ who already holds the limit number of slots in the event (limit 2); a Notion hand edit that gives a DJ a third slot is applied (intake test with the same setup); `cancel_slot_override` succeeds inside the cutoff; `/config show` lists both lines for stored values, for "off", and for defaults when nothing is stored.
- [ ] **Step 2: Run** the new tests. Expected: the first three PASS already (they pin existing behavior: do not change those paths), the `/config show` test FAILS.
- [ ] **Step 3: Implement** the `/config show` lines.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: show the booking rules in /config show`).

---

### Task 6: Setup wizard step "Booking rules"

**Files:**
- Modify: `bot/ui/setup_wizard.py`, `bot/ui/texts.py`
- Test: `tests/ui/test_setup_wizard.py`

**Interfaces:**
- Consumes: `BookingRules`, `DEFAULT_RULES`, `load_rules`, `save_rules`, `parse_rules_form`, `rules_summary`.
- Produces: step key `"booking_rules"` in `STEPS` directly before `"summary"` (after `"management_channel"`); control ids `ID_SLOT_LIMIT_TOGGLE = "setup:slot_limit_toggle"`, `ID_CUTOFF_TOGGLE = "setup:cutoff_toggle"`, `ID_RULES_CHANGE = "setup:rules_change"`; draft data keys equal to the four config keys (booleans and ints), prefilled from the stored values (first run: defaults). The step follows the invites step: two toggle buttons (labels show on/off), a "Change" button opening a popup with two text fields ("Slots per DJ and event", "Minutes before the event start") validated by `parse_rules_form`-style checks with the same range messages, Back and Next. Next is always enabled. `_write_all` stores the four keys through `save_rules`; the summary lists `rules_summary`. All three ids are in `_layout`. Draft migration: a draft parked at the old summary index moves to the new one exactly once (next free marker after `steps_v4` in `migrate_legacy_draft`, same mechanism).

- [ ] **Step 1: Write failing tests:** the step key position and `total_steps`; `_layout` contains the three ids plus Back/Next; first-run draft starts with the defaults and a re-run draft with stored values; toggling flips only its rule; the popup accepts valid numbers and rejects `0` for the limit, `101`, blank, `abc`, `10081`; Next enabled; `_write_all` writes the four keys (assert the stored strings); summary lines present; an old draft at the old summary index migrates to the new one, once; activation and save paths persist the rules.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_setup_wizard.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the step, popup, texts, layout, migration and write.
- [ ] **Step 4: Run** `uv run pytest -q`, `uv run ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: booking rules step in the setup wizard`).

---

### Task 7: Admin webpage "Booking rules"

**Files:**
- Modify: `bot/web/app.py`, `bot/web/pages.py`, `bot/web/service.py`
- Test: `tests/web/` (follow the schedule page tests there)

**Interfaces:**
- Consumes: `load_rules`, `save_rules`, `parse_rules_form`, `BookingRules`.
- Produces: routes `GET /booking-rules` and `POST /booking-rules` (CSRF, session and maintenance handling exactly like `/schedule` and `/schedule/defaults`); `AdminPageService.booking_rules() -> BookingRules` and `async set_booking_rules(raw: dict[str, str]) -> ActionResult` (uses `parse_rules_form`, `save_rules`; returns the validation errors joined as the message on failure); `render_booking_rules(rules: BookingRules, csrf: str, message: ActionResult | None, values: dict[str, str] | None) -> str` in `pages.py`, same space theme and helpers as `render_schedule`: two cards (Slot limit, Cancellation cutoff), each with a checkbox and a number input, hint texts from the spec ("Existing bookings are kept...", "0 means DJs can cancel until the event starts. Admins can always cancel."), one "Save booking rules" button; a dashboard card "Booking rules" with a link button next to the Schedule card; a saved change redirects to `/booking-rules?done=saved` showing a confirmation.

- [ ] **Step 1: Write failing tests:** GET shows the stored values (checkboxes and numbers) and the defaults when nothing is stored; unauthenticated GET/POST are refused like other pages; POST without a CSRF token is refused; valid POST stores the four keys and redirects with the confirmation; unchecked boxes store `"false"`; invalid numbers (blank, `-1`, `abc`, `0` limit, `101`, `10081`) return status 400 with the error text, keep the typed values in the form and store nothing; maintenance mode refuses like the schedule page; the dashboard has the link; a saved change is visible in `/config show` terms via `load_rules`.
- [ ] **Step 2: Run** `uv run pytest tests/web -q`. Expected: FAIL.
- [ ] **Step 3: Implement** routes, service methods, page rendering and dashboard link.
- [ ] **Step 4: Run** `uv run pytest -q`, `uv run ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: Booking rules page on the admin webpage`).

---

### Task 8: Docs and final verification

**Files:**
- Modify: `docs/user-guide.md` (setup walkthrough step, a short "Booking rules" subsection covering both rules, the defaults, the admin override and the admin page), `docs/developer-guide.md` (`bot/policy.py`, the config keys, where each rule is enforced, the max_per_dj transaction check, why admin paths pass no limit), `CHANGELOG.md` (`### Added` under `[Unreleased]`: both rules, wizard step, admin page, note that existing installs start with both rules on), the spec only if a call changed during the build.

- [ ] **Step 1: Update** the documents (no em dash).
- [ ] **Step 2: Run** `uv run pytest -q`, `uv run ruff check .`. Expected: all pass.
- [ ] **Step 3: Manual checklist** (needs a live guild; report as not run if unavailable): book a third slot as a DJ (refused, names the limit); change the limit on the admin page and book again; switch the limit off; cancel inside the cutoff (refused), change the cutoff to 0 and 10 minutes; admin books a DJ above the limit; re-run /setup and see the new step with stored values.
- [ ] **Step 4: Commit** (`docs: booking rules`).

---

## Self-review notes

- Spec coverage: both rules with defaults and ranges (Task 1); in-transaction slot limit (Task 2); pickers, invites and no-open-event message (Task 3); cutoff on choose and confirm (Task 4); admin overrides and `/config show` (Task 5); wizard step with migration (Task 6); admin page (Task 7); docs (Task 8).
- Design deviation to tell the user: the wizard step uses toggle buttons plus a "Change" popup for the two numbers (the pattern of the existing invites step) instead of the dropdowns shown in the mockup. Same fields and ranges.
- Names used across tasks: `BookingRules`, `current_rules`, `load_rules`, `save_rules`, `parse_rules_form`, `at_slot_limit`, `cancellation_closed`, `slot_limit_text`, `no_event_below_limit_text`, `cancellation_closed_text`, `rules_summary`, `DjSlotLimitReached`, `max_per_dj`.
