# /cancel event Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin command `/cancel event` that, after a confirm step, sets one upcoming event to `Canceled` in Notion, removes its bookings, DMs the booked DJs, reports unreachable DJs in the log channel and announces the cancel in the DJ channel when DJs were booked.

**Architecture:** A new `EventCancelService` (`bot/services/event_cancel.py`) does the work in a fixed order: status write, booking cleanup, DMs, log entry, DJ channel post, lineup refresh. The admin cog only adds the sub command and a two-button confirm view. Outreach, reminders, the fully-booked check and the Notion sync learn to skip canceled events through one shared status constant.

**Tech Stack:** Python 3.13, disnake 2.12.0, asyncpg, pytest (pytest-asyncio, pytest-mock), ruff (line length 100), pyright.

**Spec:** `docs/superpowers/specs/2026-11-16-cancel-event-design.md`

All commands run in `/home/godfather/SoftwareProjects/DiscordRaveBot/output/ravebot`. Work on branch `feat/cancel-event` (create it from `main` before Task 1). Test command: `uv run pytest -q`. Lint: `uv run ruff check . && uv run pyright`.

## Global Constraints

- English only in code, texts and docs; no em dashes in texts (`tests/test_no_em_dash.py`).
- The Notion status value is exactly `Canceled`. User-facing texts about the event say "canceled". Existing booking texts ("cancelled") stay unchanged.
- Order inside the service is fixed: status write first; if it fails, nothing else is changed and nobody is notified.
- Nothing is sent or changed before the admin clicks "Cancel event".
- A failed DM always produces a log channel entry. Guests (Discord id <= 0) and Notion DJs without a Discord ID always produce a "tell them by hand" entry; `dm_user` is never called for them.
- No DJ booked: log channel entry only. One or more DJs booked: log channel entry plus DJ channel post.
- No Notion schema change, no migration.
- Services return results and never raise to the cog. All user text lives in `bot/ui/texts.py`; names are escaped with `texts.escape_text`; every send uses `allowed_mentions=disnake.AllowedMentions.none()`.
- Commits: author and committer `M4XXX <44500247+M4XXX@users.noreply.github.com>`, no AI attribution lines, check the staged diff for secrets first. Never `git add docs/superpowers` (planning files stay out of the repo).

## Review Focus

- A DJ books a slot between the confirm preview and the button click: that booking is canceled and the DJ is notified. Pinned in Task 4 (`cancel_event` reads the bookings itself).
- Two admins confirm the same event, or one double-clicks: the second run answers "already canceled" and sends nothing. Task 4.
- Notion and the database disagree: a DJ in a Notion slot row without a lock (hand edit not yet synced), or a lock without a Notion DJ. Both are cleaned up and notified once. Task 4.
- Cleanup of one slot fails in Notion: the lock is still released, and the next sync must not re-book the DJ from the stale Notion row or DM "you are in the lineup". Task 2 (sync skips canceled events) and Task 4.
- A DJ confirms a booking, or accepts an outreach invite, for an event that was canceled a moment ago: the booking is refused with a clear text. Task 6.
- More than 15 booked DJs, or names with markdown or mentions: the confirm message stays under Discord's 2000 characters and nothing is pinged. Task 3 and Task 5.

---

### Task 1: Shared status constants and Notion writes

**Files:**
- Modify: `bot/notion/models.py` (constants next to the `PROP_*` block)
- Modify: `bot/notion/client.py` (two methods after `clear_slot`)
- Modify: `bot/services/lineup.py`, `bot/services/event_replenish.py` (use the shared constant, delete `_EXCLUDED_STATUSES` and `_FINISHED_STATUSES`)
- Test: `tests/notion/test_client_events.py`, `tests/notion/test_client_slots.py`

