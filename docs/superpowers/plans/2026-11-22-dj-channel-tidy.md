# DJ Channel Tidy-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the bot posts a lineup update for an event into the DJ channel and the last message in the channel is its own previous lineup post for the same event, delete the old post after the new one was sent (automatic and by-hand posts alike).

**Architecture:** A small table `dj_channel_lineup_posts` (one row per DJ channel: channel id, event key, message id of the bot's latest lineup post) plus a repository module. `LineupService._send` compares the stored record with the event and with `channel.last_message_id` right before sending, stores the new record after a successful send and then deletes the old message best effort.

**Tech Stack:** Python, disnake, asyncpg, Alembic, pytest (asyncio, mocks), ruff.

**Spec:** `docs/superpowers/specs/2026-11-22-dj-channel-tidy-design.md`

## Global Constraints

- Delete only a message the bot recorded as its own lineup post, only when `channel.last_message_id` equals that recorded message id (so nobody wrote after it), and only when the recorded event key equals the event being posted (`EventInfo.page_id`).
- Delete only after the new post was sent successfully; a failed send leaves the old post and the record untouched.
- A failing delete (`NotFound`, `Forbidden`, any error) never raises, never fails the post and never stops the record update.
- `last_message_id` unknown (None) or the channel not cached: nothing is deleted.
- No new permission: delete through `channel.get_partial_message(record.message_id).delete()`; no history fetch.
- The DJ Management channel (`bot/services/management_lineup.py`) is not touched.
- Migration `0021`, `down_revision = "0020"` (`tests/test_migration_chain.py` head assertion moves to `0021`).
- No em dash (U+2014) in user-facing strings under `bot/`. English only.
- Commits authored as M4XXX <44500247+M4XXX@users.noreply.github.com>, no AI attribution or co-author lines. Check staged files for secrets.
- Verification from `output/ravebot`: `uv run pytest -q`, `uv run ruff check .`.

## Review Focus

- A person wrote after the bot's lineup post: the old post is kept (Task 1 test).
- Same channel, last message is the bot's lineup post of another event: kept, record replaced (Task 1 test).
- The new post fails to send: old post and record unchanged (Task 1 test).
- The old post was already deleted by someone: no error, record updated (Task 1 test).
- Restart: a record from the database with the channel's `last_message_id` equal to it still triggers the replacement (Task 1 test with a fresh service instance).
- The by-hand post path and the automatic refresh both replace (Task 1 tests).
- `last_message_id` None: no deletion (Task 1 test).

---

## File Structure

- Create `migrations/versions/0021_dj_channel_lineup_posts.py`.
- Create `bot/db/dj_channel_posts.py`: repository.
- Modify `bot/services/lineup.py`: `_send`.
- Modify docs: `docs/user-guide.md` (section 7.4 lineup posts), `docs/developer-guide.md`, `CHANGELOG.md`.
- Tests: `tests/test_migration_0021.py`, `tests/db/test_dj_channel_posts.py`, `tests/services/test_lineup.py`, `tests/test_migration_chain.py` (head).

---

### Task 1: Storage and the replacement in `LineupService._send`

**Files:**
- Create: `migrations/versions/0021_dj_channel_lineup_posts.py`, `bot/db/dj_channel_posts.py`
- Modify: `bot/services/lineup.py`, `tests/test_migration_chain.py`
- Test: `tests/test_migration_0021.py`, `tests/db/test_dj_channel_posts.py`, `tests/services/test_lineup.py`

**Interfaces:**
- Produces in `bot/db/dj_channel_posts.py` (all `async`, first arg `pool: asyncpg.Pool`):
  - `@dataclass(frozen=True) class LastLineupPost: channel_id: int; event_id: str; message_id: int`
  - `get_last_post(pool, channel_id: int) -> LastLineupPost | None`
  - `set_last_post(pool, channel_id: int, event_id: str, message_id: int) -> None` (upsert on `channel_id`)
- Table `dj_channel_lineup_posts`: `channel_id BIGINT PRIMARY KEY`, `event_id TEXT NOT NULL`, `message_id BIGINT NOT NULL`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`; `downgrade` drops it.
- `LineupService._send(guild_id, event, report_error=True)` keeps its signature and return values (`_SENT`, `_FAILED`, `_NO_CONFIG`). New behavior after resolving the channel: load the record (`get_last_post`; any read error is logged and treated as "no record"); `replace_old = record is not None and record.event_id == event.page_id and getattr(channel, "last_message_id", None) == record.message_id`; send as today keeping the returned message; on `HTTPException` return `_FAILED` as today (record untouched); on success `set_last_post(db, channel.id, event.page_id, sent.id)` (a failure here is logged and does not change the return value), then if `replace_old` delete the old message with `channel.get_partial_message(record.message_id).delete()` inside a try/except that logs and ignores every exception; return `_SENT`.

- [ ] **Step 1: Write failing tests.** Migration test like `tests/test_migration_0020.py` (revision ids, table and columns); update the head assertion in `tests/test_migration_chain.py` to `0021`. Repository tests like `tests/db/test_management_lineup.py` (mocked pool: SQL fragments `ON CONFLICT (channel_id) DO UPDATE`, `get_last_post` returns None for no row and a `LastLineupPost` otherwise with ints). Service tests in `tests/services/test_lineup.py` (follow its fakes for bot, channel, db): a fake channel with `last_message_id` and `send` returning a message with an id and a `get_partial_message(id)` whose `delete` is an AsyncMock; cases: (1) first post for an event: nothing deleted, record stored with the new id; (2) second post for the same event while `last_message_id` equals the stored id: new post sent, record updated, old message deleted exactly once and only after the send (assert call order); (3) a person wrote after (`last_message_id` differs): no delete, record updated to the new post; (4) record belongs to another event: no delete, record replaced; (5) `send` raises `HTTPException`: `_FAILED`, record unchanged, no delete; (6) delete raises `NotFound` (and a generic error): `_SENT` still returned, record updated; (7) `last_message_id` None: no delete; (8) a fresh `LineupService` instance with the same stored record replaces (restart); (9) `post_event` (by-hand path) replaces too; (10) `refresh` (automatic path) replaces too; (11) the record read failing is treated as no record: post still sent, nothing deleted; (12) `set_last_post` failing after a successful send does not change the result and does not delete anything.
- [ ] **Step 2: Run** `uv run pytest tests/test_migration_0021.py tests/test_migration_chain.py tests/db/test_dj_channel_posts.py tests/services/test_lineup.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the migration, the repository and the `_send` change.
- [ ] **Step 4: Run** the same command plus `uv run pytest -q` and `uv run ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: replace the old lineup post in the DJ channel`).

