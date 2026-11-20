# PostgreSQL as Source of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot reads and writes only PostgreSQL; Notion becomes an optional, asynchronous live mirror that also feeds checked hand edits back, with backup, restore and sync status on the admin page.

**Architecture:** New `events` table plus the existing `slot_locks` (the slots table) and `djs` hold all booking data. A Postgres-built `list_events()` returns the same `EventInfo` model, so lineup, outreach, reminders and the booking UI keep their interfaces. Every write enqueues a row in `notion_outbox`; a background worker mirrors it to Notion. A poll compares Postgres, Notion and the last agreed state (`notion_mirror`) three ways and applies, pushes, warns or opens a per-entity conflict.

**Tech Stack:** Python 3 (uv), asyncpg, Alembic, disnake, aiohttp admin page, Notion API 2026-03-11 via `bot/notion/api.py`, pytest with `tests/notion/fake_notion.py` and opt-in real-Postgres tests (`-m pg`).

**Spec:** `docs/superpowers/specs/2026-11-20-postgres-source-of-truth-design.md`

## Global Constraints

- No Discord handler awaits a Notion call; the Notion poll and the outbox worker never hold a lock or connection a Discord handler needs (spec decision 9).
- Notion is optional: without `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID` the bot runs on Postgres alone, with no outbox, no poll, no Notion calls and no startup failure (decision 10).
- Admin accounts (`web_admins`) never go to Notion; the export file contains everything (decisions 2, 7).
- Conflict: per-entity stall; after 24 h without a decision Postgres wins and the bot logs it (decision 5).
- "Bot data" in Notion is read-only; hand edits there are overwritten (decision 6).
- Upgrade: safety export, then one automatic import from Notion on first start; Notion sync off until done (decision 8).
- The two-DJs-one-slot guarantee (`acquire_slot_lock`, `SELECT ... FOR UPDATE`) stays.
- Admin-log text has no em dash (`tests/test_no_em_dash.py`); user, DJ and Notion text is escaped as today (`_safe_dj_name`, `bot/log.py`).
- Commits: author and committer `M4XXX <44500247+M4XXX@users.noreply.github.com>`, no AI attribution lines. Work on branch `feat/postgres-source-of-truth`, one PR at the end.
- Test commands: `uv run pytest -q` (whole suite), `RAVEBOT_TEST_POSTGRES_URL=postgresql://... uv run pytest -m pg -q tests/pg` (podman PostgreSQL).

## Plan-level calls (not in the spec, decided here)

- **Event key reuse.** `events.uid` is TEXT. Events imported from Notion keep their Notion page id as `uid`; new events get `str(uuid.uuid4())`. All tables already keyed by the Notion page id (`slot_locks.event_id`, `fully_booked_events`, `manual_events`, `outreach_*`, `reminders_sent`) therefore need no remapping, and old Discord buttons whose `custom_id` carries `event.page_id` keep working. `EventInfo.page_id` keeps its name and now means "bot event key" (docstring updated).
- **Slots table.** `slot_locks` is the slots table (no rename). Rows `0..slot_count-1` always exist per event (`init_event_slots`).
- **SlotInfo from Postgres.** `page_id = f"{uid}:{number}"`, `dj_page_id = str(discord_user_id)` (or None), `dj_name = djs.dj_name`, `discord_id = discord_user_id`, `extra_dj_page_ids = ()`.
- **Notion page ids live in one place:** `notion_mirror.notion_page_id`. `djs.notion_dj_page_id` is copied there by migration 0017 and dropped in 0018.
- **Outbox enqueue happens after the Postgres commit**, not in the same transaction. A crash in between is repaired by the poll (Postgres changed, Notion unchanged → push).
- **Logos** go Postgres → Notion only. A logo changed by hand in Notion is ignored; a rebuild downloads the Notion file to restore `logo_image`.
- **Clearing or replacing a DJ in a Notion slot cell** is a changed value (applied, DJs DMed as the old "adopt"/"free" did). Deleting a Slots, Events or DJs page is a deletion (warning).
- **One log channel.** The bot has only `log_channel`; "info channel" entries use `log_changed_in_notion`/a new `log_notion_applied`, warnings and conflicts use `log_attention`.
- **Backup file:** gzip-compressed JSON, extension `.ravebot.json.gz`, see Task 6.
- `event_shapes` is replaced by the intake validation (Task 12) and dropped in 0018. The old `NotionSyncService`/`notion_sync_plan.py` are deleted in Task 10.

## Review Focus

1. Notion answers with an empty or partial result (lost share, API hiccup) → nothing is deleted or freed in Postgres; one warning. Test in Task 11 (`test_mass_deletion_guard`).
2. Notion unreachable while a DJ books, cancels or edits the profile → the DJ gets the normal answer at once; the change reaches Notion later. Test in Task 10 (`test_booking_replies_while_notion_down`).
3. A hand edit in Notion on an entity with an unsent Postgres change → conflict for that entity only; other entities keep syncing. Test in Task 13 (`test_conflict_stalls_only_its_entity`).
4. A corrupt backup file or one from a newer migration revision → refused, database untouched. Test in Task 15 (`test_import_refuses_newer_revision`, `test_import_corrupt_file_leaves_db`).
5. Upgrade import over a Notion with duplicate event titles or slot rows without an event → rolled back, bot in maintenance with a clear reason, retried next start. Test in Task 7 (`test_upgrade_import_rolls_back_on_duplicate_titles`).