**Interfaces:**
- Produces:
  - `bot.notion.models.STATUS_CANCELED = "Canceled"`
  - `bot.notion.models.FINISHED_STATUSES: frozenset[str] = frozenset({"Done", "Canceled"})`
  - `NotionClient.set_event_status(self, event: EventInfo, status: str) -> None`: PATCH `/pages/{event.page_id}` with `{"properties": {"Status": {"select": {"name": status}}}}`, description `f"set status of {event.event_id} to {status}"`. Raises `NotionWriteError` after failed retries (through `self._api.request`, like `_write_slot_djs`).
  - `NotionClient.empty_slot(self, event: EventInfo, slot_index: int) -> None`: writes an empty DJ relation to the slot row found by `self._slot_row(event, slot_index + 1)`, description `f"empty slot {slot_index + 1} of {event.event_id}"`. Removes every DJ of the row, including extra ones. Raises `SlotRowMissingError` or `NotionWriteError`.

- [ ] **Step 1: Write the failing tests** in the style of the existing fake-Notion tests (`fake.requests_to`, the `_patches` helper in `test_client_slots.py`):

```python
async def test_set_event_status_patches_the_status_select(...):
    await client.set_event_status(event, "Canceled")
    method, path, body = <the one PATCH request>
    assert path == f"/pages/{event.page_id}"
    assert body == {"properties": {"Status": {"select": {"name": "Canceled"}}}}

async def test_empty_slot_writes_an_empty_relation(...):
    # slot 2 holds two DJs (one extra)
    await client.empty_slot(event, 1)
    assert <PATCH body for the slot 2 row> == {"properties": {"DJ": {"relation": []}}}

async def test_empty_slot_without_a_row_raises(...):
    with pytest.raises(SlotRowMissingError):
        await client.empty_slot(event_without_row_3, 2)
```

