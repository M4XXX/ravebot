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

### Changed

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

- The edit-in-place lineup roster embed with its one-post-per-day limit, plus the Mission Roster, Mission Overview and proactive outreach embeds.
- The single shared Notion database for events and DJs, and its startup property check.
- The TDS layout and the extra optional Notion keys.
