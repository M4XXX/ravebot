# Changelog

All notable changes to RaveBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Lineup Update posts: the bot posts a new `Lineup Update` message in the DJ channel for every lineup change of the upcoming event (earliest event that is not Done or Canceled). Earlier posts are never edited or deleted, and there is no daily cap. Format: `Event Saturday 14.11.2026`, then one line per slot, `Slot 1  20:00  DJ name` or `OPEN SLOT`.
- `/lineup post [event]` (admin) posts the lineup of the upcoming event, or of a chosen future event, on demand.
- A `Book a slot` button under every Lineup Update post. It opens the booking flow in the DJ's DMs and replies privately.
- `/config set outreach` turns the automatic invite DMs on or off (global, default on). `/config show` has a new `Automatic outreach` row. Booking, reminders and DMs keep working while it is off.
- The bot creates three Notion databases itself under one shared page: Events, Slots (one row per slot) and DJs, plus the views Overview, Lineup and All DJs. Existing databases and views are never recreated.
- Hand edits in Notion are synced: picking a DJ on an open slot becomes a booking, clearing the DJ cell frees the slot. The DJ gets a DM in both cases; conflicting hand entries are reverted and logged.
- Changes to an event's `Start` or `Slot length` in Notion are applied only while no slot of the event has a DJ; otherwise the bot restores the previous values and logs it in the admin log channel.
- Event IDs use the format `YYYY-MM-DD <name>`, e.g. `2026-11-14 Rave Night`; duplicates are refused.

- The admin log channel now shows only what needs an organizer: bookings, cancellations, organizer and Notion changes, new DJs, events that became fully booked, critical problems and the DMs users send to the bot. Every entry is a short embed with plain sentences.
- A `Reply` button under every user DM entry. An organizer types an answer in a popup and the bot sends it to the user. The button keeps working after a restart.
- An `Event fully booked` entry is posted once per fill and again after a slot opened and the event filled up once more.
- Migration `0009` adds the `fully_booked_events` table and migration `0010` adds the `log_replies` table. Both run at startup.

### Changed

- The first poll after deploy announces every currently fully booked future event once, because nothing is stored for them yet.
- Critical problems (DM to a DJ failed, Notion write failed, hand edit reverted, no more DJs to ask, database unavailable, something failed) are plain entries with a `What to do` line. The error codes no longer appear in the channel.
- The notice about newly created Notion databases is titled `New Notion databases created`.
- The "no more DJs to ask" entry reads `The only DJ in the pool was asked` instead of `All 1 DJs` when one DJ was asked.
- All bot texts (DMs, buttons, embeds, replies) were reworded and now live in `bot/ui/texts.py`.
- User-facing text contains no em dash. `tests/test_no_em_dash.py` enforces this for `bot/`.
- The config keys `lineup_snapshot`, `lineup_last_post_date` and `lineup_post_message_id` are no longer used. `lineup_post_snapshot` replaces them. Old values stay in the database and are ignored.
- `NOTION_PARENT_PAGE_ID` (an empty page shared with the integration) replaces `NOTION_DATABASE_ID`.
- The bot stops at startup with a clear message if the Notion parent page is not shared with the integration or the key/ID is wrong.
- Reminder timing is now relative to the event start, and slot times use the event's own `Start` and `Slot length`.
- New events use the configured default start time, slot length and slots per event.
- License: AGPL-3.0, with a commercial license available on request.

### Fixed

- A second booking by the same DJ in one event now shows up in Notion.

### Removed

- DM transcripts, button tap logs and bot reply logs in the admin log channel, plus the old error embed.
- The edit-in-place lineup roster embed with its one-post-per-day limit, plus the Mission Roster, Mission Overview and proactive outreach embeds.
- The single shared Notion database for events and DJs, and its startup property check.
- The TDS layout and the extra optional Notion keys.