- [ ] **Step 2: Run** `uv run pytest tests/notion -q`. Expected: the three new tests fail with `AttributeError`.
- [ ] **Step 3: Implement** the constants and the two methods; replace the two private status sets with `FINISHED_STATUSES`.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: Notion writes for event status and emptying a slot`

---

### Task 2: Canceled events are ignored by outreach, reminders, fully-booked and the sync

**Files:**
- Modify: `bot/services/outreach.py` (`run_gap_detection`), `bot/services/reminders.py` (`send_reminders`), `bot/services/fully_booked.py` (`check_fully_booked`), `bot/services/notion_sync.py` (`run_cycle`)
- Test: `tests/services/test_outreach.py`, `tests/services/test_reminders.py`, `tests/services/test_fully_booked.py`, `tests/services/test_notion_sync.py`

**Interfaces:**
- Consumes: `FINISHED_STATUSES`, `STATUS_CANCELED` from Task 1.

Rules:
- Outreach: an event with a status in `FINISHED_STATUSES` is skipped before the window check. If it has an outreach sequence with status `active`, that sequence is marked complete (`mark_sequence_complete`), so no further invites go out.
- Reminders: events with a status in `FINISHED_STATUSES` are dropped from `upcoming`.
- Fully booked: events with a status in `FINISHED_STATUSES` are neither announced nor do they clear a flag.
- Sync: `run_cycle` drops events whose status is `STATUS_CANCELED` before reading locks and planning (Done events keep syncing, as today).

- [ ] **Step 1: Write the failing tests** (use `make_event(status="Canceled")` from `tests/notion/factories.py`; mirror the neighbor tests in each file):

```python
async def test_run_gap_detection_skips_a_canceled_event():        # returns [], upsert_sequence not called
async def test_run_gap_detection_completes_the_active_sequence_of_a_canceled_event():  # mark_sequence_complete called once
async def test_run_gap_detection_skips_a_done_event():
async def test_send_reminders_skips_a_canceled_event():           # booked DJ in the window, no DM, nothing recorded
async def test_check_fully_booked_ignores_a_canceled_full_event():# log_fully_booked not called, mark_announced not called
async def test_run_cycle_ignores_canceled_events():               # Notion row holds a DJ, no lock: no action planned, no DM
```

- [ ] **Step 2: Run** the four test files. Expected: the new tests fail.
- [ ] **Step 3: Implement** the four filters.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `fix: canceled events get no outreach, reminders, fully-booked entry or sync`

---

### Task 3: Texts and the log entry

**Files:**
- Modify: `bot/ui/texts.py`, `bot/log.py`
- Test: `tests/test_texts.py`, `tests/test_log.py`

**Interfaces:**
- Produces in `bot/ui/texts.py` (exact copy; `{slots}` is `slot_phrase` per slot joined with `", "`, `{day}` is `event.date.strftime("%A %d.%m.%Y")`; DJ and admin names are passed through `escape_text` by these functions):
  - `dm_event_canceled(event: EventInfo, slot_indexes: Sequence[int]) -> str`
    `"The event {event_id} was canceled by an organizer. Your booking ({slots}) is canceled too."`
  - `dj_line(name: str, event: EventInfo, slot_indexes: Sequence[int]) -> str` gives `"- {name}, {slots}"`
  - `LOG_EVENT_CANCELED_TITLE = "Event canceled"`
  - `log_event_canceled_text(admin: str, event: EventInfo, dj_lines: Sequence[str]) -> str`
    `"{admin} canceled {event_id}."` then `"\nNo DJ was booked."` or `"\nBooked DJs:\n" + lines`
  - `dj_channel_event_canceled(event: EventInfo) -> str` gives `"**{event_id}** on {day} is canceled."`
  - `event_canceled_dm_failed(name: str, event: EventInfo) -> str`
    `"The notice that {event_id} was canceled could not be delivered to {name} because their DMs are closed or the bot cannot reach them."`
  - `event_canceled_no_discord(name: str, event: EventInfo) -> str`
    `"{name} has no Discord account, so the bot could not tell them that {event_id} was canceled."`
  - `EVENT_CANCELED_ANYWAY = "The event was canceled anyway."`
  - `tell_canceled_todo(name: str) -> str` gives `"Tell {name} yourself that the event is canceled."`
  - `event_cancel_cleanup_failed(event: EventInfo, slot_indexes: Sequence[int]) -> str`
    `"{event_id} was canceled, but these slots could not be emptied in Notion: {numbers}."` (numbers are 1-based, joined with `", "`)
  - `EVENT_CANCEL_CLEANUP_TODO = "Remove the DJs from these Slots rows in Notion."`
  - `EVENT_CANCEL_POST_FAILED = "The cancel notice could not be posted in the DJ channel."`
  - `cancel_event_confirm(event: EventInfo, dj_lines: Sequence[str], by_hand: Sequence[str]) -> str`: first line `"Cancel the event **{event_id}**?"`; with no lines `"No DJ is booked."`; otherwise `"These DJs are booked and will be told by DM:"` plus at most 15 lines, then `"and {n} more"` if cut; if `by_hand` is not empty a last line `"Without a Discord account, tell them yourself: {names}"`.
  - `EVENT_CANCEL_KEPT = "Nothing was canceled."`
  - `event_cancel_done(event: EventInfo, notified: Sequence[str], by_hand: Sequence[str], failed_slots: Sequence[int]) -> str`: `"Canceled {event_id}."`, then `" {n} DJ(s) told by DM."` when n > 0, `" Tell these DJs yourself: {names}."` when not empty, `" Some slots could not be emptied in Notion, see the log channel."` when `failed_slots` is not empty. Use "1 DJ" / "2 DJs", not "DJ(s)".
  - `EVENT_NOT_CANCELABLE = "That event was not found, is in the past, or is already done or canceled."`
  - `EVENT_CANCEL_NOTION_FAILED = "Notion could not be updated. The event was not canceled and nobody was told."`
  - `EVENT_WAS_CANCELED = "This event was canceled. Open the menu again to pick another event."`
- Produces in `bot/log.py`:
  - `AdminLogService.log_event_canceled(self, admin_name: str, event: EventInfo, dj_lines: Sequence[str]) -> None`: a `LogEntryEmbed` with `LOG_EVENT_CANCELED_TITLE`, `log_event_canceled_text(...)`, `EmbedColors.MISSION_GOLD`, built through `_post_built` like `log_cancelled`.

- [ ] **Step 1: Write the failing tests**

```python
def test_dm_event_canceled_lists_every_slot():
    # event "2026-11-28 Rave Night", start 22:00, 30 min slots
    assert texts.dm_event_canceled(event, [0, 2]) == (
        "The event 2026-11-28 Rave Night was canceled by an organizer. "
        "Your booking (slot 1 at 22:00, slot 3 at 23:00) is canceled too."
    )