---

## Phase 1: Postgres core (Notion write-only mirror)

### Task 1: Optional Notion configuration

**Files:**
- Modify: `bot/config.py`
- Modify: `bot/main.py` (Notion setup block, `notion_api.close()`)
- Modify: `.env.example`, `unraid/ravebot.xml` (mark both Notion vars optional)
- Test: `tests/test_config.py`, `tests/test_main.py`

**Interfaces:**
- Produces: `Config.notion_api_key: str` and `Config.notion_parent_page_id: str` may be `""`; property `Config.notion_enabled -> bool` (both set). In `main()`, when disabled: no `NotionApi`, `ctx.notion_client is None`, `ctx.notion_recover is None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_notion_vars_optional(monkeypatch):  # both unset -> loads, notion_enabled False
def test_notion_vars_both_set_enables(monkeypatch):  # notion_enabled True
def test_only_one_notion_var_exits(monkeypatch):  # SystemExit naming the missing one
async def test_main_without_notion_skips_setup(...):  # ensure_schema never called, ctx.notion_client is None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_main.py -q` — expected: the four new tests fail.
- [ ] **Step 3: Implement.** Move the two Notion vars out of `require()`; exit with `"Set both NOTION_API_KEY and NOTION_PARENT_PAGE_ID, or neither"` when exactly one is set. Guard the Notion block and the shutdown close in `main()` with `config.notion_enabled`.
- [ ] **Step 4: Run** the same command — expected: PASS; then `uv run pytest -q` — expected: no new failures.
- [ ] **Step 5: Commit** `feat: Notion settings are optional`

### Task 2: Migration 0017 (events, mirror, outbox, issues)

**Files:**
- Create: `migrations/versions/0017_postgres_source_of_truth.py`
- Modify: `tests/pg/conftest.py` (`TABLES` gets the four new tables)
- Test: `tests/test_migration_0017.py` (pattern of `tests/test_migration_0015.py`), `tests/pg/test_postgres.py`

**Interfaces:**
- Produces tables:
  - `events(uid TEXT PK, event_id TEXT NOT NULL UNIQUE, date DATE NOT NULL, start TEXT NOT NULL, slot_minutes INT NOT NULL, slot_count INT NOT NULL CHECK (slot_count BETWEEN 1 AND 100), status TEXT NULL, genre TEXT NULL, notes TEXT NULL, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())`
  - `notion_mirror(entity TEXT, entity_key TEXT, notion_page_id TEXT NULL, synced_hash TEXT NULL, synced_payload JSONB NULL, synced_at TIMESTAMPTZ NULL, PRIMARY KEY (entity, entity_key))`; `entity` in `'event','slot','dj','botdata'`.
  - `notion_outbox(entity TEXT, entity_key TEXT, op TEXT NOT NULL DEFAULT 'upsert', notion_page_id TEXT NULL, enqueued_at TIMESTAMPTZ DEFAULT now(), attempts INT DEFAULT 0, next_attempt_at TIMESTAMPTZ DEFAULT now(), last_error TEXT NULL, PRIMARY KEY (entity, entity_key))`; `op` in `'upsert','delete'`.
  - `sync_issues(id BIGSERIAL PK, kind TEXT NOT NULL, entity TEXT NULL, entity_key TEXT NULL, summary TEXT NOT NULL, pg_payload JSONB NULL, notion_payload JSONB NULL, created_at TIMESTAMPTZ DEFAULT now(), resolved_at TIMESTAMPTZ NULL, resolution TEXT NULL)`; `kind` in `'conflict','warning'`; partial unique index on `(entity, entity_key) WHERE resolved_at IS NULL AND kind = 'conflict'`.
- Data step: `INSERT INTO notion_mirror (entity, entity_key, notion_page_id) SELECT 'dj', discord_user_id::text, notion_dj_page_id FROM djs WHERE notion_dj_page_id IS NOT NULL`.
- Downgrade drops the four tables.

- [ ] **Step 1: Write the failing tests:** `test_0017_creates_tables_and_copies_dj_page_ids` (mock-op style as 0015) and pg `test_events_slot_count_check` (101 rejected), `test_one_open_conflict_per_entity` (second open conflict raises `UniqueViolationError`).
- [ ] **Step 2: Run** `uv run pytest tests/test_migration_0017.py -q` and the pg tests — expected: FAIL.
- [ ] **Step 3: Write the migration.**
- [ ] **Step 4: Run** both — expected: PASS; `uv run pytest tests/test_migration_chain.py -q` PASS.
- [ ] **Step 5: Commit** `feat: tables for events, the Notion mirror, outbox and sync issues`

### Task 3: Events repository (Postgres read model)

**Files:**
- Create: `bot/db/events.py`
- Modify: `bot/notion/models.py` (docstring of `EventInfo.page_id`)
- Test: `tests/db/test_events.py` (mock pool SQL), `tests/pg/test_events_repo.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class EventDraft: event_id: str; date: datetime.date; start: datetime.time; slot_minutes: int; slot_count: int; status: str | None = None; genre: str | None = None; notes: str | None = None`
  - `async def list_events(pool, *, include_past: bool = False) -> list[EventInfo]` — same filter and sort as `NotionClient.list_events` (past = `date < local_today()`), slots joined from `slot_locks` + `djs` per the plan-level SlotInfo call.
  - `async def get_event(pool, uid: str) -> EventInfo | None`
  - `async def insert_event(pool, draft: EventDraft, uid: str | None = None) -> EventInfo` — one transaction: insert row, `init_event_slots`; raises `DuplicateEventError(event_id)` (reuse `bot/notion/client.py` class, move it to `bot/db/events.py` and re-export) on the unique violation.
  - `async def update_event(pool, uid: str, **fields) -> bool` — allowed fields: the `EventDraft` names; sets `updated_at`.
  - `async def set_event_status(pool, uid: str, status: str | None) -> None`
  - `async def event_id_exists(pool, event_id: str) -> bool`

