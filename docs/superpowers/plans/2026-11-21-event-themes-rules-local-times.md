# Event Genre, Location, Notes, Schedule Rules and Local Times Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Events get admin-editable genre, location and note, scheduled events get genre and location from defaults and conflict-free rules, every lineup shows them when set, and no DJ ever reads a clock time or date that is not in their own timezone.

**Architecture:** Postgres stays the source of truth. A new `schedule_rules` table plus two config keys hold rules and defaults; pure functions in `bot/services/schedule_rules.py` decide hits, conflicts and resolution; `bot/services/schedule.py` is the one service every surface (slash commands, setup wizard, admin page, replenish, Notion intake) calls. Pending rule changes are not stored: they are always recomputed as "upcoming scheduled, not hand-set, stored value differs from resolved value". Times in Discord text become `<t:unix:style>` timestamps; buttons carry only numbers.

**Tech Stack:** Python 3.13, disnake, asyncpg, Alembic, aiohttp (admin page), Notion API via httpx, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-11-21-event-themes-rules-local-times-design.md`

## Global Constraints

- Postgres is the source of truth; Notion is an optional mirror. No user or admin action may await Notion; every event change calls `changed(ctx, "event", uid)` after commit.
- Rules apply only to scheduled events (never to events in `manual_events`).
- A hand value (command, admin page, or Notion hand edit) always beats rules and defaults.
- A rule change never rewrites upcoming events without the admin choosing "Apply to all" or picking events.
- Two rules for the same field that can hit the same schedule date must never both exist.
- No button label contains a clock time or a date. Embed titles keep the plain date (Discord does not render timestamps there).
- Empty genre, location or note produce no line and no section.
- Discord limits: button label ≤ 80 chars, embed description ≤ 4096, ≤ 25 components per message, select menu ≤ 25 options.
- User-facing strings live in `bot/ui/texts.py`; no em dash anywhere (`tests/test_no_em_dash.py`).
- Cogs delegate to services. Every behaviour has a test; Postgres behaviour gets a real-SQL test under `tests/pg/` (run with `RAVEBOT_TEST_POSTGRES_URL=postgresql://ravebot:test@localhost:55432/ravebot_test`, container `ravebot-pg-test`).
- `uv run ruff check .` clean; full suite green before each commit.
- Value limits (decided here): genre ≤ 40 chars, location ≤ 80 chars, single line; note ≤ 300 chars, newlines allowed. Values are stripped; an empty value means "clear".
- Rule `every` is 1..52.
- Commits: author `M4XXX <44500247+M4XXX@users.noreply.github.com>`, no AI attribution lines, check staged changes for secrets.

## Review Focus

1. A rule whose anchor date is not on the schedule grid of another rule (offset not a whole number of intervals) never hits the same date: the conflict check must accept it, and resolution must not misfire. → Task 2 test `test_off_grid_anchors_never_conflict`.
2. A long note plus a full slot list overflows 4096 chars: the note is dropped before any slot line is cut. → Task 6 test `test_long_note_dropped_before_slots`.
3. Stale apply: between showing the apply list and pressing "Apply", an event became hand-set, finished, or the rule changed again. Apply recomputes at press time and only writes events still pending with the value it would show now. → Task 4 test `test_apply_recomputes_at_press_time`.
4. Notion echo: the bot mirrors a rule value to Notion, the next sync cycle reads it back; this must not set the hand marker. → Task 5 test `test_mirrored_rule_value_is_not_a_hand_edit`.
5. Manual, past and finished events are never touched by rules nor listed as pending, and never offered as a rule's starting event. → Task 4 test `test_pending_skips_manual_past_and_finished`.

---

## File Structure

| File | Responsibility |
|---|---|
| `migrations/versions/0019_event_location_and_rules.py` (new) | `events.location`, hand markers, `schedule_rules` table |
| `bot/db/events.py` | `EventDraft`/row mapping gain `location`, `genre_by_hand`, `location_by_hand` |
| `bot/notion/models.py` | `EventInfo` gains the same three fields; `PROP_EVENT_LOCATION`; parse Location |
| `bot/db/schedule_rules.py` (new) | Rules CRUD and the two default config keys |
| `bot/services/schedule_rules.py` (new) | Pure logic: hits, conflicts, resolution, re-anchoring |
| `bot/services/schedule.py` (new) | `ScheduleService`: the single entry point for rules, defaults, per-event values, pending changes and apply |
| `bot/services/event_replenish.py` | New scheduled events get resolved genre/location |
| `bot/notion/schema.py`, `bot/notion/client.py`, `bot/sync/payloads.py`, `bot/sync/intake.py`, `bot/sync/cycle.py`, `bot/sync/worker.py`, `bot/sync/notion_import.py`, `bot/sync/botdata.py` | Location mirrored; Notion hand edits set markers; imports recompute markers; rules table in Bot data |
| `bot/ui/texts.py` | Timestamp helpers; new copy |
| `bot/ui/embeds.py` | Lineup body (meta line, `Slot N` rows, Notes), numbered event picker |
| `bot/ui/views.py` | Number-only buttons; numbered event buttons; numbered cancellation buttons |
| `bot/ui/schedule_views.py` (new) | `ApplyChangesView` (Apply to all / Pick events / Keep) |
| `bot/cogs/admin.py`, `bot/cogs/schedule_cog.py` (new) | `/create event` options, `/edit event`, `/schedule …` |
| `bot/ui/setup_wizard.py`, `bot/ui/setup_options.py` | Default genre/location steps and a rules step |
| `bot/web/app.py`, `bot/web/pages.py`, `bot/web/service.py` | Schedule overview page with editing and apply flow |
| `README.md`, `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md` | Docs |