def test_dj_channel_event_canceled():
    assert texts.dj_channel_event_canceled(event) == (
        "**2026-11-28 Rave Night** on Saturday 28.11.2026 is canceled."
    )

def test_log_event_canceled_text_without_and_with_djs(): ...
def test_cancel_event_confirm_caps_at_15_lines():      # 20 lines in: 15 shown, "and 5 more", len < 2000
def test_cancel_event_confirm_without_bookings():      # contains "No DJ is booked."
def test_event_cancel_done_counts():                   # "1 DJ told by DM." / "2 DJs told by DM."
def test_names_are_escaped():                          # "**DJ** @everyone" arrives escaped in dj_line and the todo
async def test_log_event_canceled_posts_one_gold_entry():   # in tests/test_log.py, like the log_cancelled test
```

- [ ] **Step 2: Run** `uv run pytest tests/test_texts.py tests/test_log.py tests/test_no_em_dash.py -q`. Expected: new tests fail.
- [ ] **Step 3: Implement** the texts and `log_event_canceled`.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: texts and log entry for a canceled event`

---

### Task 4: EventCancelService

**Files:**
- Create: `bot/services/event_cancel.py`
- Modify: `bot/context.py` (field `event_cancel_service: "Any | None" = None`), `bot/main.py` (after `ctx.dj_admin_service = ...`: `ctx.event_cancel_service = EventCancelService(ctx, bot)`)
- Test: `tests/services/test_event_cancel.py` (new; build the mocks like `_make_service` and `_patch_db` in `tests/services/test_admin_booking.py`), `tests/test_context.py` or `tests/test_main.py` if they assert the service list

**Interfaces:**
- Consumes: `set_event_status`, `empty_slot`, `STATUS_CANCELED`, `FINISHED_STATUSES` (Task 1); the texts and `log_event_canceled` (Task 3); existing `get_slot_locks_for_events`, `release_slot_lock` (`bot.db.bookings`), `get_dj_row` (`bot.db.djs`), `dm_user` (`bot.services.dm`), `get_value` (`bot.db.config`, key `"dj_channel"`), `future_events`, `_today` (`bot.services.lineup`), `AdminLogService.post_error`, `log_attention`, `ErrorCode.ERR_DM_FAILED`, `LineupService.notify_changed`.
- Produces:

```python
@dataclass(frozen=True)
class BookedDj:
    dj_id: int | None            # Discord id; <= 0 is a guest; None: Notion DJ without Discord ID
    name: str
    slot_indexes: tuple[int, ...]  # zero-based, ascending

    @property
    def reachable(self) -> bool: ...   # dj_id is not None and dj_id > 0

@dataclass(frozen=True)
class CancelPreview:
    event: EventInfo
    djs: tuple[BookedDj, ...]

@dataclass(frozen=True)
class CancelResult:
    ok: bool
    message: str                    # ready for the admin
    notified: tuple[str, ...] = ()  # names told by DM
    by_hand: tuple[str, ...] = ()   # names to tell by hand (failed DM, guest, no Discord ID)
    failed_slots: tuple[int, ...] = ()  # zero-based slots not emptied in Notion

class EventCancelService:
    def __init__(self, ctx: BotContext, bot: commands.Bot) -> None: ...
    async def preview(self, event_name: str) -> CancelPreview | None: ...
    async def cancel_event(self, admin_name: str, event_name: str) -> CancelResult: ...
```