- [ ] **Step 1: Write failing tests:** `test_list_events_matches_notion_shape` (EventInfo fields, slots 1..n, booked slot carries `discord_id`, `dj_name`, `dj_page_id == str(id)`), `test_list_events_hides_past_unless_asked`, `test_insert_event_creates_slots`, `test_insert_duplicate_event_id_raises`, `test_update_event_rejects_unknown_field` (ValueError).
- [ ] **Step 2: Run** `uv run pytest tests/db/test_events.py -q` + pg — expected: FAIL.
- [ ] **Step 3: Implement** with one query for events and one for their slots (no N+1).
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: events repository in Postgres`

### Task 4: Canonical payloads and Notion mirror writes

**Files:**
- Create: `bot/sync/__init__.py`, `bot/sync/payloads.py`
- Modify: `bot/notion/client.py` (new mirror methods), `bot/notion/schema.py` (`"Bot ID": {"rich_text": {}}` in `_events_props`), `bot/notion/models.py` (`PROP_EVENT_BOT_ID = "Bot ID"`)
- Test: `tests/sync/test_payloads.py`, `tests/notion/test_client_mirror.py`

**Interfaces:**
- Produces (pure, `bot/sync/payloads.py`):
  - `event_payload(row: Mapping) -> dict` keys `event_id, date (iso), start ("HH:MM"), slot_minutes, slot_count, status, genre, notes`
  - `slot_payload(event_uid: str, slot_index: int, discord_user_id: int | None) -> dict` keys `event_uid, number, discord_id`
  - `dj_payload(row: Mapping) -> dict` keys `discord_id, dj_name, vrchat_name, logo_url, link, alt_link` (no logo bytes)
  - `notion_event_payload(page: dict) -> dict | None`, `notion_slot_payload(page: dict, event_uid_by_page: dict[str, str], discord_id_by_dj_page: dict[str, int | None]) -> dict | None`, `notion_dj_payload(page: dict) -> dict | None` — same keys, built with the existing parsers in `bot/notion/models.py`; None when the page cannot be read (missing title, bad date).
  - `payload_hash(payload: dict) -> str` — sha256 of `json.dumps(payload, sort_keys=True, separators=(",", ":"))`.
- Produces (`NotionClient`):
  - `async def mirror_event(self, uid: str, payload: dict, page_id: str | None) -> str`
  - `async def mirror_slot(self, payload: dict, event_page_id: str, dj_page_id: str | None, page_id: str | None) -> str`
  - `async def mirror_dj(self, payload: dict, page_id: str | None, *, logo_image: bytes | None, upload_logo: bool) -> str` (reuse `_write_dj`/upload code of `_upsert_dj`)
  - `async def trash_page(self, page_id: str) -> None` (404 tolerated)
  - `async def read_all(self) -> NotionSnapshot` with `@dataclass NotionSnapshot: events: list[dict]; slots: list[dict]; djs: list[dict]; botdata: list[dict]` (raw non-trashed pages; `botdata` empty until Task 14)
  - Each `mirror_*` creates the page when `page_id` is None or the page is gone (404/trashed) and returns the page id.

- [ ] **Step 1: Write failing tests:** `test_payload_round_trip_event` (`notion_event_payload(page written by mirror_event) == event_payload(row)`), same for slot and dj, `test_hash_ignores_key_order`, `test_mirror_event_recreates_trashed_page`, `test_mirror_event_writes_bot_id`.
- [ ] **Step 2: Run** `uv run pytest tests/sync tests/notion/test_client_mirror.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: canonical sync payloads and Notion mirror writes`

### Task 5: Outbox and mirror worker

**Files:**
- Create: `bot/db/outbox.py`, `bot/db/mirror.py`, `bot/sync/worker.py`
- Modify: `bot/context.py` (field `mirror: Any | None = None`; `attach_notion` hands the client to it), `bot/main.py` (start/stop)
- Test: `tests/db/test_outbox.py`, `tests/sync/test_worker.py`, `tests/pg/test_outbox.py`

**Interfaces:**
- Produces (`bot/db/outbox.py`): `async def enqueue(pool, entity: str, key: str, op: str = "upsert", notion_page_id: str | None = None) -> None` (upsert on PK: resets `attempts`, `next_attempt_at = now()`, `last_error = NULL`; a later `upsert` replaces a pending `delete` and vice versa); `async def due(pool, limit: int) -> list[OutboxRow]`, `async def done(pool, entity, key, enqueued_at) -> None` (deletes only if `enqueued_at` unchanged), `async def failed(pool, entity, key, error: str) -> None` (backoff `min(2**attempts * 10 s, 1 h)`), `async def stats(pool) -> OutboxStats(count: int, oldest: datetime | None, last_error: str | None)`.
- Produces (`bot/db/mirror.py`): `get(pool, entity, key) -> MirrorRow | None`, `get_all(pool, entity) -> dict[str, MirrorRow]`, `set_synced(pool, entity, key, page_id, payload) -> None` (stores hash + payload + now), `forget(pool, entity, key) -> None`, `open_issue_keys(pool) -> set[tuple[str, str]]`.
- Produces (`bot/sync/worker.py`): `class MirrorWorker` with `__init__(self, db, notion_client, log_service)`, `start() -> None`, `async stop() -> None`, `notify() -> None` (wakes the loop, never blocks), `async def drain_once(self) -> int`. Convenience used by every write path: `def changed(ctx, entity: str, key: str, op: str = "upsert", notion_page_id: str | None = None) -> None` — schedules `enqueue` + `notify` as a background task (`asyncio.create_task`, task kept in a set, exceptions logged) and returns at once; no-op when `ctx.mirror is None` (Notion disabled).
- Order inside `drain_once`: events before slots and djs before slots (slot pages need both page ids); skips keys in `open_issue_keys`; at most 3 Notion requests per second; each success calls `mirror.set_synced` with the payload it wrote.

- [ ] **Step 1: Write failing tests:** `test_enqueue_coalesces`, `test_done_keeps_newer_enqueue`, `test_failed_backs_off`, `test_drain_writes_event_then_slots`, `test_drain_skips_open_conflict_key`, `test_changed_returns_without_awaiting_db` (enqueue mocked with an `asyncio.Event` that is never set; `changed()` returns), `test_changed_is_noop_without_notion`.
- [ ] **Step 2: Run** `uv run pytest tests/db/test_outbox.py tests/sync/test_worker.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.** Loop: `await asyncio.wait_for(self._wake.wait(), 10)`, `drain_once()`, never raises out of the loop.
- [ ] **Step 4: Run** — expected: PASS (+ pg).
- [ ] **Step 5: Commit** `feat: Notion outbox and background mirror worker`

