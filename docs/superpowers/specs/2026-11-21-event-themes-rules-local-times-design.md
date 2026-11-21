# Event genre, location, notes, schedule rules and local times — design

Date: 2026-11-21. Status: draft for review.

## Why

Events have a theme (genre) and a place (location) that change on a pattern:
today the organizer runs one weekly schedule with a default location and genre,
but every 3rd event is at a different location. DJs need to see genre, location
and any organizer note wherever a lineup is shown. Separately, DJs live in
different timezones, but the bot prints slot times and event dates as plain text
in the bot's timezone, and puts those times and dates on buttons. A DJ for whom
the slot is at 06:00 sees a button saying 22:00.

## Goals

1. Admins can set, change and clear an event's **genre**, **location** and
   **note** (free text), on creation and afterwards.
2. **Schedule defaults and rules** give scheduled events their genre and location
   automatically. A value set by hand for one event always wins.
3. Genre, location and note show on every lineup, but only when set.
4. Every time and date a DJ reads is either a Discord timestamp (shown in the
   reader's timezone) or carries no time at all (buttons).
5. Admins manage all of it through Discord commands, the setup wizard, and a new
   schedule overview on the admin web page.

## Non-goals

- More than one schedule. Rules belong to the one existing schedule.
- Rules for anything except genre and location (no rules for start time, slot
  count, notes).
- Rules for hand-created events (`/create event`); rules never touch them.
- Per-DJ timezone settings. Discord timestamps do the conversion.

## Anti-goals (would count as failure even if it works)

- A rule silently changing an event whose value an admin set by hand.
- A rule change silently rewriting upcoming events without asking.
- Two rules that disagree about the same event being allowed to exist.
- Any button label containing a clock time or a date.
- Showing empty "Genre:", "Location:" or "Notes" lines when nothing is set.

## Current state (as found on 2026-11-21)

- Postgres `events` has `genre` and `notes` (text, nullable). Both sync with the
  Notion "Genre" and "Notes" properties, but no command sets them and no message
  shows them. There is no location column.
- No `/edit event` command exists; events change only through Notion hand edits.
- One schedule: config keys `event_schedule_enabled`, `event_interval_days`,
  `first_event_date`, `event_lookahead_count`; created by
  `bot/services/event_replenish.py`. Hand-created events are recorded in
  `manual_events` and ignored by the scheduler.
- Lineup body (`bot/ui/embeds.py: lineup_text`) prints `**HH:MM**  icon name`
  in the bot timezone. Slot buttons (`texts.slot_button`) read `Slot 2 · 22:00`.
  The event picker (`BookingEventPickEmbed`, `_pick_label`) and the five
  event buttons under a lineup (`LineupEventPickView`) label buttons with plain
  dates. A few confirmation views already use `<t:unix>` timestamps
  (`bot/ui/views.py` around lines 379 and 1021).
- Postgres is the source of truth; Notion is an optional mirror via `changed()`
  and the sync cycle. User actions never wait on Notion; this must stay true.

## What gets built

### 1. Event fields

- New column `events.location` (text, nullable). Notion gets a matching
  "Location" property (rich text) on the events database, mirrored and read by
  the sync cycle exactly like "Genre".
- Per-field "set by hand" markers for genre and location, so the bot knows which
  values a rule may change. A hand value is one set through a command, the
  admin page, or a Notion hand edit. Clearing a hand value removes the marker;
  for a scheduled event the field then falls back to the rule or default value.
- `notes` stays as is and becomes the event note. It is always a hand value.
  It is never touched by rules.
- Length limits: decided by the builder, small enough that a lineup embed stays
  inside Discord's 4096-character description limit with a full slot list
  (the existing cut-off logic must keep slots ahead of the note, or cut the note
  first — builder's call, but slots must never disappear because of a long note).

### 2. Schedule defaults and rules

- **Defaults:** a default genre and a default location for scheduled events
  (config values, both optional).
- **A rule:** field (genre or location), value, "every N events", and a starting
  event. The admin picks the starting event from the upcoming scheduled events,
  shown by name and date. The rule stores the starting event's **date** as its
  anchor.
- **Counting:** a rule counts schedule dates, not existing events: schedule date
  D is position `(D - anchor) / interval_days`; the rule hits when that position
  is `>= 0` and divisible by N. A date with no event (for example taken by a
  hand-created event) still counts, so rotations never shift. Rules therefore
  also cover events the bot has not created yet.
- **Resolution per scheduled event and field:** hand value, else the matching
  rule's value, else the default, else empty.
- **Conflicts:** two rules for the same field conflict when they can hit the same
  schedule date. Creating (or editing into) a conflicting rule is refused with a
  message naming the colliding rule(s); the admin must remove that rule first.
  Rules for different fields never conflict. Example that must be accepted:
  every 2nd starting at event A plus every 2nd starting one event later, giving
  A, B, A, B. Four "every 4th" rules each starting one event later must be
  accepted (full rotation).
- **Changing the interval or anchor validity:** if `event_interval_days`
  changes, rules keep their anchor date; the builder decides how a rule whose
  anchor no longer lands on a schedule date is handled, and must surface it to
  the admin rather than silently misapply it.
- **Applying changes to upcoming events:** when a rule or default is created,
  edited or removed, the bot lists the upcoming scheduled events whose genre or
  location would change, e.g.
  `Rave Night · 05.12. · 📍 Club Vortex → Warehouse 9`, with three choices:
  "Apply to all", "Pick events" (one toggle per event), "Keep all as they are".
  Events with a hand value for that field are not listed (the hand value wins).
  Events created later always follow the current rules. The same flow exists on
  the admin page.
- **New scheduled events** get their genre and location resolved at creation.
- **Hand-created events** (`/create event`) start with whatever the admin enters;
  rules never apply to them.
- Changes to events go through Postgres and `changed()` like every other event
  change, so the Notion mirror follows in the background.

### 3. How lineups look (approved mockup: layout A with B's slot and note style)

```
🎧 Rave Night · Saturday 28.11.2026
🎵 Techno · 📍 Club Vortex, Berlin          <- only the parts that are set; line omitted if none
4 of 6 slots booked · 2 open

**Slot 1** · <t:…:t>  🎤 DJ Nova
**Slot 2** · <t:…:t>  ⚪ *open slot*
…
Notes                                       <- section only when a note is set
Doors open 19:30. Bring your own USB stick.
```

- Slot buttons read only `Slot 2`.
- Applies to: public lineup updates, the lineup shown in the booking flow, and the
  lineup behind the "Lineup" button. Confirmation messages and DMs that mention a
  slot time also switch to Discord timestamps.
- The embed title keeps the plain date (Discord does not render timestamps in
  titles); a timestamp of the event start may be added in the body if the builder
  finds a clean place for it.

### 4. Event picker (approved: numbered events)

- The booking event picker lists events as `Event 1 · Rave Night`, then the start
  as a full Discord timestamp (`<t:…:F>`), then the genre/location line (if set),
  then open slots. Buttons read `Event 1`, `Event 2`, …
- Builder's call (made by the designer): the five event buttons under a lineup
  (`LineupEventPickView`) also become `Event 1`…`Event 5`, matched to a numbered
  list in that message, for the same timezone reason.

### 5. Where admins manage it

- **Discord slash commands** (admin only): set/clear one event's genre, location
  and note; create, edit, list and remove rules; set the defaults. `/create event`
  gets optional genre, location and note options. Command names and grouping are
  the builder's call, following the existing `/create`, `/cancel`, `/config` style.
- **Setup wizard:** new steps after the schedule steps for default genre, default
  location and the rules.
- **Admin web page:** a schedule overview page showing the schedule settings,
  the defaults, every rule, and the upcoming events with their resolved genre and
  location (marking hand values). Defaults, rules and per-event values are
  editable there, with the same conflict check and the same apply-to-upcoming
  flow.

## Constraints

- Postgres stays the source of truth; Notion stays an optional mirror. No user or
  admin action may wait on Notion.
- Schema changes via a new Alembic migration; existing data keeps working
  (existing genre/notes values count as hand values).
- Backup export/import and the Notion upgrade import/rebuild must carry the new
  data (location, hand markers, rules, defaults).
- Discord limits: button labels ≤ 80 chars, embed description ≤ 4096, ≤ 25
  components per message.
- Keep the project's conventions: user-facing strings in `bot/ui/texts.py` (no em
  dash), cogs delegate to services, tests for every behaviour (unit and real-SQL
  where relevant), ruff clean.

## Decisions from the builder check

Owner's answer:

- Existing Notion "Notes" values are shown publicly after the update; no
  migration step or opt-in. Notes are optional; with no note, no Notes section.

Designer's calls (check these):

1. **Notion hand edits vs mirrored values.** A Notion genre/location value counts
   as a hand edit only when it differs from the current Postgres value (which is
   what the bot last mirrored). The bot's own mirrored rule values therefore
   never set the hand marker.
2. **Stored, not computed.** Resolved genre and location are stored on the event.
   "Keep all as they are" leaves the old value stored as a non-hand value; it is
   offered again on the next rule or default change.
3. **Notion "Location" property** is added by the bot, the same way it already
   creates and adopts its databases and properties. A missing property must
   never make the sync fail.
4. **Interval or first date changes with rules present.** The change is allowed.
   The bot lists the existing rules and asks the admin to confirm; each rule is
   re-anchored to the first schedule date on or after its old anchor, and the
   normal apply-to-upcoming flow follows.
5. **Clearing a hand value** on a scheduled event applies the rule or default
   value at once, without a further prompt (the admin acted on that event).
6. **Old backups:** unchanged behaviour. Backup import already refuses a file
   made with another database revision (`IMPORT_REVISION_MISMATCH`), so backups
   from before this change are refused as today. New tables and columns are
   exported and imported automatically (the export covers every table).
7. **"Upcoming scheduled events"** (rule-start picker and apply list) means
   existing scheduled events dated today or later and not Done or Canceled.
   After an apply, the existing lineup refresh updates the posted lineups.
8. **Admin page editing** reuses the existing form + CSRF pattern of the DJ
   edit pages.

## Left to the builder

Command names and options, table and column names, the rule-conflict algorithm
(gcd-based or by enumeration), text and length limits, wizard step layout, admin
page layout, and how stale "Event N" button presses are handled (the existing
position-based reload pattern is the reference).