Behavior:
- The service reads `ctx.db`, `ctx.notion_client`, `ctx.log_service`, `ctx.lineup_service` at call time. Any of db or notion missing: `preview` returns None, `cancel_event` returns `CancelResult(False, texts.GENERIC_ERROR)`.
- Finding the event: `list_events()`, then the event from `future_events(events, _today())` whose `event_id` matches case-insensitively. Not found (unknown, past, Done, Canceled): `preview` returns None; `cancel_event` returns `CancelResult(False, texts.EVENT_NOT_CANCELABLE)`. A failed `list_events` is treated the same in `preview` and returns `CancelResult(False, texts.GENERIC_ERROR)` in `cancel_event`.
- Booked DJs (one private helper used by both methods), per zero-based slot index:
  - holder = lock holder from `get_slot_locks_for_events(db, [event.page_id])` if set, else the `discord_id` of the row in `event.resolved_slots()` if that row has a `dj_page_id`.
  - a row with a `dj_page_id` but no lock and no `discord_id` gives a `BookedDj(dj_id=None, ...)`, grouped by `dj_page_id`.
  - name: the Notion row's `dj_name` when the row belongs to that holder; else `get_dj_row(db, id)["dj_name"]`; else `str(id)`.
  - one `BookedDj` per DJ with all their slot indexes; result sorted by first slot index.
- `cancel_event` steps, each in its own try/except so a later step always runs:
  1. Find the event (above).
  2. `set_event_status(event, STATUS_CANCELED)`. On any exception: return `CancelResult(False, texts.EVENT_CANCEL_NOTION_FAILED)`. Nothing else runs.
  3. Read the booked DJs now (not from a preview). If this read fails: log attention entry with `texts.GENERIC_ERROR` context, continue with an empty list (the event is already canceled).
  4. For every slot index with a Notion DJ (`dj_page_id` or `extra_dj_page_ids`): `empty_slot(event, index)`; a failure adds the index to `failed_slots`. For every slot index with a lock holder: `release_slot_lock(db, event.page_id, index)` (no expected holder), always, also when the Notion write failed.
  5. For every reachable DJ: `dm_user(bot, dj_id, texts.dm_event_canceled(event, slot_indexes))`. True: name goes to `notified`. False: name goes to `by_hand` and `post_error(ErrorCode.ERR_DM_FAILED, texts.event_canceled_dm_failed(name, event), texts.EVENT_CANCELED_ANYWAY, texts.dm_failed_todo(name))`.
  6. For every DJ that is not reachable: name goes to `by_hand`, `post_error(ErrorCode.ERR_DM_FAILED, texts.event_canceled_no_discord(name, event), texts.EVENT_CANCELED_ANYWAY, texts.tell_canceled_todo(name))`. `dm_user` is not called.
  7. `log_service.log_event_canceled(admin_name, event, [texts.dj_line(...) for each DJ])`. If `failed_slots`: `log_attention(texts.LOG_FAILED_TITLE, texts.event_cancel_cleanup_failed(event, failed_slots), texts.EVENT_CANCEL_CLEANUP_TODO)`.
  8. If at least one DJ was booked: send `texts.dj_channel_event_canceled(event)` to the channel `bot.get_channel(int(dj_channel))` with mentions disabled. Missing config, missing channel or a send error: `log_attention(texts.LOG_FAILED_TITLE, texts.EVENT_CANCEL_POST_FAILED, texts.DJ_CHANNEL_TODO)`.
  9. `lineup_service.notify_changed()` if the service exists.
  10. Return `CancelResult(True, texts.event_cancel_done(event, notified, by_hand, failed_slots), notified, by_hand, failed_slots)`.
- The guild id for `get_value` is `bot.guilds[0].id` (as `LineupService._get_guild_id`); no guild counts as a missing DJ channel.

- [ ] **Step 1: Write the failing tests** (event: `2026-11-28 Rave Night`, 4 slots; patch `bot.services.event_cancel._today` to a date before it):