---

### Task 1: Schema and event fields

**Files:**
- Create: `migrations/versions/0019_event_location_and_rules.py`
- Modify: `bot/db/events.py` (`EventDraft`, `_COLUMNS`, `_event`, `_insert`), `bot/notion/models.py` (`EventInfo`)
- Test: `tests/test_migration_0019.py`, `tests/pg/test_events_repo.py`, `tests/test_migration_chain.py` (if it lists heads)

**Interfaces:**
- Produces:
  - Table `schedule_rules(id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, field TEXT NOT NULL CHECK (field IN ('genre','location')), value TEXT NOT NULL, every INTEGER NOT NULL CHECK (every BETWEEN 1 AND 52), anchor DATE NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`.
  - Columns `events.location TEXT NULL`, `events.genre_by_hand BOOLEAN NOT NULL DEFAULT false`, `events.location_by_hand BOOLEAN NOT NULL DEFAULT false`.
  - `EventDraft(..., location: str | None = None, genre_by_hand: bool = False, location_by_hand: bool = False)` (appended after `notes`), so `update_event`/`edit_event` accept these names.
  - `EventInfo(..., location: str | None = None, genre_by_hand: bool = False, location_by_hand: bool = False)` appended after `slots` (defaults keep every existing constructor call valid).

- [ ] **Step 1: Write the failing migration test** `tests/test_migration_0019.py`, same style as `tests/test_migration_0018.py`:

```python
def test_chain():
    m = _load()
    assert (m.revision, m.down_revision) == ("0019", "0018")

def test_0019_upgrade_and_downgrade():
    m = _load()
    with patch.object(m, "op") as op:
        m.upgrade()
    sql = _sql(op)
    assert "ALTER TABLE events ADD COLUMN location TEXT NULL" in sql
    assert "genre_by_hand BOOLEAN NOT NULL DEFAULT false" in sql
    assert "location_by_hand BOOLEAN NOT NULL DEFAULT false" in sql
    assert "UPDATE events SET genre_by_hand = true WHERE genre IS NOT NULL" in sql
    assert "CREATE TABLE schedule_rules" in sql
    with patch.object(m, "op") as op:
        m.downgrade()
    sql = _sql(op)
    assert "DROP TABLE schedule_rules" in sql
    assert "ALTER TABLE events DROP COLUMN location" in sql
```

- [ ] **Step 2: Write the failing pg test** in `tests/pg/test_events_repo.py`: `test_location_and_markers_round_trip` inserts an `EventDraft(location="Club Vortex", genre="Techno", genre_by_hand=True)`, reads it with `get_event`, asserts `location == "Club Vortex"`, `genre_by_hand is True`, `location_by_hand is False`; then `update_event(pool, uid, location=None, location_by_hand=False)` and asserts `location is None`.
- [ ] **Step 3: Run both; expect FAIL** (`uv run pytest tests/test_migration_0019.py tests/pg/test_events_repo.py -q`).
- [ ] **Step 4: Implement** the migration (upgrade order: add columns, set `genre_by_hand` for non-null genre, create table + index on `guild_id`; downgrade reverses) and the dataclass/column/row-mapping changes. Existing notes need no marker (notes are always hand values).
- [ ] **Step 5: Run the full suite; expect PASS** (`uv run pytest -q`). Fix any test that asserts the exact `_COLUMNS` string or exact `EventInfo` field list.
- [ ] **Step 6: Commit** `feat: add event location, hand markers and schedule rules table`.

---

### Task 2: Rule logic (pure)

**Files:**
- Create: `bot/services/schedule_rules.py`
- Test: `tests/services/test_schedule_rules.py`

**Interfaces:**
- Produces:

