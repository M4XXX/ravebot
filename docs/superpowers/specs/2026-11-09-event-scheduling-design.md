# Event scheduling in /setup: design

Date: 2026-11-09

## Intent

Today `/setup` cannot say when the schedule starts. With "every 14 days" the first
event lands one interval after the day of setup (`max(latest event, today) + interval`).
Organizers also cannot run the bot without a schedule, and `/create event` cannot set
slots, start time or slot length per event.

Goals:
- Let the organizer pick the date of the first scheduled event (for example next Saturday, then every 14 days).
- Replace the single "Events" popup (5 fields, Discord's maximum) with one wizard step per setting.
- Let the organizer turn the schedule off. The bot is then fully set up and works with an empty event list.
- Let `/create event` take its own slots, start time and slot length. This works with the schedule on or off.

Non-goals:
- No change to booking, lineup, invites or Notion schema.
- No weekday-based recurrence rules ("every second Saturday"). Interval in days plus a first date covers it.
- No editing or deleting of events from the bot.

Anti-goals:
- A manual event must never shift, replace or suppress scheduled events (decided: manual events are independent of the schedule).
- Leaving "first event date" empty must keep today's behavior.
- No two events with the same event ID.

## Wizard steps

`STEPS` in `bot/ui/setup_wizard.py` becomes:
`welcome, dj_channel, log_channel, roles, slots, start_time, slot_length, schedule_toggle, interval, first_date, lookahead, timezone, invites, summary`.
The "n of N" numbering derives from `STEPS`. When scheduling is off, `interval`, `first_date` and `lookahead` are skipped in both directions (Next and Back) and are not counted in N.

Each value step shows the current value, a dropdown of common values, an "Other" button (opens a popup with one field and the existing validation style `texts.setup_invalid`), Back and Next. Persistent custom ids follow the existing `setup:*` pattern. Re-running `/setup` starts from stored values, as today.

| Step | Dropdown options (builder's call) | "Other" accepts |
|---|---|---|
| Slots per event | 1 to 25 | 1 to 100 (`MAX_SLOTS`) |
| Start time | 18:00 to 23:00 hourly, 00:00, 01:00 | any `HH:MM` |
| Slot length | 15, 20, 30, 45, 60, 90, 120 min | 1 to 1440 |
| Schedule on/off | buttons "Turn on" / "Turn off" (like the invites step) | n/a |
| Interval | 7, 14, 21, 28 days | 1 to 365 |
| First event date | next 25 days labelled with weekday, e.g. "Sat 14 Nov", plus "As soon as the interval allows" (today's behavior) | `YYYY-MM-DD`, today or later |
| Events kept ready | 1, 2, 3, 4, 6, 8, 12 | 1 to 52 |
| Time zone | common zones incl. Europe/Berlin (builder's call) | any valid zone name |

The currently stored value is always selectable even if it is not among the options.
Dates in the first-date dropdown use the draft's time zone (`local_today`).
The summary lists: slots, start time with zone, slot length, "Scheduled events: off" or "every N days from DATE, M ahead" (date part only if set).

## Settings

New config keys (same store as `event_interval_days`):
- `event_schedule_enabled`: `"true"` or `"false"`. Missing means `"true"` (existing installs keep scheduling).
- `first_event_date`: `YYYY-MM-DD`, optional. Written by the wizard on Activate/Save.

`/config show` lists both. First-run draft defaults: schedule on, first date empty.

## Scheduling logic (`bot/services/event_replenish.py`)

1. If `event_schedule_enabled` is false: return 0. Nothing is created at activation or in the poll loop.
2. Manual events (see below) are removed from the event list before any calculation. They do not count toward "kept ready" and do not move the base date.
3. Active scheduled events: local date today or later, status not Done/Canceled, not manual. `missing = lookahead - len(active)`.
4. Base date: if `first_event_date` is set, valid and today or later, and there are no scheduled events dated today or later (any status), the first created event gets exactly that date and the next ones follow at `+interval` steps. Otherwise: `max(latest scheduled event date, today)`, new dates `base + interval * i` (today's rule).
5. If a candidate date's event ID already exists (for example a manual event that day), skip that date and take the next date on the schedule, so the run still creates `missing` events (bounded: stop after `missing + 52` candidates).
6. `first_event_date` is cleared after the first event it applies to has been created. A past or invalid value is ignored (and cleared).

## Manual events

- New table `manual_events(page_id text primary key, created_at timestamptz)` via migration `0015_manual_events.py` (next after `0014`), plus helpers in `bot/db`.
- `/create event` records the new event's `page_id` there. Every other event (all existing ones, events added directly in Notion) counts as scheduled.
- `/create event` gets optional parameters `slots` (1 to 100), `start` (`HH:MM`), `slot_minutes` (1 to 1440). Omitted values use `default_event_shape`. Invalid values get a private error naming the field. The date rules stay (`YYYY-MM-DD`, strictly in the future). The event ID stays `YYYY-MM-DD Rave Night`; a second event on the same date is rejected as a duplicate.
- Works with the schedule on or off.

## Constraints

- Follow existing code style: persistent views, stateless callbacks reading the `setup_drafts` row, texts in `bot/ui/texts.py`, no em dashes (`tests/test_no_em_dash.py`), English only.
- Discord limits: select menus 25 options, modal 5 fields (each popup here has one).
- Existing stored settings and events must keep working unchanged after migration.

## Tests

- Wizard: step order with schedule on and off (forward and back), each dropdown stores its value, each "Other" popup validates and stores, first-date options and past-date rejection, summary text, activation with schedule off creates no events.
- Replenish: schedule off; first date applied once and then cleared; first date ignored when scheduled events exist; manual events ignored for count and base; skipped duplicate date; legacy behavior with no first date.
- `/create event`: optional parameters, validation, fallback to defaults, manual marker recorded.
- Migration test in the style of `test_migration_0014.py`.
- `/config show` lists the new keys.

## Left to the builder

Exact dropdown option sets and labels, popup titles and texts, custom id names, the helper API for `manual_events`, and the common-zone list.