### Task 6: Backup file export

**Files:**
- Create: `bot/backup/__init__.py`, `bot/backup/export.py`
- Test: `tests/backup/test_export.py`, `tests/pg/test_backup.py`

**Interfaces:**
- Produces: `async def export_database(pool) -> bytes` — gzip of JSON `{"format": "ravebot-backup", "version": 1, "alembic_revision": str, "created_at": iso, "tables": {name: {"columns": [..], "rows": [[..], ..]}}, "sequences": {name: last_value}}` covering every table in schema `public` except `alembic_version`. Encoding: `bytea` base64 string, timestamps/dates ISO, `jsonb` as JSON, other types as JSON scalars.
- Produces: `async def write_safety_export(pool, reason: str) -> pathlib.Path` — writes into `BACKUP_DIR` env (default `/data/backups`, created if missing; falls back to `./backups` when not writable), file `ravebot-<UTC yyyymmdd-HHMMSS>-<reason>.ravebot.json.gz`, keeps the newest 10 safety files.

- [ ] **Step 1: Write failing tests:** `test_export_contains_every_table_but_alembic` (pg: compare with `information_schema.tables`), `test_export_encodes_bytea_and_jsonb`, `test_safety_export_rotates_to_ten`.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement** (read each table with `SELECT *` inside one `REPEATABLE READ` transaction).
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: full database export file`

### Task 7: Upgrade import from Notion and maintenance gate

**Files:**
- Create: `bot/sync/notion_import.py`
- Modify: `bot/context.py` (`maintenance: str | None = None`, `sync_problem: str | None = None`), `bot/ui/gates.py` (`async def refuse_in_maintenance(inter, ctx) -> bool`), `bot/ui/texts.py` (`MAINTENANCE = "The bot is restoring its data. Please try again in a few minutes."`, upgrade log texts), `bot/main.py` (run after Notion setup, before services start the poll), the entry points of booking, cancel, profile edit, `/create event`, `/cancel event`, admin book/cancel and the admin-page write routes
- Test: `tests/sync/test_notion_import.py`, `tests/ui/test_gates.py`, `tests/pg/test_notion_import.py`

**Interfaces:**
- Consumes: `NotionClient.read_all()`, payload parsers (Task 4), `insert_event` semantics (Task 3), `write_safety_export` (Task 6), `mirror.set_synced` (Task 5).
- Produces: `async def import_from_notion(pool, notion_client, *, mode: Literal["upgrade", "replace"]) -> ImportResult` with `@dataclass ImportResult: ok: bool; events: int; slots: int; djs: int; problems: list[str]`. One transaction. `upgrade`: inserts events (`uid` = Notion page id), sets `slot_locks` holders from Notion, inserts DJs missing in `djs` (guests from negative Discord IDs; a DJ page without a Discord ID gets `next_guest_id`), writes `notion_mirror` rows for every page with the Notion payload as synced state; existing `djs` rows win over Notion for profile fields. `replace` (used by Task 16): truncates `events`, `slot_locks`, `djs`, `notion_mirror`, `notion_outbox`, `sync_issues` first, then the same.
- Validation → `problems` and rollback: duplicate event titles; slot row without an event or with a number outside `1..slot_count`; two DJ pages with the same Discord ID; unreadable required properties.
- Produces: `async def run_upgrade_import_if_needed(ctx) -> None` — config key `pg_truth_imported` (guild 0). Notion disabled or key `"1"` → nothing. Else `write_safety_export(pool, "upgrade")`, `ctx.maintenance = texts.MAINTENANCE`, import; ok → key `"1"`, `ctx.maintenance = None`, admin log summary; failed → stays in maintenance, `ctx.sync_problem = "; ".join(problems[:10])`, admin log with the problems; retried on next start (and by the admin page, Task 17).
- Produces: `refuse_in_maintenance` sends `texts.MAINTENANCE` ephemerally and returns True while `ctx.maintenance` is set.

- [ ] **Step 1: Write failing tests:** `test_upgrade_import_copies_events_slots_djs`, `test_upgrade_import_keeps_existing_dj_profile`, `test_upgrade_import_rolls_back_on_duplicate_titles` (Review Focus 5: no rows written, `ok False`, problem names the title), `test_upgrade_import_rolls_back_on_orphan_slot`, `test_upgrade_runs_once` (key set → no Notion read), `test_upgrade_writes_safety_export_first`, `test_booking_refused_in_maintenance`.
- [ ] **Step 2: Run** `uv run pytest tests/sync/test_notion_import.py tests/ui/test_gates.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS (+ pg).
- [ ] **Step 5: Commit** `feat: one-time import from Notion on upgrade, maintenance gate`