```python
async def test_cancel_without_bookings_logs_only():
    # result.ok, set_event_status called with (event, "Canceled"), log_event_canceled called with dj_lines == [],
    # no channel.send, no dm, notify_changed called once
async def test_cancel_with_two_djs_dms_logs_and_posts_in_the_dj_channel():
    # two user.send calls, channel.send called once with texts.dj_channel_event_canceled(event),
    # empty_slot and release_slot_lock called for both slots, result.notified has both names
async def test_dj_with_two_slots_gets_one_dm():
    # one send, text == texts.dm_event_canceled(event, [0, 2])
async def test_blocked_dm_is_reported():
    # user.send raises disnake.Forbidden: post_error called with ErrorCode.ERR_DM_FAILED and
    # manual_action == texts.dm_failed_todo(name); name in result.by_hand; DJ channel post still sent
async def test_guest_gets_a_by_hand_entry_and_no_dm_attempt():
    # lock holder -5: bot.get_user / fetch_user never called, post_error manual_action == texts.tell_canceled_todo(name)
async def test_notion_dj_without_lock_and_without_discord_id_is_by_hand():
async def test_notion_dj_without_lock_is_notified_and_slot_emptied():   # hand edit not yet synced
async def test_lock_without_notion_dj_is_released_and_notified():
async def test_status_write_failure_changes_nothing():
    # set_event_status raises NotionWriteError: not ok, message == texts.EVENT_CANCEL_NOTION_FAILED,
    # empty_slot, release_slot_lock, dm, log_event_canceled, channel.send all not called
async def test_slot_cleanup_failure_continues():
    # empty_slot raises for slot 1 only: slot 3 still emptied, both locks released, both DJs told,
    # result.failed_slots == (0,), log_attention called with texts.EVENT_CANCEL_CLEANUP_TODO
async def test_missing_dj_channel_is_reported_and_cancel_still_succeeds():
async def test_unknown_past_done_and_canceled_events_are_refused():      # parametrized; set_event_status not called
async def test_second_cancel_of_the_same_event_sends_nothing():
    # list_events returns the event with status "Canceled": not ok, EVENT_NOT_CANCELABLE, no DM
async def test_cancel_reads_bookings_at_cancel_time():
    # preview() sees one DJ; the lock mock then returns two holders; cancel_event notifies two
async def test_preview_lists_booked_djs_and_changes_nothing():
async def test_preview_returns_none_for_an_unknown_event():
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_event_cancel.py -q`. Expected: fail with `ModuleNotFoundError`.
- [ ] **Step 3: Implement** the service and the wiring in `bot/context.py` and `bot/main.py`.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: EventCancelService cancels an event and notifies its DJs`

---

### Task 5: `/cancel event` command, confirm view, docs

**Files:**
- Modify: `bot/cogs/admin.py` (sub command on `cancel_base`, autocomplete, `_ConfirmCancelEvent` next to `_ConfirmDelete`, command tree in the module docstring; change the `cancel_base` description to `"Cancel bookings or events (admin only)"`)
- Modify: `README.md` (command table near line 225), `docs/user-guide.md` (command table near line 378 and the log entry table near line 498), `docs/developer-guide.md` (lines 94 and 170), `CHANGELOG.md` (`## [Unreleased]`, under `### Added`)
- Test: `tests/cogs/test_admin.py`

**Interfaces:**
- Consumes: `ctx.event_cancel_service` with `preview` and `cancel_event`, `CancelPreview`, `BookedDj`, `CancelResult` (Task 4); `texts.cancel_event_confirm`, `texts.dj_line`, `EVENT_NOT_CANCELABLE`, `EVENT_CANCEL_KEPT`, `GENERIC_ERROR` (Task 3).
- Produces:
  - `AdminCog.cancel_event(self, inter, event: str = commands.Param(description="The event to cancel"))`, registered with `@cancel_base.sub_command(name="event", description="Cancel an upcoming event and tell its DJs")`.
  - `@cancel_event.autocomplete("event")` delegating to `future_event_autocomplete`.
  - `class _ConfirmCancelEvent(disnake.ui.View)` with `__init__(self, ctx, service, event_name: str, admin_id: int, inter)`, `timeout=120`, buttons "Cancel event" (danger) and "Keep event" (secondary), `interaction_check` as in `_ConfirmDelete`.