```python
Field = Literal["genre", "location"]

@dataclass(frozen=True)
class Rule:
    id: int | None
    field: Field
    value: str
    every: int
    anchor: datetime.date

def rule_hits(rule: Rule, day: datetime.date, interval_days: int) -> bool
def conflicts(new: Rule, existing: Sequence[Rule], interval_days: int) -> list[Rule]
def resolve(field: Field, day: datetime.date, rules: Sequence[Rule],
            default: str | None, interval_days: int) -> str | None
def reanchor(anchor: datetime.date, grid_origin: datetime.date, interval_days: int) -> datetime.date
```

Algorithm (not determined by the signatures):
- `rule_hits`: `delta = (day - anchor).days`; true iff `delta >= 0`, `delta % interval_days == 0` and `(delta // interval_days) % every == 0`.
- `conflicts`: for each existing rule with the same `field` (and a different `id`), let `d = (b.anchor - a.anchor).days`. If `d % interval_days != 0` → no conflict (never on the same grid). Else `k = d // interval_days`; conflict iff `k % gcd(a.every, b.every) == 0` (Chinese remainder theorem: both hit infinitely many common dates). Anchors in the past or future do not matter because both rules hit forever.
- `resolve`: the value of the single rule for `field` that hits `day`, else `default`, else `None`. (Conflict-freedom guarantees at most one; if more than one hits anyway, take the lowest `id` and log a warning.)
- `reanchor`: the smallest `grid_origin + n * interval_days` (n any integer) that is `>= anchor`.

- [ ] **Step 1: Write the failing tests**:

```python
I = 7
D = datetime.date(2026, 12, 5)

def r(every, offset_weeks, value="A", field="location", id=None):
    return Rule(id, field, value, every, D + datetime.timedelta(days=7 * offset_weeks))

def test_alternating_example_from_spec():
    a, b = r(2, 0, "A", id=1), r(2, 1, "B", id=2)
    got = [resolve("location", D + datetime.timedelta(weeks=n), [a, b], "Z", I) for n in range(5)]
    assert got == ["A", "B", "A", "B", "A"]

def test_four_rule_rotation_is_conflict_free():
    rules = [r(4, n, f"L{n}", id=n + 1) for n in range(4)]
    for n, rule in enumerate(rules):
        assert conflicts(rule, rules[:n], I) == []

def test_every_2_and_every_3_collide_and_name_the_rule():
    a, b = r(2, 0, id=1), r(3, 1, id=2)
    assert conflicts(b, [a], I) == [a]

def test_same_offset_mod_gcd_conflicts():
    assert conflicts(r(4, 2, id=2), [r(2, 0, id=1)], I) != []
    assert conflicts(r(4, 1, id=2), [r(2, 0, id=1)], I) == []

def test_off_grid_anchors_never_conflict():
    a = r(1, 0, id=1)
    b = Rule(2, "location", "B", 1, D + datetime.timedelta(days=3))
    assert conflicts(b, [a], I) == []
    assert rule_hits(b, D + datetime.timedelta(days=7), I) is False

def test_different_fields_never_conflict():
    assert conflicts(r(1, 0, field="genre", id=2), [r(1, 0, id=1)], I) == []

def test_default_and_none():
    assert resolve("genre", D, [], "Techno", I) == "Techno"
    assert resolve("genre", D, [], None, I) is None
    assert resolve("location", D - datetime.timedelta(days=7), [r(1, 0)], None, I) is None

def test_reanchor():
    assert reanchor(D, D - datetime.timedelta(days=28), 14) == D
    assert reanchor(D + datetime.timedelta(days=1), D, 14) == D + datetime.timedelta(days=14)
```

- [ ] **Step 2: Run; expect FAIL** (`uv run pytest tests/services/test_schedule_rules.py -q`).
- [ ] **Step 3: Implement** `bot/services/schedule_rules.py`.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat: schedule rule hits, conflicts and resolution`.

---

### Task 3: Rules repository and defaults

**Files:**
- Create: `bot/db/schedule_rules.py`
- Modify: `bot/sync/botdata.py` (`BOTDATA_TABLES`, `_PRIMARY_KEYS`)
- Test: `tests/pg/test_schedule_rules_repo.py`, `tests/pg/test_botdata.py`

**Interfaces:**
- Consumes: `Rule`, `Field` (Task 2); `get_value`/`set_value` (`bot/db/config.py`).
- Produces:

```python
DEFAULT_GENRE_KEY = "default_genre"
DEFAULT_LOCATION_KEY = "default_location"
async def list_rules(db, guild_id: int) -> list[Rule]            # ordered by field, anchor, id
async def get_rule(db, guild_id: int, rule_id: int) -> Rule | None
async def insert_rule(db, guild_id: int, rule: Rule) -> Rule      # returns it with id
async def update_rule(db, guild_id: int, rule: Rule) -> bool
async def delete_rule(db, guild_id: int, rule_id: int) -> bool
async def get_default(db, guild_id: int, field: Field) -> str | None   # "" -> None
async def set_default(db, guild_id: int, field: Field, value: str | None) -> None
```

- [ ] **Step 1: Write failing pg tests**: insert two rules and list them in order; update changes value/every/anchor; delete returns True then False; `set_default(..., "location", "Club")` then `get_default` returns `"Club"`, `set_default(..., None)` returns `None`; rules of another guild are not listed. In `tests/pg/test_botdata.py` add `test_schedule_rules_are_bot_data` asserting a rule row appears in `botdata_rows` under key `schedule_rules:<id>`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement**; add `"schedule_rules"` to `BOTDATA_TABLES` with primary key `("id",)` so the Notion rebuild restores rules. Check `restore_botdata` resets the `schedule_rules_id_seq` sequence after restoring explicit ids (add `setval` if the existing code does not handle serial tables; `outreach_*` tables show whether one is needed).
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat: schedule rules repository and default values`.

