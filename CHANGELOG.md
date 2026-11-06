# Changelog

All notable changes to RaveBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- The bot creates three Notion databases itself under one shared page: Events, Slots (one row per slot) and DJs, plus the views Overview, Lineup and All DJs. Existing databases and views are never recreated.
- Hand edits in Notion are synced: picking a DJ on an open slot becomes a booking, clearing the DJ cell frees the slot. The DJ gets a DM in both cases; conflicting hand entries are reverted and logged.
- Changes to an event's `Start` or `Slot length` in Notion are applied only while no slot of the event has a DJ; otherwise the bot restores the previous values and logs it in the admin log channel.
- Event IDs use the format `YYYY-MM-DD <name>`, e.g. `2026-11-14 Rave Night`; duplicates are refused.

### Changed

- `NOTION_PARENT_PAGE_ID` (an empty page shared with the integration) replaces `NOTION_DATABASE_ID`.
- The bot stops at startup with a clear message if the Notion parent page is not shared with the integration or the key/ID is wrong.
- Reminder timing is now relative to the event start, and slot times use the event's own `Start` and `Slot length`.
- New events use the configured default start time, slot length and slots per event.
- License: AGPL-3.0, with a commercial license available on request.

### Fixed

- A second booking by the same DJ in one event now shows up in Notion.

### Removed

- The single shared Notion database for events and DJs, and its startup property check.
- The TDS layout and the extra optional Notion keys.
