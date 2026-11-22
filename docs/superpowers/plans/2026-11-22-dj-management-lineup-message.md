# DJ Management Lineup Message Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post and keep current a lineup message (with copyable streaming links) in an optional DJ Management channel when an event is fully booked, plus an admin-only "Send me more information" button that DMs the full details.

**Architecture:** A new `ManagementLineupService` owns the messages. It renders pure embeds (`bot/ui/management_lineup.py`), stores `channel_id` plus message ids per event in a new table, and refreshes through a debounced `notify_changed()` that existing change points call. A persistent `ManagementInfoView` button resolves the event from the clicked message id and checks `ctx.is_admin` at click time. A new optional setup wizard step stores the channel and asks "post all existing or only new" once.

**Tech Stack:** Python, disnake, asyncpg, Alembic, pytest (asyncio, mocks), ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-11-22-dj-management-lineup-message-design.md`

## Global Constraints

- All text, code, comments and docs in English.
- No em dash (U+2014) in user-facing strings under `bot/` (`tests/test_no_em_dash.py` enforces it).
- Channel message shows DJ name, streaming link and alternative streaming link only. Discord name, VRChat name and logo appear only in the admin DMs.
- Button permission check: `ctx.is_admin(user_id)` at click time. Non-admin gets an ephemeral refusal and no data.
- At most one logical lineup per event. A refill edits the stored message(s), never posts a second one.
- Message ids survive the fully-booked flag being cleared.
- Finished events (`FINISHED_STATUSES`: Done, Canceled) get no new message. A Canceled event with a stored message is edited once to say so; Done is left as is. The bot never deletes lineup messages.
- Feature is inert when `management_channel` is not set. It never blocks booking or raises into callers.
- Schema changes via Alembic (next revision is `0020`, `down_revision = "0019"`).
- All AI-generated files stay in this repo's tree. Commits: author and committer `M4XXX <44500247+M4XXX@users.noreply.github.com>`, no AI attribution or co-author lines in messages. Check staged files for secrets before each commit.
- Verification commands (run from `output/ravebot`): `uv run pytest -q`, `uv run ruff check .`, `uv run pyright`.

## Review Focus

- Bot restarts between posting and editing: the stored ids are used, no duplicate post (Task 3 test).
- Stored message was deleted by hand: next refresh posts a new one and stores new ids (Task 3 test).
- DJ has no streaming link, no alternative link, no logo, or is a guest (negative id, no Discord account): message shows "none", DM shows "no logo" and no Discord name (Tasks 2 and 4 tests).
- Lineup too long for one Discord message (many slots, long links): split across several messages, button on the first only, all edited together (Tasks 2 and 3 tests).
- Admin has DMs closed: private note, no crash, no partial silent failure (Task 4 test).
- Channel deleted or bot lost permission: no exception, one admin log entry per problem, booking unaffected (Task 3 test).
- Admin changes the channel in setup: old messages keep being edited in their stored channel, new events post to the new channel (Task 3 test).
- Channel cleared in setup: feature stops posting and editing (Task 6 test).

---

## File Structure

- Create `migrations/versions/0020_management_lineup.py`: table `management_lineup_messages`.
- Create `bot/db/management_lineup.py`: table access.
- Modify `bot/db/config.py`: add `delete_value`.
- Modify `bot/db/djs.py`: add `get_dj_details`.
- Create `bot/ui/management_lineup.py`: pure builders (channel embeds, DM payloads) and `ManagementInfoView`.
- Modify `bot/ui/texts.py`: new strings.
- Create `bot/services/management_lineup.py`: `ManagementLineupService` and `notify_management(ctx)`.
- Modify `bot/context.py`: field `management_lineup_service`.
- Modify `bot/main.py`: build the service, register the persistent view.
- Modify change points (Task 5) to call `notify_management`.
- Modify `bot/ui/setup_wizard.py`, `bot/cogs/admin.py` (config listing): the channel step and backfill.
- Modify `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md`.
- Tests under `tests/db`, `tests/ui`, `tests/services`, plus `tests/test_migration_0020.py`.

Design change from the spec's own calls: the button uses a static custom id `mgmt:info` and resolves the event by the clicked message id (looked up in the table), instead of embedding the event key in the custom id. Reason: disnake persistent views need exact custom ids. Update the spec's "Button lookup" line accordingly in Task 7.

---

### Task 1: Storage (migration, db module, config delete, DJ details)

**Files:**
- Create: `migrations/versions/0020_management_lineup.py`, `bot/db/management_lineup.py`
- Modify: `bot/db/config.py`, `bot/db/djs.py`
- Test: `tests/test_migration_0020.py`, `tests/db/test_management_lineup.py`, additions to `tests/db/test_config.py` and `tests/db/test_djs.py`

**Interfaces:**
- Produces (`bot/db/management_lineup.py`, all `async`, first arg `pool: asyncpg.Pool`):
  - `@dataclass(frozen=True) class LineupRecord: event_id: str; channel_id: int; message_ids: tuple[int, ...]; content_hash: str`
  - `get_record(pool, event_id: str) -> LineupRecord | None`
  - `list_records(pool) -> list[LineupRecord]`
  - `get_record_by_message(pool, message_id: int) -> LineupRecord | None` (matches any id in `message_ids`)
  - `upsert_record(pool, event_id: str, channel_id: int, message_ids: Sequence[int], content_hash: str) -> None`
  - `delete_record(pool, event_id: str) -> None`
- Empty `message_ids` means "suppressed": the event was fully booked when the admin chose "Only new", so no message is posted until the event opens up and fills again.
- Produces: `bot.db.config.delete_value(pool, guild_id: int, key: str) -> None`.
- Produces: `bot.db.djs.get_dj_details(pool, user_ids: Sequence[int]) -> dict[int, dict]` with keys `discord_user_id, dj_name, vrchat_name, stream_url_primary, stream_url_backup, logo_url, has_logo` (one query, `= ANY($1)`).

- [ ] **Step 1: Write failing tests.** Migration test follows `tests/test_migration_0019.py` (revision ids, upgrade SQL mentions `management_lineup_messages`, columns `event_id TEXT PRIMARY KEY`, `channel_id BIGINT NOT NULL`, `message_ids BIGINT[] NOT NULL DEFAULT '{}'`, `content_hash TEXT NOT NULL DEFAULT ''`, `updated_at`). DB tests follow `tests/db/test_fully_booked.py` (mocked pool; assert SQL fragments and arguments): `upsert_record` uses `ON CONFLICT (event_id) DO UPDATE`; `get_record_by_message` SQL contains `= ANY(message_ids)`; `get_record` returns `None` for no row and a `LineupRecord` with a tuple of ints otherwise; `delete_value` issues `DELETE FROM config WHERE guild_id = $1 AND key = $2`; `get_dj_details` returns `{}` for an empty id list without querying, and keys rows by `discord_user_id`.
- [ ] **Step 2: Run** `uv run pytest tests/test_migration_0020.py tests/db/test_management_lineup.py tests/db/test_config.py tests/db/test_djs.py -q`. Expected: FAIL (missing modules and functions).
- [ ] **Step 3: Implement** the migration (also drop the table in `downgrade`), the db module, `delete_value` and `get_dj_details` with the signatures above.
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/test_migration_chain.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: storage for DJ management lineup messages`).