---

### Task 4: ScheduleService

**Files:**
- Create: `bot/services/schedule.py`
- Modify: `bot/services/event_replenish.py` (new events get resolved values), `bot/context.py` (attribute `schedule_service`), `bot/main.py` (construct it)
- Test: `tests/services/test_schedule.py`, `tests/pg/test_schedule_service.py`, `tests/services/test_event_replenish.py`

**Interfaces:**
- Consumes: Tasks 1–3; `changed` (`bot/sync/worker.py`); `get_manual_page_ids`; `FINISHED_STATUSES`; `local_today`; `ctx.lineup_service.refresh()`.
- Produces:

```python
@dataclass(frozen=True)
class ScheduleDate:
    date: datetime.date
    event: EventInfo | None        # None: projected, not created yet
    @property
    def label(self) -> str: ...    # "Rave Night · 05.12.2026" (event name via embeds._event_name, else "Rave Night")

@dataclass(frozen=True)
class PendingChange:
    event: EventInfo
    field: Field
    old: str | None
    new: str | None

class RuleConflict(Exception):
    rules: list[Rule]

class ScheduleService:
    def __init__(self, ctx: BotContext) -> None
    async def interval_days(self, guild_id: int) -> int
    async def upcoming_dates(self, guild_id: int, count: int = 10, *,
                             interval_days: int | None = None,
                             first_date: datetime.date | None = None) -> list[ScheduleDate]
    async def add_rule(self, guild_id: int, rule: Rule) -> Rule          # raises RuleConflict
    async def edit_rule(self, guild_id: int, rule: Rule) -> Rule         # raises RuleConflict, LookupError
    async def remove_rule(self, guild_id: int, rule_id: int) -> bool
    async def set_default(self, guild_id: int, field: Field, value: str | None) -> None
    async def resolved(self, guild_id: int, field: Field, day: datetime.date) -> str | None
    async def pending_changes(self, guild_id: int) -> list[PendingChange]
    async def apply(self, guild_id: int, picks: Collection[tuple[str, Field]] | None) -> int
    async def set_event_values(self, guild_id: int, uid: str, *,
                               genre: str | None = UNSET, location: str | None = UNSET,
                               note: str | None = UNSET) -> EventInfo   # raises LookupError
    async def check_reanchor(self, guild_id: int, new_interval: int,
                             grid_origin: datetime.date) -> list[tuple[Rule, Rule]]
    async def reanchor_rules(self, guild_id: int, new_interval: int,
                             grid_origin: datetime.date) -> None
```

Behaviour the signatures do not fix:
- "Upcoming scheduled event" = not in `manual_events`, `date >= local_today()`, status not in `FINISHED_STATUSES`. `upcoming_dates` lists those events plus projected dates continuing the schedule grid (`max(latest scheduled date, today)` + n × interval, or `first_date` + n × interval when no scheduled event exists), sorted, `count` long. Every rule-start picker uses it.
- `pending_changes`: for each upcoming scheduled event and each field whose `<field>_by_hand` is false, `new = resolved(...)`; listed when `new != stored`. Sorted by date, then field.
- `apply(picks=None)` applies every current pending change; with picks only those `(uid, field)` pairs that are still pending **now** (recomputed inside the call), writing the value recomputed now. Writes via `update_event`, then `changed(ctx, "event", uid)` per event, then one `lineup_service.refresh()` (errors logged, not raised). Returns the number of events changed.
- `set_event_values`: `UNSET` leaves a field alone; a non-empty string sets the value and (genre/location) the marker true; `None` or `""` clears: note → `None`; genre/location on a scheduled event → marker false and the value becomes `resolved(...)` immediately; on a manual event → `None`. Values are validated against the Global Constraints limits (raise `ValueError` with the text from `texts`).
- `add_rule`/`edit_rule` validate value (same limits as the field), `every` 1..52, and that `anchor` is a date in `upcoming_dates` (else `ValueError`); `RuleConflict.rules` lists every colliding rule.
- `check_reanchor` returns the conflicting pairs the re-anchored rules would have (empty = safe); `reanchor_rules` updates every rule's anchor with `reanchor(...)`.
- Replenish: each new scheduled event's draft gets `genre=resolved(...)`, `location=resolved(...)`, markers false. Hold the existing replenish lock; the service reads rules once per replenish run.

