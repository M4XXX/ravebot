# PostgreSQL as source of truth, Notion as live mirror — design

Date: 2026-11-20. Status: draft for approval.

## Intent and why

Today Notion holds the events, slots (bookings) and DJs, and the bot reads them live
(`NotionClient.list_events()` queries all three databases in full, about 20 call sites, no
cache). A booking waits on several Notion calls (up to about 33 s each with retries) before the DJ
gets a reply. Postgres only holds `slot_locks` and the DJ profile. Story 10.4 (2026-11-05) decided
"Notion is authoritative". This design reverses that decision.

New model: the bot reads and writes only Postgres. Notion is a live backup and restore source that
organizers may also edit by hand; hand edits flow back into Postgres after being checked. The admin
page gains full backup, restore and sync-status features.

## Goals

- Bookings, DJ changes, cancellations and every bot read are served from Postgres only. No Notion
  call sits in a user-facing path (decision 9).
- Notion is optional (decision 10).
- Every change is mirrored to Notion in the background; Notion being slow or down never blocks or
  loses a change.
- Hand edits in Notion (changed values, added booking, new DJ, new event) are validated like the
  equivalent command and applied to Postgres, then logged to the info channel.
- Destructive or identity-changing hand edits are never applied silently: warn, offer a resync.
- Admin page: export and import of the complete Postgres database as one file, sync status, and
  one-button rebuild of Postgres from Notion.
- Postgres lost, Notion filled: rebuild. Postgres filled, Notion empty: create and fill Notion.

## Non-goals

- Realtime push from Notion (no webhooks; polling is enough).
- Hand-editing bot internals in Notion ("Bot data" is read-only).
- Multi-guild or multi-master replication.

## Anti-goals (a failure even if it technically works)

- A booking, DJ or event silently lost or duplicated by a sync, import or rebuild.
- A Notion outage, a Notion mass-deletion or an empty Notion read wiping Postgres data.
- Admin accounts or secrets (API keys, tokens) ending up in Notion.

## Decisions by the project lead (2026-11-20)

1. Postgres is the main source of truth for all data the bot holds.
2. Notion mirrors everything except admin accounts: events, slots, DJs with logos (as today),
   plus the bot config, outreach, reminder and DM-conversation state.
3. Hand edits in Notion that the bot applies automatically (and logs to the info channel): a
   changed value, an added booking, a new DJ, a new event. A new DJ or event must pass the same
   checks as the command path (valid data, unique name or ID, Slots rows created, no missing
   links); otherwise it counts as a bigger change.
4. Bigger changes only warn (admin channel and admin page, with a "resync Postgres to Notion"
   option): any deletion, any change to a key other records refer to.
5. Conflict (Notion edit to something with an unsynced Postgres change): the bot posts what changed
   on each side with two options, keep Postgres or keep Notion. Syncing stalls only for the
   conflicting entity (that booking, DJ or event); everything else keeps syncing. After at most
   24 h without a decision Postgres wins, overwrites Notion and the bot logs what happened.
6. The non-event data ("Bot data") is a read-only backup; hand edits there are ignored and
   overwritten, with a note on the rows.
7. The export and import file from the admin page is complete, including admin accounts.
8. Upgrade: on the first start after the update the bot writes a safety export, then imports
   events, slots and DJs from Notion into Postgres once, automatically, and posts the result in
   the admin channel. Notion syncing stays off until that import is done.
9. Notion never slows or blocks a Discord response. A DJ booking a slot, changing a profile or
   anything else that changes a DJ gets the answer as soon as Postgres has saved the change. The
   Notion mirror runs afterwards in the background, and a slow, failing or unreachable Notion does
   not delay, block or fail the request. No Discord handler awaits a Notion call, and the Notion
   poll and outbox worker never hold a lock or connection a Discord handler needs.
10. The Notion backup is optional. Without `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID` the bot
    runs on Postgres alone: no outbox, no poll, no Notion calls, no startup failure, and the admin
    page hides the Notion sync, status and rebuild features. Export and import still work. When an
    admin adds the keys later, Notion is empty and Postgres is filled, so the bot creates the
    Notion databases and fills them. A bot that runs on Postgres alone is a supported setup and
    gets tests of its own.

## Design

### Data model

- New Postgres tables `events` and `slots`, plus the DJ fields already in `djs`. Events get their
  own stable key (a UUID, `event_uid`) so Postgres is not keyed by Notion page ids. Every mirrored
  row stores `notion_page_id` (nullable until first synced), `notion_synced_at` and a
  `dirty`/revision marker. The existing tables keyed by the Notion page id (`slot_locks`,
  `fully_booked_events`, `manual_events`, `event_shapes`, `outreach_*`, `reminders_sent`) are
  migrated to `event_uid`. `slot_locks` is folded into `slots` (holder column plus the existing
  row lock for concurrent bookings).
- Event key policy (builder may refine): `events.event_id` (the human title such as
  `2026-11-20 Rave Night`) stays unique; it is the identifier the checks for "changed key" use,
  together with a DJ's Discord ID and the slot number.
- Outbox table `notion_outbox`: one row per pending Notion write (entity, key, operation,
  payload revision, attempts, last error, next attempt). Writes are coalesced per entity (latest
  state wins) and executed by one background worker in order, rate limited to the Notion API
  limit (about 3 requests per second), with backoff.
- Sync state table `notion_sync_state`: last successful poll, last full compare, open conflicts and
  warnings (each with a created-at for the 24 h timer), per-entity Notion snapshot hash used to
  detect hand edits.

### Write path