---

### Task 2: Docs and final verification

**Files:**
- Modify: `docs/user-guide.md` (section 7.4: when the bot posts a new lineup for the same event as its last post in the channel, the old post is removed; only its own lineup post, only for the same event, only when nobody wrote after it; also for `/post lineup update`), `docs/developer-guide.md` (the table, the repository, the `_send` rule, the accepted limits: cache-only `last_message_id` after reconnect, a write between check and send), `CHANGELOG.md` (`### Added` under `[Unreleased]`).

- [ ] **Step 1: Update** the documents (no em dash).
- [ ] **Step 2: Run** `uv run pytest -q`, `uv run ruff check .`. Expected: pass.
- [ ] **Step 3: Manual checklist** (needs a live guild; report as not run if unavailable): trigger two lineup updates for one event in a row (the old one disappears); write a message between them (both stay); `/post lineup update` right after an automatic post (old one disappears); different events (both stay).
- [ ] **Step 4: Commit** (`docs: DJ channel tidy-up`).

---

## Self-review notes

- Spec coverage: delete rule and its three conditions, after-send ordering, failure isolation, restart, both post paths, no new permission (Task 1); docs and limits (Task 2).
- Names used across tasks: `dj_channel_lineup_posts`, `LastLineupPost`, `get_last_post`, `set_last_post`.