- [ ] **Step 1: Write failing unit tests** (fake db/repo, patterns from `tests/services/test_event_replenish.py`): `test_add_rule_conflict_names_rules`, `test_add_rule_rejects_anchor_not_upcoming`, `test_set_value_marks_hand`, `test_clear_hand_value_falls_back_to_rule`, `test_clear_on_manual_event_empties`, `test_value_limits` (41-char genre, 81-char location, 301-char note, newline in location → `ValueError`).
- [ ] **Step 2: Write failing pg tests**: `test_pending_lists_changes_and_skips_hand_values`; `test_pending_skips_manual_past_and_finished`; `test_apply_all_and_picked`; `test_apply_recomputes_at_press_time` (compute pending, then set one event by hand and change the rule value, then `apply(picks=<old picks>)` → the hand-set event untouched, the other gets the new value); `test_keep_reoffers_on_next_change`; `test_replenish_resolves_new_events` (rule every 2 at the first projected date → created events alternate rule value and default).
- [ ] **Step 3: Run; expect FAIL.**
- [ ] **Step 4: Implement** the service, wire it in `BotContext`/`main.py`, call it from `_replenish`.
- [ ] **Step 5: Run the full suite; expect PASS.**
- [ ] **Step 6: Commit** `feat: schedule service for rules, defaults and per-event values`.

---

### Task 5: Notion mirror and imports

**Files:**
- Modify: `bot/notion/models.py` (`PROP_EVENT_LOCATION = "Location"`, parse), `bot/notion/schema.py` (`_events_props` adds `"Location": {"rich_text": {}}`), `bot/notion/client.py` (`mirror_event` writes Location), `bot/sync/payloads.py` (`event_payload`, `notion_event_payload`, `event_draft` carry `location`), `bot/sync/worker.py` and `bot/sync/cycle.py` (SELECT lists include `location`; `_FIELD_LABELS` adds `"location": "location"`), `bot/sync/intake.py` (`_EVENT_FIELDS`/`_EVENT_LABELS` add location; a Notion change to genre/location sets the marker), `bot/sync/notion_import.py` and `bot/sync/rebuild.py` (recompute markers)
- Test: `tests/notion/test_models.py`, `tests/notion/test_schema.py`, `tests/sync/test_payloads.py`, `tests/sync/test_intake.py`, `tests/pg/test_sync_cycle.py`, `tests/pg/test_notion_import.py`, `tests/pg/test_rebuild.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4 (`ScheduleService.resolved`).
- Produces: `async def recompute_hand_markers(conn, guild_id: int) -> None` in `bot/services/schedule.py`: for scheduled events, `<field>_by_hand = value IS NOT NULL AND value <> resolved(field, date)`; manual events: `value IS NOT NULL`. Called inside the import/rebuild transaction after `restore_botdata`.

Behaviour: intake already diffs the Notion value against the current Postgres value (which is what the bot mirrored), so a mirrored value reads back equal and is not a change. When the diff contains `genre` or `location`, intake passes `genre_by_hand=True` / `location_by_hand=True` (or false plus the resolved value when the Notion value was cleared on a scheduled event, same rule as `set_event_values`). Notion must not become a required dependency: a database missing the Location property is fixed by the existing `_reconcile` on startup; until then `parse_event` returns `location=None`.

- [ ] **Step 1: Write failing tests**: `test_parse_event_reads_location`; `test_events_schema_has_location`; `test_event_payload_has_location`; `test_notion_location_edit_sets_hand_marker`; `test_notion_genre_cleared_falls_back_to_rule`; `test_mirrored_rule_value_is_not_a_hand_edit` (pg: apply a rule value, mirror snapshot equal to Postgres, run the cycle → `location_by_hand` stays false); `test_import_recomputes_markers` (pg: replace-mode import with a rule in Bot data → event whose location equals the rule value has marker false, differing value has marker true).
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite; expect PASS** (includes `tests/test_no_notion_reads.py`).
- [ ] **Step 5: Commit** `feat: mirror event location to Notion and track Notion hand edits`.

---

### Task 6: Lineup layout and timestamps

**Files:**
- Modify: `bot/ui/texts.py`, `bot/ui/embeds.py` (`lineup_text`, `LineupUpdateEmbed`), `bot/services/lineup.py` (`event_snapshot`), `bot/ui/views.py` (`SlotSelectionView`, `CancellationView`, confirmation texts), `bot/services/reminders.py`, `bot/services/outreach.py`, `bot/services/admin_booking.py`, `bot/cogs/admin.py`, `bot/web/app.py` (callers of the old formatters)
- Test: `tests/test_texts.py`, `tests/test_embeds.py`, `tests/services/test_lineup.py`, `tests/ui/test_booking_flow_texts.py`, plus any test asserting old time strings

**Interfaces:**
- Produces in `bot/ui/texts.py`:

```python
def ts(moment: datetime.datetime, style: str) -> str          # "<t:{int(moment.timestamp())}:{style}>"
def slot_time(event: EventInfo, slot_index: int) -> str       # ts(slot_start, "t")
def slot_when(event: EventInfo, slot_index: int) -> str       # ts(slot_start, "F")
def plain_time(event: EventInfo, slot_index: int) -> str      # old fmt_time, bot timezone, for web page/Notion/logs only
def slot_button(slot_index: int) -> str                       # "Slot 3"
def event_meta(event: EventInfo) -> str | None                # "🎵 Techno · 📍 Club Vortex", parts only when set, None when neither
NOTES_HEADING = "Notes"
```

`fmt_time`, `fmt_slot_when` and `slot_phrase` are removed; `slot_phrase` becomes `"slot 2 at <t:…:t>"` via `slot_time`. Every caller that writes Discord message content, embed descriptions or embed field values uses the timestamp helpers; button labels and embed titles never contain a time; the admin web page and Notion use `plain_time`. `fmt_date` stays for embed titles.

Lineup body (exact shape, `escape_text`/`_safe_dj_name` for user text):

```
{event_meta}                     <- only if not None, then a blank line
{summary}
                                  <- blank line