### Task 8: Read path from Postgres

**Files:**
- Modify every `notion_client.list_events()` call site: `bot/cogs/admin.py:387,452`, `bot/cogs/events.py:127`, `bot/ui/setup_wizard.py:287,1028`, `bot/ui/views.py:343,531,652,726,1190,1271,1856`, `bot/services/event_cancel.py:95`, `bot/services/admin_booking.py:137,257`, `bot/services/lineup.py:129,156`, `bot/services/fully_booked.py:69`, `bot/services/dj_admin.py:128`, `bot/services/reminders.py:103`, `bot/services/event_replenish.py:91`, `bot/services/outreach.py:127`
- Test: existing tests of those modules (update their fakes), new `tests/test_no_notion_reads.py`

**Interfaces:**
- Consumes: `bot.db.events.list_events(pool, include_past=...)`.
- Produces: no runtime caller of `NotionClient.list_events` outside `bot/sync/` and `bot/notion/`. `setup_wizard.py:287` (a Notion connectivity check) becomes a `ctx.db` check; the Notion check moves to Task 17's status.

- [ ] **Step 1: Write the failing test** `test_no_list_events_outside_sync` — greps `bot/` (AST or text) for `.list_events(` on a Notion client outside `bot/sync/`, `bot/notion/`; expects none.
- [ ] **Step 2: Run** `uv run pytest tests/test_no_notion_reads.py -q` — expected: FAIL listing the call sites.
- [ ] **Step 3: Replace the call sites;** remove the Notion re-read at the top of `BookingConfirmView._handle_confirm` (now a Postgres `get_event`). Update the affected tests to seed events through a fake `list_events`.
- [ ] **Step 4: Run** `uv run pytest -q` — expected: PASS.
- [ ] **Step 5: Commit** `refactor: all event reads come from Postgres`

### Task 9: Notion-free write paths for events and DJs

**Files:**
- Modify: `bot/cogs/admin.py:309` (`/create event`), `bot/services/event_replenish.py:142-151`, `bot/services/event_cancel.py:194,231`, `bot/services/dj_sync.py` (replace body), `bot/services/dj_admin.py:217,290,353`, `bot/services/dj_conversation.py:478,507`, `bot/main.py` (`_retry_logos_if_active` removed)
- Delete: `bot/services/dj_pages.py` and its test
- Test: the existing tests of those modules

**Interfaces:**
- Consumes: `insert_event`, `set_event_status`, `event_id_exists`, `update_event` (Task 3); `worker.changed(ctx, ...)` (Task 5).
- Produces: `sync_dj(ctx, discord_user_id, discord_name) -> bool` keeps its signature (callers unchanged) and only calls `changed(ctx, "dj", str(id))`, returning True. Deleting a DJ: `changed(ctx, "dj", key, op="delete", notion_page_id=<mirror page id>)`. Event create: `changed` for the event and each slot. Event cancel: status + emptied slots in Postgres, then `changed` for event and slots.