---

### Task 2: Rendering (channel embeds, DM payloads, texts)

**Files:**
- Create: `bot/ui/management_lineup.py`
- Modify: `bot/ui/texts.py`
- Test: `tests/ui/test_management_lineup.py`

**Interfaces:**
- Consumes: `EventInfo`, `SlotInfo` (`bot/notion/models.py`), `EmbedColors` and `_safe_dj_name` (`bot/ui/embeds.py`), DJ detail dicts from Task 1 (`get_dj_details`), `EventInfo.slot_start(index)`.
- Produces in `bot/ui/management_lineup.py`:
  - `LineupState = Literal["booked", "open", "canceled"]`
  - `lineup_state(event: EventInfo) -> LineupState`: `canceled` when `event.status == STATUS_CANCELED`, `booked` when `is_fully_booked(event)`, else `open`.
  - `build_channel_messages(event: EventInfo, djs: dict[int, dict], state: LineupState) -> list[list[disnake.Embed]]`: one inner list per Discord message. Every message stays under 6000 characters and 10 embeds. Slots are never split across messages.
  - `content_hash(messages: list[list[disnake.Embed]]) -> str`: stable hash of `embed.to_dict()` values, used to skip no-op edits.
  - `@dataclass(frozen=True) class DmPayload: text: str; logo: bytes | None; logo_filename: str`
  - `build_dm_payloads(event: EventInfo, djs: dict[int, dict], discord_names: dict[int, str], logos: dict[int, bytes]) -> list[DmPayload]`: first item is the header (event, date, genre, location), then one payload per booked slot in slot order.