**Slot 1** · <t:…:t>  {booked icon} {dj}
**Slot 2** · <t:…:t>  {open icon} *open slot*
…
                                  <- blank line, only with a note
**Notes**
{note}
```

Overflow order: if the text exceeds 4096, drop the Notes block first (no partial note), then apply the existing slot cut-off. `event_snapshot` adds `genre`, `location`, `notes` so posted lineups re-render when they change. `CancellationView` buttons become `Booking 1`…`Booking 4`; the message lists `Booking N · {event name} · Slot {n} · {slot_when}`.

- [ ] **Step 1: Write failing tests**:

```python
def test_slot_button_has_no_time():
    assert texts.slot_button(1) == "Slot 2"

def test_slot_time_is_discord_timestamp(event):
    assert re.fullmatch(r"<t:\d+:t>", texts.slot_time(event, 0))

def test_lineup_shows_meta_slots_and_notes(event_factory):
    e = event_factory(genre="Techno", location="Club Vortex", notes="Doors 19:30")
    text = lineup_text(e)
    assert text.startswith("🎵 Techno · 📍 Club Vortex\n\n")
    assert re.search(r"\*\*Slot 1\*\* · <t:\d+:t>", text)
    assert text.endswith("**Notes**\nDoors 19:30")

def test_lineup_without_meta_or_notes_has_no_empty_lines(event_factory):
    text = lineup_text(event_factory())
    assert "🎵" not in text and "📍" not in text and "Notes" not in text

def test_only_location_set(event_factory):
    assert lineup_text(event_factory(location="Club")).startswith("📍 Club\n\n")

def test_long_note_dropped_before_slots(event_factory):
    e = event_factory(slot_count=60, notes="x" * 300)
    text = lineup_text(e)
    assert "Notes" not in text and "**Slot 60**" in text

def test_snapshot_changes_with_location(event_factory):
    assert event_snapshot(event_factory(location="A")) != event_snapshot(event_factory(location="B"))

def test_no_button_label_contains_a_time():
    # build SlotSelectionView and CancellationView with a fixture event; assert no label matches r"\d{1,2}:\d{2}"