- [ ] **Step 1: Update tests first:** each flow asserts the Postgres write and the `changed(...)` calls, and asserts the Notion client mock got no call (`notion.assert_not_called()` / `method_calls == []`).
- [ ] **Step 2: Run** the module tests — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` — expected: PASS.
- [ ] **Step 5: Commit** `feat: events and DJs are written to Postgres, Notion follows in the background`

### Task 10: Notion-free booking and cancel; retire the old sync

**Files:**
- Modify: `bot/ui/views.py` (`_handle_confirm` ~1225-1420, `_cancel_booking` ~1628), `bot/services/admin_booking.py:174,291`, `bot/cogs/events.py` (poll: drop `notion_sync_service.run_cycle`), `bot/context.py`, `bot/main.py`
- Delete: `bot/services/notion_sync.py`, `bot/services/notion_sync_plan.py`, `bot/db/event_shapes.py` and their tests
- Test: `tests/ui/test_booking_*` / existing view tests, `tests/services/test_admin_booking.py`, new `tests/ui/test_booking_notion_down.py`

**Interfaces:**
- Consumes: `acquire_slot_lock`, `release_slot_lock` (unchanged), `changed(ctx, "slot", f"{uid}:{slot_index}")`.
- Produces: booking confirm = stale check → `get_event` (status) → `acquire_slot_lock` → `changed` → `BookedEmbed` → lineup notify → log. No `ensure_dj_page`, no `assign_slot`, no `_release_pg_lock` after a Notion failure. Cancel and admin override likewise (`force` = conditional UPDATE of the holder in Postgres; add `async def force_slot_holder(pool, event_id, slot_index, discord_user_id: int | None) -> int | None` to `bot/db/bookings.py`, returns the previous holder).

- [ ] **Step 1: Write failing tests:** `test_booking_replies_while_notion_down` (Review Focus 2: Notion client whose every call hangs on an un-set `asyncio.Event`; confirm sends `BookedEmbed` and returns within 1 s, outbox row exists), `test_cancel_never_calls_notion`, `test_admin_force_returns_previous_holder` (pg), keep the existing concurrent-booking test green.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement and delete the retired modules.**
- [ ] **Step 4: Run** `uv run pytest -q` and pg — expected: PASS.
- [ ] **Step 5: Commit** `feat: bookings never wait for Notion; old Notion-wins sync removed`

## Phase 2: Hand edits from Notion

### Task 11: Three-way planner (pure)

**Files:**
- Create: `bot/sync/plan.py`
- Test: `tests/sync/test_plan.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Side: payloads: dict[str, dict]; page_ids: dict[str, str]` and
  `def plan(entity: str, pg: dict[str, dict], notion: Side, mirror: dict[str, MirrorRow], open_keys: set[str], *, unmatched_notion: list[dict]) -> list[Change]`
  with `@dataclass(frozen=True) class Change: kind: Literal["push", "apply", "conflict", "new_in_notion", "deleted_in_notion", "key_changed", "mass_change", "agree"]; entity: str; key: str | None; pg: dict | None; notion: dict | None; page_id: str | None`.
- Rules (h = `payload_hash`, m = mirror synced hash):
  - key in open_keys → nothing.
  - pg ≠ m, notion = m → `push`. pg = m, notion ≠ m → `apply`. both ≠ m and pg ≠ notion → `conflict`. both ≠ m and pg = notion → `agree` (store as synced).
  - mirror has a page id that Notion no longer has → `deleted_in_notion`.
  - Notion page matched by page id whose key field (event `event_id`, dj `discord_id`, slot `event_uid`/`number`) differs from the mirror payload → `key_changed`.
  - Notion page with no mirror row → `new_in_notion` (unless its key duplicates an existing key → `key_changed`).
  - Postgres key with no mirror row → `push`.
  - Guard: Notion read returned zero pages of this entity while pg has ≥ 1, or `deleted_in_notion` count > `max(3, 20 % of mirror rows)` → a single `mass_change` and nothing else for this entity.

- [ ] **Step 1: Write failing tests:** one per rule, plus `test_mass_deletion_guard` (Review Focus 1) and `test_open_key_is_skipped`.
- [ ] **Step 2: Run** `uv run pytest tests/sync/test_plan.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: three-way Notion sync planner`

### Task 12: Intake: validate and apply hand edits

**Files:**
- Create: `bot/sync/intake.py`
- Modify: `bot/cogs/admin.py` (extract the `/create event` checks into `bot/services/event_defaults.py: def validate_event_draft(draft: EventDraft, *, today: date) -> list[str]`), `bot/services/dj_validation.py` (reuse), `bot/log.py` (`async def log_notion_applied(self, what: str) -> None`)
- Test: `tests/sync/test_intake.py`

**Interfaces:**
- Consumes: `Change` (Task 11), repositories (Task 3), `force_slot_holder` (Task 10), `validate_event_draft`, `dj_validation`.
- Produces: `class Intake` with `__init__(self, ctx, bot)` and `async def apply(self, change: Change) -> IntakeOutcome` where `IntakeOutcome = Literal["applied", "rejected"]`; on `rejected` it returns the reasons via `self.last_reasons: list[str]` for the caller to raise a warning.
- Behaviour:
  - event `apply`/`new_in_notion`: `validate_event_draft`; reject a `slot_count` below a booked slot and a `start`/`slot_minutes` change while any slot is booked (the rule `event_shapes` enforced). New event gets a new uid; `notion_mirror` row re-keyed from the page id; `changed(ctx, "event", uid)` so the `Bot ID` is written back.
  - dj `apply`/`new_in_notion`: same checks as the admin-page DJ form (unique normalised name, URL rules); a page without Discord ID becomes a guest (`next_guest_id`).
  - slot `apply`: DJ set on a free slot → `acquire_slot_lock`, DM `texts.dm_added`, lineup notify, `check_fully_booked`, `log_booked(..., "Notion")`. DJ cleared → `release_slot_lock`, DM `texts.dm_removed`, `clear_announced`, `log_changed_in_notion`. DJ replaced → both. DJ not in `djs` or slot held by someone else in Postgres → rejected.
  - Every applied change → `log_notion_applied` (one entry per cycle, up to 10 lines + "and N more", as in Story 10.4).

- [ ] **Step 1: Write failing tests:** `test_apply_event_field_change`, `test_reject_slot_count_below_booking`, `test_new_event_gets_uid_and_bot_id_pushed`, `test_new_dj_duplicate_name_rejected`, `test_new_guest_dj`, `test_slot_set_books_and_dms`, `test_slot_cleared_frees_and_dms`, `test_slot_set_unknown_dj_rejected`.
- [ ] **Step 2: Run** `uv run pytest tests/sync/test_intake.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: checked hand edits from Notion are applied to Postgres`

### Task 13: Sync cycle, conflicts, warnings, 24 h rule

**Files:**
- Create: `bot/sync/cycle.py`, `bot/db/issues.py`
- Modify: `bot/cogs/events.py` (poll calls the cycle when `ctx.sync_cycle` is set and `ctx.maintenance` is None), `bot/main.py`, `bot/context.py` (`sync_cycle: Any | None = None`), `bot/ui/texts.py` (conflict and warning texts)
- Test: `tests/sync/test_cycle.py`, `tests/pg/test_issues.py`

**Interfaces:**
- Produces (`bot/db/issues.py`): `open_conflict(pool, entity, key, summary, pg_payload, notion_payload) -> int | None` (None when one is already open), `add_warning(pool, summary, entity=None, key=None, notion_payload=None) -> int`, `list_open(pool) -> list[IssueRow]`, `resolve(pool, issue_id, resolution: Literal["postgres", "notion", "resynced", "expired"]) -> IssueRow | None`, `expired_conflicts(pool, older_than: timedelta) -> list[IssueRow]`.
- Produces (`bot/sync/cycle.py`): `class SyncCycle` with `__init__(self, ctx, bot)`, `async def run(self) -> CycleReport`, `async def resolve_issue(self, issue_id: int, choice: str) -> ActionResult`, `async def resync_all(self) -> int` (enqueues every Postgres entity, returns the count). `CycleReport(applied: int, pushed: int, conflicts: int, warnings: int)`. Stores `sync_last_run`, `sync_last_error` under config guild 0.
- Order in `run`: expire conflicts older than 24 h (`resolve(..., "expired")` → `changed` for that key, admin log "Postgres kept after 24 h"), `read_all()`, plan per entity (events, djs, then slots), execute: `push` → `changed`; `apply`/`new_in_notion` → `Intake.apply` (rejected → `add_warning`); `conflict` → `open_conflict` + admin log with both sides and "choose on the admin page"; `deleted_in_notion`/`key_changed`/`mass_change` → `add_warning` + admin log; `agree` → `set_synced`.
- `resolve_issue`: `postgres` → `changed`; `notion` → `Intake.apply` with the stored Notion payload (rejected → stays open with the reasons); `resynced` (warnings) → `changed` for the entity, or `resync_all` when the warning has no entity.
- The cycle runs in the existing poll task; it uses its own short-lived connections and never holds a row lock across a Notion call.

- [ ] **Step 1: Write failing tests:** `test_conflict_stalls_only_its_entity` (Review Focus 3: two events, conflict on one; the other's outbox row drains and its Notion edit applies), `test_conflict_expires_after_24h_postgres_wins`, `test_resolve_keep_notion_applies`, `test_deleted_page_warns_and_keeps_pg`, `test_failed_notion_read_changes_nothing`, `test_cycle_skipped_in_maintenance`.
- [ ] **Step 2: Run** `uv run pytest tests/sync/test_cycle.py -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` and pg — expected: PASS.
- [ ] **Step 5: Commit** `feat: Notion sync cycle with per-entity conflicts and warnings`

### Task 14: "Bot data" mirror (read-only)

**Files:**
- Create: `bot/sync/botdata.py`
- Modify: `bot/notion/schema.py` (fourth database `Bot data`: `Key` title, `Table` select, `Payload` rich_text chunked at 2000 chars, `Updated` date, `Note` rich_text; description "Read-only backup written by the bot. Edits here are overwritten."), `bot/notion/views.py` if views are created per database, `bot/notion/client.py` (`mirror_botdata(key, table, payload, page_id) -> str`), `bot/sync/cycle.py` (push step), `bot/sync/notion_import.py` (restore step)
- Test: `tests/sync/test_botdata.py`

**Interfaces:**
- Produces: `BOTDATA_TABLES = ("config", "dj_conversations", "outreach_sequences", "outreach_contacts", "reminders_sent", "fully_booked_events", "manual_events", "setup_drafts")`; excluded: `web_admins`, `log_replies`, `config` rows with `guild_id = 0`, `dj_conversations.logo_image`. `async def botdata_rows(pool) -> dict[str, tuple[str, dict]]` (key `f"{table}:{pk}"` → (table, JSON-safe row)), `async def restore_botdata(conn, pages: list[dict]) -> int`.
- In the cycle: Postgres rows whose hash differs from the mirror (or whose Notion payload differs, i.e. a hand edit) → `changed(ctx, "botdata", key)`; Notion rows with no Postgres row → trashed. Never `apply`, never a conflict.

- [ ] **Step 1: Write failing tests:** `test_botdata_excludes_admins_and_notion_ids`, `test_hand_edit_in_botdata_is_overwritten`, `test_payload_over_2000_chars_round_trips`, `test_rebuild_restores_config_rows`.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: read-only Bot data mirror in Notion`

