# Changelog

Notable changes to RaveBot. Newest first.

## Unreleased

### Changed
- `/ravebot-status` marks names an organizer typed into the plan as "(typed in the plan)" (tds layout).
- `/admin cancel` on a slot filled in by hand in the plan explains that the bot only clears names it wrote, instead of "No booking found" (tds layout).

## Epic 8: release hardening (PR #3, 2026-10-18)

### Added
- Single-instance guard: a PostgreSQL advisory lock refuses a second bot against the same database; after a lost connection the bot takes the lock again and only stops if another instance got it.
- Optional `NOTION_DJ_DATABASE_ID` for a separate DJ profile database (classic layout).
- Startup check of Notion property types and of the `DJs` relation target.
- DJs without a Notion page get one at booking time; if that fails the booking is kept and admins are told to link the DJ by hand.

### Changed
- Database credentials are masked in migration and connection errors and in all log output.
- Broad exception handlers narrowed; failures that leave PostgreSQL and Notion apart post `ERR_DB_UNAVAILABLE`.
- "Today" is the configured time zone's date everywhere (event polling, lineup, auto-creation, `/add-event`).
- Per-event Notion locks no longer grow without bound; re-onboarding no longer creates duplicate DJ pages.
- uv image in the Dockerfile pinned; ruff clean.

## Organizer plan layout `tds` (PR #2, 2026-10-18)

### Added
- `NOTION_LAYOUT=tds`: the bot works in an organizers' Notion plan with one text column per hour (`21:00 - 22:00`, `24:00 - 01:00`, ...). `classic` stays the default.
  - A slot is free when its column is empty and nobody holds it in the bot; a name typed in by hand books it as well.
  - Booking writes the DJ's name into an empty column after reading the page again, never over text the bot did not write; `/admin book` replaces only the displaced DJ's text.
  - The written text is recorded (`slot_locks.plan_text`, migration `0007`); cancelling empties the column only while it still holds exactly that text.
  - DJ profiles stay in the bot's database; no DJ pages in the plan.
  - `NOTION_BOOKABLE_STATUSES` limits self-booking and outreach to events in those statuses (checked again right before writing); the first one is set on events the bot creates.
  - `NOTION_CANCELLED_STATUSES`: nights called off get no reminders and are left out of the lineup and `/ravebot-status`.
  - Slots whose column is missing from the plan are never offered; outreach skips DJs already typed into the plan.
  - The startup check reports missing slot columns and unknown status names; the reconcile check reports booked slots missing from the plan.

### Other changes in PR #2 (not specific to the layout)
- All bot messages reworded (plain grammar, US spelling, no jargon); errors say what could not be loaded.
- The event name is read from the title column by type, so the column may be called anything.
- `/admin book` and `/admin cancel` take a date (`YYYY-MM-DD` or `DD.MM.YYYY`) or the exact event name, refuse a name or date that fits several events, and accept only slots 1 to slots-per-event.
- `/admin book` only books members who finished onboarding (points to `/admin onboard` otherwise).
- `/add-event` does not create a second event on a day that already has one.
- Go Active can reopen a closed onboarding form; Go Active without the DJ role asks for the role; a DJ whose role was removed is told so.
- The greeting and embed footers use the bot's current Discord name; more bot replies are mirrored to the DM log.