Booking, cancel, DJ edit, event create/cancel, admin overrides: validate, write Postgres in one
transaction (including the outbox row), reply. The Notion worker mirrors it afterwards. The
existing Notion-in-the-flow code (`ensure_dj_page`, `assign_slot`, the in-flight grace periods in
`notion_sync_plan.py`) goes away or moves into the worker.

### Read path

`list_events()`-style reads come from a Postgres repository that returns the same `EventInfo`
model, so lineup, outreach, reminders and the booking UI keep their interfaces.

### Sync cycle (poll, every 5 min as today, builder may tune)

1. Drain the outbox (also continuously in the background).
2. Read Notion (Events, Slots, DJs, Bot data) and compare against the stored per-entity snapshot
   and Postgres.
3. Classify each difference:
   - Notion changed, Postgres unchanged since last sync: apply (changed value, added booking, new
     DJ, new event) after validation, log to the info channel.
   - Notion changed and Postgres has an unsynced change to the same entity: conflict (decision 5).
   - Notion row deleted, key changed, validation of a new row failed, or a mass change: warning,
     nothing applied (decision 4).
   - Postgres changed, Notion unchanged: outbox handles it.
4. Safeguard: an empty or failed Notion read, or a Notion state that would delete more than a
   threshold of Postgres rows, is a warning, never an apply. (Builder chooses the threshold.)

### Startup and recovery cases

| Postgres | Notion | Action |
|---|---|---|
| empty | filled | Rebuild Postgres from Notion (also the admin button). |
| filled | empty or databases missing | Create Notion databases (existing `ensure_schema`) and fill from Postgres. |
| filled, unsynced writes | any | Outbox pushes them. |
| filled, no events table yet (upgrade) | filled | Decision 8. |

Rebuild from Notion restores events, slots, DJs with logos, config and the bot-state tables
mirrored in "Bot data". Admin accounts are not in Notion: after a rebuild the environment owners
(`ADMIN_USER_IDS`) work as today and the rest come back only from an export file.

### Notion layout

Events, Slots and DJs databases stay as they are (readable and hand-editable). Added: one
"Bot data" database, one row per record (table name, record key, JSON payload, updated-at),
marked read-only in its description and a note property. Logos stay as Notion files.

### Admin page

- Sync status: last sync, outbox size and oldest age, last error, open conflicts and warnings.
- Conflict and warning list with the actions: keep Postgres, keep Notion, resync Postgres to
  Notion (confirm step).
- Export: one file containing the complete database including logos and admin accounts (builder
  chooses the format; the container has no guarantee of `pg_dump`, so a Python-side export of all
  tables through asyncpg that records the migration revision is preferred).
- Import: upload that file, confirm step, replaces the database in one transaction, refuses a
  file from a newer migration revision, writes an automatic safety export first, then forces a
  full Notion resync.
- Rebuild from Notion: one button, confirm step, safety export first.
- Same authentication, CSRF and security headers as the existing admin routes.

### Logging

Info channel: every hand edit applied. Admin channel: warnings, conflicts, 24 h overrides, upgrade
import result, rebuild and import results.

## Calls made while writing the design (designer's choices, not decisions by the project lead)

- **Bot data allowlist.** Config rows (channel IDs and settings; secrets live in the environment,
  not in `config`), `dj_conversations` (step and draft data, no message text), outreach, reminders,
  `event_shapes`, `fully_booked_events`, `manual_events`, `setup_drafts`. Excluded: `web_admins`,
  `log_replies`, and the `config` rows with `guild_id = 0` (Notion database IDs; after a rebuild
  they are found again through `NOTION_PARENT_PAGE_ID`).
- **Event identity in Notion.** Events carry `event_uid` in a new Notion property `Bot ID`. A
  hand-added event gets one when it is accepted. A rebuild remaps by `Bot ID`, falling back to the
  event title.
- **Upgrade import fails validation** (duplicate titles, slots without an event, DJs without a
  Discord ID, Notion unreachable): the import is one transaction and rolls back, the bot stays in
  its existing idle mode (no bookings, no Notion writes), and the admin channel gets the reasons
  and a retry button on the admin page. The schema migration is split from the data import; tables
  keyed by Notion page ids are remapped to `event_uid` only after a successful import.
- **Rebuild from Notion** is a full replace in one transaction after the safety export. The outbox
  and open conflicts are discarded.
- **Hand-added booking** is accepted only if the slot is free in Postgres and the DJ exists (or is
  created through the DJ checks). It then runs the same side effects as the current "adopt": DM
  to the DJ, log entry, lineup refresh, fully-booked check.
- **Changed key** means a Notion page whose event title, DJ Discord ID or slot number differs from
  the stored one (matched by `notion_page_id`). A pasted duplicate page with an existing key is a
  warning, not a new row.
- **Import and rebuild** put the bot in a maintenance state: bookings and edits are refused with a
  short message until the operation finishes.
- **Single process.** The bot already refuses a second instance (Story 8.2 guard); the outbox
  worker, poller and 24 h timers rely on that.

## Constraints

- Python/asyncpg/aiohttp/disnake, Alembic migrations, Notion API version 2026-03-11, existing
  fake Notion transport for tests.
- Existing deployment must upgrade in place (decision 8); migrations must be reversible or
  protected by the safety export.
- The slot-booking concurrency guarantee (two DJs, one slot) must stay.

## Left to the builder

Table and column names, outbox coalescing details, thresholds for the mass-change safeguard,
export file format, poll interval, how `EventInfo` is built from Postgres, migration order, the
test strategy (including real-Postgres tests with `-m pg`).

## Docs to update

PRD, architecture (reverse the Story 10.4 decision), `docs/developer-guide.md`,
`docs/user-guide.md`, `CHANGELOG.md`.