- Message rules: title has the event id and date. A description block lists start time and slot length (local times via `slot_start`), genre, location and notes when set. Per slot: `Slot N, HH:MM` then DJ name, then `Stream: <url>` and `Alt stream: <url>` each on its own line as plain text. Missing link or DJ: `none`. An unbooked slot shows `open`. State `open` adds the note "No longer fully booked: open slots are marked." State `canceled` replaces the lineup with "This event was canceled." Use `_safe_dj_name` for DJ names. Use plain URLs, no markdown link labels.
- DM payload rules: per DJ the lines are slot and time, DJ name, Discord name (`none` for guests or unknown), VRChat name, streaming link, alternative streaming link; `logo` set from the logos map when present, otherwise the text gets `Logo: <logo_url>` or `Logo: none`.

- [ ] **Step 1: Write failing tests** with assertions for: slot order and the exact `Stream:` / `Alt stream:` lines under each DJ; `none` for a DJ with no alt link; guest DJ (id <= 0) shows `Discord: none` in the DM; open state note present and open slot shown as `open`; canceled state has no DJ lines; 40 slots with 200-character links yield more than one message, each under 6000 characters and at most 10 embeds, and every slot appears exactly once; `content_hash` equal for equal input and different after a link change; DM header comes first and a payload carries `logo` bytes when supplied; no U+2014 anywhere in the output.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_management_lineup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the builders and add the user-facing strings to `bot/ui/texts.py` (follow the naming style of existing constants).
- [ ] **Step 4: Run** `uv run pytest tests/ui/test_management_lineup.py tests/test_no_em_dash.py tests/test_texts.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: render DJ management lineup messages`).

---

### Task 3: Service (post, edit, backfill, channel problems)

**Files:**
- Create: `bot/services/management_lineup.py`
- Modify: `bot/context.py` (add `management_lineup_service: "Any | None" = None`)
- Test: `tests/services/test_management_lineup.py`