```

(Pick a slot count for the overflow test that fits 4096 without the note but not with it; compute it in the test from the line length.)
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement**; update every caller found by `grep -rn "fmt_time\|fmt_slot_when\|slot_phrase\|slot_button" bot`.
- [ ] **Step 4: Run the full suite; expect PASS** (update tests that asserted old plain times).
- [ ] **Step 5: Commit** `feat: lineup shows genre, location and notes; times as Discord timestamps`.

---

### Task 7: Numbered event pickers

**Files:**
- Modify: `bot/ui/embeds.py` (`BookingEventPickEmbed`, `LineupUpdateEmbed`), `bot/ui/views.py` (`EventSelectionView`, `LineupEventPickView`, `_pick_label`), `bot/ui/texts.py`
- Test: `tests/test_embeds.py`, `tests/ui/test_booking_flow_texts.py`, view tests for `LineupEventPickView`

**Interfaces:**
- Consumes: `texts.ts`, `texts.event_meta` (Task 6).
- Produces: `texts.event_button(position: int) -> str` → `"Event 1"` for position 0. `_pick_label` is removed.

Booking picker block per event (position n from 1):

```
**Event {n} · {event name}**
{ts(slot_start(0), "F")}
{event_meta}                 <- only when set
{booked icon} {open} of {total} slots open
```

Under a lineup: `LineupUpdateEmbed` gains an optional `shown: list[EventInfo]` argument; when the message carries `LineupEventPickView`, an embed field named `Events` lists `Event {n} · {event name} · {ts(start, "D")}`, the displayed event in bold. Buttons read `Event 1`…`Event 5`. Custom ids and the position-based reload stay as they are (a press reloads the events and takes the one at that position; that behaviour already handles stale messages and needs a test, not a change).

- [ ] **Step 1: Write failing tests**: `test_booking_picker_is_numbered_with_timestamps` (two events → description contains `**Event 1 · Rave Night**` and `<t:` with style `F`, no `%d.%m.%Y` date pattern outside the name); `test_event_buttons_are_numbered` (`EventSelectionView` and `LineupEventPickView` labels == `["Event 1", "Event 2", …]` plus the existing extra buttons); `test_lineup_events_field_lists_shown_events`; `test_stale_event_press_uses_current_position` (existing behaviour pinned).
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite; expect PASS.**
- [ ] **Step 5: Commit** `feat: numbered event pickers with local timestamps`.

---

### Task 8: Discord admin commands and apply view

**Files:**
- Create: `bot/cogs/schedule_cog.py`, `bot/ui/schedule_views.py`
- Modify: `bot/cogs/admin.py` (`/create event` options; new `/edit event`), `bot/main.py` (load cog), `bot/ui/texts.py`
- Test: `tests/cogs/test_schedule_cog.py`, `tests/cogs/test_admin.py`, `tests/ui/test_schedule_views.py`

**Interfaces:**
- Consumes: `ScheduleService` (Task 4); `future_event_autocomplete` in `AdminCog`; admin whitelist check pattern (`cog_slash_command_check`) and `refuse_in_maintenance`.
- Produces commands (all admin-only, `contexts=SERVER_ONLY`, ephemeral replies):
  - `/create event … genre? location? note?` (values set with hand markers through `set_event_values` after creation).
  - `/edit event event:<autocomplete> genre? location? note? clear:<genre|location|note>?`. At least one option; `clear` and a value for the same field → error text.
  - `/schedule show`: defaults, rules (`#id · Location "B" · every 2 · from Rave Night · 05.12.2026`), then the next upcoming scheduled events with resolved genre/location, hand values marked `(set by hand)`.
  - `/schedule default field:<genre|location> value?` (no value clears).
  - `/schedule rule add field:<genre|location> value:<str> every:<1..52> start:<autocomplete from upcoming_dates>`.
  - `/schedule rule edit rule:<autocomplete> value? every? start?`.
  - `/schedule rule remove rule:<autocomplete>`.
  - `/schedule apply`: opens the apply view for whatever is pending.
  - After every successful default or rule change: if `pending_changes` is non-empty, the reply carries `ApplyChangesView`; else a "No upcoming event changes" line.
- `ApplyChangesView(ctx, guild_id, admin_id, changes: list[PendingChange])`: content lists each change as `{event name} · {dd.mm.} · 🎵|📍 {old or "none"} → {new or "none"}`; buttons `Apply to all`, `Pick events`, `Keep all as they are`. "Pick events" replaces the buttons with a multi-select of up to 25 changes (option label ≤ 100 chars, value `uid:field`) and an `Apply selected` button; more than 25 → note "Showing the first 25; use Apply to all or the admin page for the rest." Only the admin who opened it can press it; timeout 15 minutes. Presses call `ScheduleService.apply` and report the count.
- `start` autocomplete returns `ScheduleDate.label` names with the ISO date as value; conflict errors read `This rule collides with #3 (Location "B", every 2, from Rave Night · 12.12.2026). Remove that rule first.` (one line per colliding rule).

- [ ] **Step 1: Write failing tests** (pattern from `tests/cogs/test_admin.py`): `test_edit_event_sets_values`, `test_edit_event_clear_and_value_same_field_refused`, `test_edit_event_needs_an_option`, `test_create_event_with_genre_location_note`, `test_rule_add_conflict_message_names_rule`, `test_rule_add_offers_apply_view_when_pending`, `test_default_without_value_clears`, `test_non_admin_refused`, `test_apply_view_apply_all`, `test_apply_view_pick_events_caps_at_25`, `test_apply_view_other_user_refused`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite; expect PASS.**
- [ ] **Step 5: Commit** `feat: slash commands for event values, schedule defaults and rules`.

---

### Task 9: Setup wizard steps

**Files:**
- Modify: `bot/ui/setup_wizard.py` (`STEPS`, `VALUE_STEPS`, `DEFAULT_VALUES`, summary, `save_from_draft`), `bot/ui/setup_options.py` (field labels, text validation)
- Test: `tests/ui/test_setup_wizard.py` (or the existing wizard test file)