## Phase 3: Admin page backup and status

### Task 15: Import from a backup file

**Files:**
- Create: `bot/backup/restore.py`
- Test: `tests/backup/test_restore.py`, `tests/pg/test_backup.py`

**Interfaces:**
- Consumes: the format of Task 6, `write_safety_export`, `ctx.maintenance`, `SyncCycle.resync_all`.
- Produces: `async def import_database(ctx, data: bytes) -> ActionResult` — refuses (no write) on bad gzip/JSON, wrong `format`, `version` ≠ 1, or `alembic_revision` not equal to the running head (newer or older: message names both revisions); otherwise safety export, maintenance on, one transaction: `TRUNCATE` all exported tables `RESTART IDENTITY CASCADE`, insert rows, `setval` sequences; maintenance off; `ctx.admins.load()`; `resync_all()` when Notion is enabled.

- [ ] **Step 1: Write failing tests:** `test_import_round_trip` (pg: export → change data → import → equal), `test_import_refuses_newer_revision`, `test_import_corrupt_file_leaves_db`, `test_import_reloads_admins_and_resyncs`.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: restore the database from a backup file`

### Task 16: Rebuild Postgres from Notion

**Files:**
- Create: `bot/sync/rebuild.py`
- Modify: `bot/main.py` (startup case "Postgres empty, Notion filled": no `djs` and no `events` rows and Notion has events → automatic `replace` import after a safety export; "Postgres filled, Notion empty or missing": existing `ensure_schema` path plus `resync_all()`)
- Test: `tests/sync/test_rebuild.py`

**Interfaces:**
- Consumes: `import_from_notion(..., mode="replace")` (Task 7), `restore_botdata` (Task 14), logo download.
- Produces: `async def rebuild_from_notion(ctx) -> ActionResult` — safety export, maintenance, import (`replace`), restore Bot data, download each DJ `Logo` file URL into `logo_image` (failures listed, not fatal), maintenance off, admin log summary. `web_admins` untouched.

- [ ] **Step 1: Write failing tests:** `test_rebuild_replaces_data_keeps_admins`, `test_rebuild_failure_keeps_old_data`, `test_startup_empty_pg_rebuilds`, `test_startup_empty_notion_pushes_all`.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat: rebuild the database from Notion`