**Interfaces:**
- Consumes: Task 1 db functions, Task 2 builders, `db_events.list_events`, `is_fully_booked`, `activation.is_active`, `db_config.get_value(pool, guild_id, "management_channel")`, `AdminLogService.log_attention(title, text, todo)`.
- Produces in `bot/services/management_lineup.py`:
  - `MANAGEMENT_CHANNEL_KEY = "management_channel"`
  - `class ManagementLineupService` with `__init__(self, bot, db, log_service)`, plus:
    - `notify_changed(self) -> None`: debounced (10 s, constant `MANAGEMENT_DEBOUNCE_SECONDS`), merges calls like `LineupService.notify_changed`, runs `refresh()`; never raises; no-op while the bot is idle.
    - `async refresh(self, events: list[EventInfo] | None = None) -> None`: under an `asyncio.Lock`, for every event: skip when `management_channel` is unset and no stored record; finished events except Canceled-with-record are skipped; stored record present then edit; no record and fully booked then post (unless suppressed); suppressed record whose event is no longer fully booked is deleted.
    - `async eligible_backfill_events(self, events: list[EventInfo] | None = None) -> list[EventInfo]`: upcoming, not finished, fully booked, no record, sorted by date.
    - `async backfill(self, mode: Literal["all", "new"]) -> int`: `all` posts for every eligible event in date order and returns the count; `new` writes empty-id (suppressed) records for them and returns the count.
  - `def notify_management(ctx) -> None`: calls `ctx.management_lineup_service.notify_changed()` when set; swallows every exception.
- Behavior: edits update only messages whose `content_hash` changed; if an edit raises `disnake.NotFound` the event is re-posted and ids replaced; new messages go to the configured channel, edits go to `record.channel_id`; the view (`ManagementInfoView`, Task 4) is attached to the first message only; channel missing or send forbidden logs one `log_attention` entry per (channel id, reason) per process and returns without raising; DJ data comes from `get_dj_details` for the slot `discord_id`s of the event, read at refresh time.

- [ ] **Step 1: Write failing tests** (fake bot, fake channel with `send`/`fetch_message`, monkeypatched db functions as in `tests/services/test_fully_booked.py`): fully booked event with no record posts once and stores ids; second `refresh` with unchanged data makes no Discord call; changed link edits instead of posting; stored message raising `NotFound` re-posts and stores the new ids; record survives the event opening up and refilling (edited, not re-posted); channel unset posts nothing; forbidden send logs once and does not raise; channel changed after posting: existing record still edited in its stored channel, a new fully booked event goes to the new channel; Canceled event with record is edited once with the canceled state; Done event untouched; `backfill("all")` posts N events in date order; `backfill("new")` stores empty-id records, and a later `refresh` posts nothing for them, then after one opens up the record is deleted and a refill posts; 40-slot event produces several messages with the view on the first only.
- [ ] **Step 2: Run** `uv run pytest tests/services/test_management_lineup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the service and `notify_management`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: DJ management lineup service`).

---

### Task 4: "Send me more information" button

**Files:**
- Modify: `bot/ui/management_lineup.py` (add the view), `bot/ui/texts.py`, `bot/main.py`
- Test: `tests/ui/test_management_info_view.py`

**Interfaces:**
- Consumes: `GatedView`, `refuse_in_maintenance` (`bot/ui/gates.py`), `ctx.is_admin`, `get_record_by_message`, `db_events.get_event`, `get_dj_details`, `db_djs.get_logo_image`, `build_dm_payloads`.
- Produces: `class ManagementInfoView(GatedView)` with `__init__(self, ctx: BotContext)` and one button (label "Send me more information", `custom_id = "mgmt:info"`, style secondary). `main.py` registers one instance with `bot.add_view`, and the service passes the same view to the first message of each lineup.

- Callback order: maintenance refusal; admin check (`ctx.is_admin(inter.author.id)`), non-admin gets an ephemeral refusal text and nothing else; look up the record by `inter.message.id` (missing: ephemeral "This lineup is no longer tracked"); load the event and DJ data now; fetch Discord names via `bot.get_user` or `fetch_user` (failure: `none`); send the header DM then one DM per DJ (logo as `disnake.File` named by `logo_filename`); after sending, an ephemeral confirmation. A `disnake.Forbidden` on the first DM gives an ephemeral "your DMs are closed" note; a failure partway gives an ephemeral note saying how many DJs were sent.