Behavior:
- Service is None: `"The bot is not ready yet. Please try again in a moment."` (private, no defer), as `/dj remove` does.
- Otherwise defer privately, call `preview(event)`. None: follow up with `texts.EVENT_NOT_CANCELABLE`. Else follow up with `texts.cancel_event_confirm(preview.event, lines of reachable DJs, names of DJs that are not reachable)` and the view.
- "Cancel event": `self.stop()`, defer, `cancel_event(admin_name=inter.author.name, event_name=...)`, edit the original message to `result.message` with `view=None`. An exception gives `texts.GENERIC_ERROR`.
- "Keep event": `self.stop()`, edit to `texts.EVENT_CANCEL_KEPT`, `view=None`.
- `on_timeout`: edit the original message (through the stored command interaction) to `texts.EVENT_CANCEL_KEPT` with `view=None`; a failed edit is swallowed.

Docs copy: command table row `/cancel event event:<event>` with "Cancels an upcoming event. Booked DJs are told by DM; DJs the bot cannot reach are listed in the log channel." Log table row `Event canceled`: "An organizer canceled an event with `/cancel event`." Changelog line: "`/cancel event`: cancel one upcoming event; booked DJs are told by DM, the log channel names DJs to tell by hand, and the DJ channel gets a notice when DJs were booked. Canceled events no longer get outreach, reminders or a fully-booked entry."

- [ ] **Step 1: Write the failing tests** (helpers `make_mock_ctx`, `make_mock_inter`, `assert_deferred_reply` already exist in the file):

```python
async def test_cancel_event_not_ready_when_service_none():
async def test_cancel_event_unknown_event_replies_error():        # preview -> None: EVENT_NOT_CANCELABLE, no view
async def test_cancel_event_shows_the_confirm_view_and_changes_nothing():
    # followup text == texts.cancel_event_confirm(...), kwargs["view"] is a _ConfirmCancelEvent, cancel_event not awaited
async def test_cancel_event_confirm_lists_a_guest_as_by_hand():
async def test_confirm_button_runs_the_service_and_shows_the_result():
    # cancel_event awaited once with admin_name and event_name; edit_original_message(content=result.message, view=None)
async def test_keep_button_changes_nothing():
async def test_confirm_view_rejects_another_user():               # interaction_check False for a different admin id
async def test_confirm_view_timeout_edits_the_message():
async def test_cancel_event_autocomplete_uses_future_events(monkeypatch):
```

- [ ] **Step 2: Run** `uv run pytest tests/cogs/test_admin.py -q`. Expected: new tests fail.
- [ ] **Step 3: Implement** the command, the view and the docs changes.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `feat: /cancel event with a confirm step`

---

### Task 6: No booking into a canceled event

**Files:**
- Modify: `bot/ui/views.py` (`BookingConfirmView._handle_confirm`, `OutreachView.accept`)
- Test: `tests/ui/test_views.py`

**Interfaces:**
- Consumes: `FINISHED_STATUSES` (Task 1), `texts.EVENT_WAS_CANCELED` (Task 3).

Behavior:
- `BookingConfirmView._handle_confirm`: after the db and Notion guards and before `acquire_slot_lock`, load `list_events()` and look up the event by `page_id`. If the fresh event has a status in `FINISHED_STATUSES`: release the flow (`self._release_flow()`), follow up with `texts.EVENT_WAS_CANCELED` (ephemeral), stop. No lock is taken and Notion is not written. If the read fails or the event is not in the list, continue as today.
- `OutreachView.accept`: after the fresh event is loaded, a status in `FINISHED_STATUSES` releases the lock and follows up with `texts.EVENT_WAS_CANCELED` instead of offering slots.

- [ ] **Step 1: Write the failing tests** (mirror the existing confirm and accept tests in the file):

```python
async def test_booking_confirm_refuses_a_canceled_event():
    # list_events returns the event with status "Canceled": acquire_slot_lock not called,
    # assign_slot not called, followup text == texts.EVENT_WAS_CANCELED, flow lock released
async def test_booking_confirm_continues_when_the_event_reload_fails():
async def test_outreach_accept_refuses_a_canceled_event():
```

- [ ] **Step 2: Run** `uv run pytest tests/ui/test_views.py -q`. Expected: new tests fail.
- [ ] **Step 3: Implement** both guards.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check . && uv run pyright`. Expected: all pass.
- [ ] **Step 5: Commit** `fix: booking and invite accept refuse a canceled event`