**Interfaces:**
- Consumes: `ScheduleService.upcoming_dates(interval_days=…, first_date=…)`, `conflicts`, `check_reanchor`, `reanchor_rules`, `set_default`, `add_rule`/`remove_rule`; `ApplyChangesView` (Task 8).
- Produces: steps `"default_genre"`, `"default_location"`, `"rules"` inserted after `"lookahead"` (before `"timezone"`), hidden when the schedule is off (same mechanism as the other schedule steps). Default steps are text value steps: a "None" choice and an "Other" popup (limits from Global Constraints). The `rules` step shows the draft's rule list with `Add rule` (popup: field select, value, every; then a select of the next 10 `upcoming_dates` computed from the draft's interval and first date) and `Remove rule` (select). Draft rules live in `draft.data["rules"]` as dicts; the conflict check runs on add, against the draft list.
- `save_from_draft`: when `event_interval_days` or `first_event_date` changed and stored rules exist, run `check_reanchor`; conflicts → refuse the save with a message naming the pairs; otherwise `reanchor_rules`. Then replace the stored rules with the draft rules and write the defaults. If `pending_changes` is non-empty afterwards, send `ApplyChangesView` as a follow-up.
- A re-run starts from the stored defaults and rules.

- [ ] **Step 1: Write failing tests**: `test_new_steps_follow_lookahead`, `test_steps_hidden_when_schedule_off`, `test_default_location_other_popup_validates_length`, `test_rule_add_conflict_refused_in_draft`, `test_rule_start_choices_follow_draft_interval`, `test_save_writes_defaults_and_rules`, `test_save_refused_when_reanchor_conflicts`, `test_save_offers_apply_view`, `test_rerun_loads_stored_rules`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** (keep `_LEGACY_STEP_KEYS` handling of stored draft step numbers working).
- [ ] **Step 4: Run the full suite; expect PASS.**
- [ ] **Step 5: Commit** `feat: setup wizard steps for default genre, location and rules`.

---

### Task 10: Admin page schedule overview, and docs

**Files:**
- Modify: `bot/web/app.py`, `bot/web/pages.py`, `bot/web/service.py`; `README.md`, `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md`
- Test: `tests/web/test_app.py` (or existing route tests), `tests/web/test_pages.py`, `tests/web/test_service.py`

**Interfaces:**
- Consumes: `ScheduleService` (Task 4), the existing session/CSRF form pattern of the DJ edit pages (`/djs/edit`), `render_notice`, `_confirm`.
- Produces routes:
  - `GET /schedule`: schedule settings (read-only summary of interval, first date, lookahead, enabled), defaults form, rules table with edit/remove links, add-rule form (start = select of `upcoming_dates` labels), upcoming events table (date, name, genre, location, note excerpt, `set by hand` badges, edit link), and a "N upcoming events differ from the rules" banner linking to `/schedule/apply` when `pending_changes` is non-empty.
  - `POST /schedule/defaults`, `POST /schedule/rules/add`, `GET|POST /schedule/rules/{id:\d{1,18}}/edit`, `GET|POST /schedule/rules/{id:\d{1,18}}/remove`, `GET|POST /schedule/events/{uid}` (genre, location, note; an empty field clears).
  - `GET /schedule/apply`: list with a checkbox per pending change; buttons "Apply to all", "Apply selected", "Keep all as they are". `POST /schedule/apply` with `choice=all|selected|keep` and `pick=uid:field` values → `ScheduleService.apply`.
  - After a successful rule/default POST, redirect to `/schedule/apply` when changes are pending, else to `/schedule`. Conflicts and validation errors re-render the form with the message (same wording as Task 8).
  - Dashboard gets a "Schedule" nav link.
- Every POST is refused during maintenance with the existing maintenance notice.

- [ ] **Step 1: Write failing tests**: `test_schedule_page_lists_defaults_rules_and_events`, `test_schedule_requires_login`, `test_post_without_csrf_refused`, `test_add_rule_conflict_rerenders_with_message`, `test_add_rule_redirects_to_apply_when_pending`, `test_apply_selected_applies_only_picks`, `test_event_edit_empty_field_clears`, `test_values_are_html_escaped` (genre `<script>` is escaped).
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Update docs**: README feature table and admin-page list (genre, location, notes, rules, schedule page, local times); user guide sections for `/edit event`, `/schedule …`, the wizard steps, the apply flow and how hand values beat rules; developer guide for `schedule_rules`, markers, `ScheduleService`, timestamp helpers; CHANGELOG "Unreleased" → Added/Changed.
- [ ] **Step 5: Run the full suite and ruff; expect PASS** (`uv run pytest -q && uv run ruff check .`).
- [ ] **Step 6: Commit** `feat: schedule overview on the admin page; docs`.