- [ ] **Step 1: Write failing tests:** non-admin click sends the refusal ephemerally and `user.send` is never called (assert no DJ names or links appear in any call); admin click sends header plus one DM per booked slot, with a `file` kwarg for a DJ with logo bytes; closed DMs (`Forbidden`) produce the ephemeral note and no exception; failure on the third DM reports 2 sent; unknown message id replies ephemeral; maintenance gate refuses; admin added at runtime (registry) is accepted.
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_management_info_view.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the view and register it in `bot/main.py` next to the other persistent views (create it before the service, pass it to the service constructor in Task 5).
- [ ] **Step 4: Run** the same command plus `uv run pytest tests/ui/test_gates.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: admin-only more information button for the lineup message`).

---

### Task 5: Wiring and change hooks

**Files:**
- Modify: `bot/main.py` (build `ManagementLineupService(bot, db, log_service)` after `ctx.lineup_service`, assign `ctx.management_lineup_service`, give it the registered `ManagementInfoView`), `bot/cogs/events.py` (after `check_fully_booked` in the poll, `await ctx.management_lineup_service.refresh(events)` when set, inside try/except that logs), plus one `notify_management(ctx)` call at each of: `bot/ui/views.py` (after the two existing `recheck_fully_booked` calls, around lines 1307 and 1532), `bot/sync/intake.py` (after the recheck near line 369 and after DJ updates near line 262), `bot/services/admin_booking.py` (after each `recheck_fully_booked`, lines 196 and 258), `bot/services/event_cancel.py` (next to the lineup notify near line 213), `bot/services/schedule.py` (next to the lineup notify near line 395, covers event edits), `bot/services/dj_admin.py` (after `update_dj_fields`, line 241 and any delete or add DJ path in the file), `bot/services/dj_conversation.py` (after `update_dj_fields`, lines 503 and 507), `bot/web/service.py` if it edits events or DJs through a path not covered above (check with `grep -rn "update_dj_fields\|update_event\|edit_event\|delete_dj" bot`).
- Test: `tests/services/test_management_lineup.py` (add `notify_management` cases), updates to existing tests that assert call sequences if any break.

**Interfaces:**
- Consumes: `notify_management(ctx)` and `ManagementLineupService.refresh` from Task 3.

- [ ] **Step 1: Write failing tests:** `notify_management` with `ctx.management_lineup_service = None` does nothing; with a service whose `notify_changed` raises, it does not raise; for the DJ profile update path in `dj_conversation` and for `DjAdminService` update, a fake ctx service receives one `notify_changed` call (adapt the existing tests in `tests/services/test_dj_conversation.py` and `tests/services/test_dj_admin.py`); the events poll calls `refresh(events)` (adapt `tests/cogs`).
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement** the wiring and the hook calls. Each hook is a single line, placed after the successful change and never inside a database transaction.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS (full suite).
- [ ] **Step 5: Commit** (`feat: refresh the DJ management lineup when data changes`).

---

### Task 6: Setup wizard step and backfill question

**Files:**
- Modify: `bot/ui/setup_wizard.py`, `bot/ui/texts.py`, `bot/cogs/admin.py` (settings listing near line 198: add `DJ Management channel: <#id> or not set`)
- Test: `tests/ui/test_setup_wizard.py` (extend), `tests/cogs` test for the listing if one exists

