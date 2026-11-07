# Changelog

All notable changes to RaveBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Guided `/setup` wizard (admins only): welcome and checks, DJ channel, log channel, roles, events with a time zone button, automatic invites, summary with Activate. Run it again (`Change settings`) to edit settings; it then ends with Save. The wizard keeps its state in the new table `setup_drafts` and survives restarts.
- The bot stays idle until it is activated. While idle it sends no posts, invites, reminders or onboarding DMs, answers buttons and DMs with "I can't answer right now. Please try again later." and answers admin commands other than `/setup` and `/config show` with a hint to run `/setup`. At start an idle bot DMs every admin in `ADMIN_USER_IDS`. A server that already has a DJ channel and a log channel stored counts as active.
- Local time: event start times, slot times, reminders and "today" use a time zone (setting `timezone`, default `Europe/Berlin`). `21:00` stays `21:00` in CET and CEST. The zone is set in `/setup`. New module `bot/timezone.py`.
- Role settings `dj_role_id` and `invites_role_id`, chosen in `/setup`. Without them the bot still uses the role names `DJ` and `Active Booking`.
- Done and canceled events are replaced automatically: the bot keeps the configured number of future events that are not Done or Canceled (`replenish_events`).
- New commands: `/setup`, `/create event`, `/post lineup update`, `/book dj slot`, `/cancel dj slot`, `/list all djs`, `/automatic dj booking`.
- Migration `0011` adds the `setup_drafts` table.
- DJ onboarding is a conversation in the DMs instead of a popup. The bot asks five questions (DJ name, VRChat name, logo, stream link, backup link) one at a time, with Skip and Cancel buttons (and Use it as it is after a stream link that does not look like Twitch, YouTube or stream.vrcdn), and ends with a profile summary to confirm. New wording: DJs now join the "Active DJ Booking Group". Under `My DJ profile` a DJ can change each answer or leave the group. Answers during the conversation are not copied to the log channel.
- The logo is now an image file (PNG or JPEG, including iPhone MPO photos, up to 5 MB) that the DJ sends in the DM, not a link. The bot scales it to at most 1024 px, stores the sanitised image and uploads it to the DJ's Notion row as a file. A logo that Notion refuses never blocks the profile or bookings; it is sent again at the next start. New dependency: Pillow. DJs that joined earlier keep their stored link.
- Migration `0013` adds the DJ profile columns and the `dj_conversations` table.
- Lineup Update posts: the bot posts a new `Lineup Update` message in the DJ channel for every lineup change of the upcoming event (earliest event that is not Done or Canceled). Earlier posts are never edited or deleted, and there is no daily cap. Format: `Event Saturday 14.11.2026`, then one line per slot, `Slot 1  20:00  DJ name` or `OPEN SLOT`.
- `/post lineup update [event]` (admin) posts the lineup of the upcoming event, or of a chosen future event, on demand.
- A `Book a slot` button under every Lineup Update post. It opens the booking flow in the DJ's DMs and replies privately.
- Automatic invite DMs can be turned on or off in the wizard's Automatic invites step (global, default on). `/config show` has an `Automatic invites` row. Booking, reminders and DMs keep working while it is off.
- The bot creates three Notion databases itself under one shared page: Events, Slots (one row per slot) and DJs, plus the views Overview, Lineup and All DJs. Existing databases and views are never recreated.
- Hand edits in Notion are synced: picking a DJ on an open slot becomes a booking, clearing the DJ cell frees the slot. The DJ gets a DM in both cases; conflicting hand entries are reverted and logged.
- Changes to an event's `Start` or `Slot length` in Notion are applied only while no slot of the event has a DJ; otherwise the bot restores the previous values and writes it to the log file only (not to the log channel).
- Event IDs use the format `YYYY-MM-DD <name>`, e.g. `2026-11-14 Rave Night`; duplicates are refused.
- The admin log channel now shows only what needs an organizer: bookings, cancellations, organizer and Notion changes, new DJs, events that became fully booked, critical problems and the DMs users send to the bot. Every entry is a short embed with plain sentences.
- A `Reply` button under every user DM entry. An organizer types an answer in a popup and the bot sends it to the user. The button keeps working after a restart.
- An `Event fully booked` entry is posted once per fill and again after a slot opened and the event filled up once more.
- Migration `0009` adds the `fully_booked_events` table and migration `0010` adds the `log_replies` table. Both run at startup.

### Changed

- An open change under `My DJ profile` expires after 1 hour without an answer: the change is dropped, nothing is saved, and the next DM is handled as a normal DM (log entry and menu). Buttons of the expired question answer that the message is out of date. Onboarding conversations do not expire.
- Start times are local time, not UTC. Existing `Start` values are kept as they are, so an event entered as `20:00` now means 20:00 local time. Lineup posts and reminders show local times. Discord timestamps are no longer used.
- `/config` has only `show` now; it lists the new settings and the bot status. All settings are changed in `/setup`.
- Renamed commands: `/add-event` is `/create event`, `/lineup post` is `/post lineup update`, `/admin book` is `/book dj slot`, `/admin cancel` is `/cancel dj slot`, `/list-djs` is `/list all djs`, `/admin dj set-active` is `/automatic dj booking`.
- The `/config set outreach` switch moved into the setup wizard (Automatic invites step).
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

- Cogs now get their `bot` attribute through `_add_cog` in `bot/main.py`, because disnake 2.12 does not set it. Until now the Notion poll, reminder and outreach loops and the DM handling could fail because cogs had no `bot` attribute. This was found in code review. It has not been tested against a live Discord server, only with the test suite.
- A second booking by the same DJ in one event now shows up in Notion.

### Removed

- Commands `/status`, `/intervene` (start, send, end), `/admin ...`, `/config set ...`, `/add-event`, `/lineup post` and `/list-djs` (see Changed for their replacements; `/status`, `/intervene` and `/config set` have none).
- The intervention flow: `bot/services/intervention.py` and the `interventions` table. Migration `0012` drops the table.
- The unused settings `outreach_max_contacts` and `event_recurrence_cron`. Stored values stay in the `config` table and are ignored.
- DM transcripts, button tap logs and bot reply logs in the admin log channel, plus the old error embed.
- The edit-in-place lineup roster embed with its one-post-per-day limit, plus the Mission Roster, Mission Overview and proactive outreach embeds.
- The single shared Notion database for events and DJs, and its startup property check.
- The TDS layout and the extra optional Notion keys.