### Task 17: Admin page: sync status, issues, export, import, rebuild

**Files:**
- Modify: `bot/web/app.py` (routes, upload limit), `bot/web/pages.py` (renderers), `bot/web/service.py` (service methods)
- Test: `tests/web/test_app.py`, `tests/web/test_pages.py`, `tests/web/test_service.py`

**Interfaces:**
- Routes (all behind the existing session + CSRF middleware; every POST has a GET confirm page):
  - `GET /backup/export` → download `ravebot-<UTC>.ravebot.json.gz` (`Content-Disposition: attachment`).
  - `GET/POST /backup/import` → upload form / restore; path added to `UPLOAD_PATHS` with its own limit `MAX_BACKUP = 512 * 1024 * 1024` (app `client_max_size` raised to it, other upload paths keep `MAX_UPLOAD` checked in the middleware).
  - `GET/POST /sync/rebuild` → rebuild from Notion.
  - `GET/POST /sync/resync` → `resync_all`.
  - `POST /sync/issues/{id}` with `choice` in `postgres|notion|resynced`.
  - `POST /sync/retry-import` → `run_upgrade_import_if_needed` again (shown only while `ctx.sync_problem` is set).
- Service: `AdminPageService.sync_status() -> SyncStatus(enabled: bool, last_run: datetime | None, last_error: str | None, outbox: OutboxStats, issues: list[IssueRow], problem: str | None, maintenance: str | None)`, plus one method per route returning `ActionResult`.
- Dashboard: a "Data and backup" card (export, import) always; a "Notion sync" card (status, open conflicts with "Keep Postgres" / "Keep Notion", warnings with "Resync Postgres to Notion", rebuild, resync all) only when `enabled`.

- [ ] **Step 1: Write failing tests:** `test_export_requires_login`, `test_export_downloads_file`, `test_import_needs_csrf`, `test_import_large_file_accepted_other_paths_limited`, `test_notion_card_hidden_when_disabled`, `test_issue_keep_notion_calls_resolve`, `test_rebuild_confirm_page`.
- [ ] **Step 2: Run** `uv run pytest tests/web -q` — expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` — expected: PASS.
- [ ] **Step 5: Commit** `feat: admin page backup, restore and Notion sync status`

### Task 18: Cleanup migration and documentation

**Files:**
- Create: `migrations/versions/0018_drop_notion_page_columns.py` (drop `djs.notion_dj_page_id`, drop table `event_shapes`; downgrade re-creates both empty and copies page ids back from `notion_mirror`)
- Modify: `bot/db/djs.py` (remove the page-id helpers and `logo_synced` users that Task 9 made dead), `tests/pg/conftest.py`, `docs/developer-guide.md` (architecture note, tables, sync section), `docs/user-guide.md` (Notion optional, hand edits, conflicts, backup), `README.md` (env vars), `CHANGELOG.md` (Unreleased); outside the repo: `_bmad-output/planning-artifacts/prd.md` and `architecture.md` (reverse the Story 10.4 decision)
- Test: `tests/test_migration_0018.py`, `tests/test_migration_chain.py`

- [ ] **Step 1: Write the failing migration test** `test_0018_drops_columns_and_downgrade_restores_page_ids`.
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Write the migration, remove dead code, update docs.**
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run pytest -m pg -q tests/pg` — expected: PASS, no skipped pg tests when the URL is set.
- [ ] **Step 5: Commit** `chore: drop Notion page id columns, document Postgres as source of truth`