**Interfaces:**
- Consumes: `ManagementLineupService.eligible_backfill_events`, `backfill`, `db_config.delete_value`, `_channel_check`, `migrate_legacy_draft`.
- Produces: step key `"management_channel"` inserted in `STEPS` directly before `"summary"`; ids `ID_MGMT_CHANNEL = "setup:mgmt_channel"`, `ID_MGMT_CLEAR = "setup:mgmt_clear"`, `ID_MGMT_ALL = "setup:mgmt_all"`, `ID_MGMT_NEW = "setup:mgmt_new"`; draft keys `management_channel` (int, optional), `orig_management_channel` (int or None, set in `prefilled_data`), `management_backfill` (`"all"` or `"new"`, optional).
- Behavior: the step shows a text channel select, a "Clear" button, and says to pick a staff-only channel and that skipping disables the feature; Next is enabled when no channel is chosen or the channel passes `_channel_check`. When a channel is chosen, differs from `orig_management_channel`, and `eligible_backfill_events` is non-empty, the step also shows the two backfill buttons ("Post all existing" with the count, "Only new") and Next stays disabled until one is pressed; choosing another channel or clearing resets `management_backfill`. Add the step's ids to `_layout` (every id of the step, so persistent routing works) and bump `migrate_legacy_draft` with a new marker `steps_v4`: a draft parked at the old summary index moves to the new summary index. `_write_all` stores `management_channel` with `set_value` when set and calls `delete_value` when cleared. After a successful activation (in `activate_from_draft` after `_run_effects`) and after a successful save (`save_from_draft`), call a new helper `_apply_management_backfill(ctx, data)` that runs `service.backfill(mode)` when `management_backfill` is set and the channel is set, logging and swallowing failures. The summary lines list the channel. Backfill choice is never stored as config.

- [ ] **Step 1: Write failing tests:** step order (new key right before `"summary"`, `total_steps` increments); `_layout` contains all four new ids for the step; Next enabled with no channel; Next disabled until a backfill answer when eligible events exist and the channel changed; no backfill buttons when no eligible events; changing the channel resets the answer; a draft at the old summary index migrates to the new one and is not migrated twice; `_write_all` stores and deletes the key; activation with `management_backfill="all"` calls `backfill("all")`, `"new"` calls `backfill("new")`, none calls nothing; a failing backfill does not fail the activation; the summary shows the channel; clearing the channel stops posting (service `refresh` with the key deleted posts nothing for events without a record).
- [ ] **Step 2: Run** `uv run pytest tests/ui/test_setup_wizard.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the step, ids, texts, layout, migration marker, writer and backfill helper.
- [ ] **Step 4: Run** `uv run pytest -q`, `uv run ruff check .`, `uv run pyright`. Expected: all clean.
- [ ] **Step 5: Commit** (`feat: optional DJ management channel in the setup wizard`).

---

### Task 7: Docs and final verification

**Files:**
- Modify: `docs/user-guide.md` (new setup step, what the message shows, the admin-only button), `docs/developer-guide.md` (service, table, hooks, `notify_management`), `CHANGELOG.md` (entry under `[Unreleased]`), `docs/superpowers/specs/2026-11-22-dj-management-lineup-message-design.md` ("Button lookup" call: static custom id `mgmt:info`, event resolved by message id from the table).
- Test: `tests/test_no_em_dash.py` (existing) and the full suite.

- [ ] **Step 1: Update** the four documents.
- [ ] **Step 2: Run** `uv run pytest -q`, `uv run ruff check .`, `uv run pyright`. Expected: all pass with no new warnings.
- [ ] **Step 3: Manual check list** (needs a test guild; report as not run if unavailable): fill an event, see the message with links; swap a DJ, see the edit; click the button as admin (DMs arrive with logos) and as a normal user (refusal, no DM); cancel one booking, see the "no longer fully booked" note; refill, same message returns to normal; restart the bot, the button still works.
- [ ] **Step 4: Commit** (`docs: DJ management lineup message`).

---

## Self-review notes

- Spec coverage: optional setup step and skip (Task 6); trigger and once-per-event (Tasks 3, 5); content and long lineups (Task 2, 3); staying current including DJ profile edits (Task 5); cancellation display (Tasks 2, 3); admin-only button with DMs, logos, closed DMs (Task 4); existing-events question (Tasks 3, 6); channel change and unusable channel (Task 3); privacy split between channel and DMs (Task 2).
- Type consistency: `LineupRecord`, `LineupState`, `DmPayload`, `notify_management`, `management_channel` and `mgmt:info` are used with the same names in every task.
- Existing admin log "fully booked" entry is untouched.
