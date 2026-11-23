# Changelog

All notable changes to RaveBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.3.0-rc2] - 2026-11-23

### Changed

- The public lineup post shows the slots as two aligned columns, `Slot` (slot number and time) and `DJ`, instead of one line per slot, so the DJ names line up. The headphone icon in front of a DJ name is gone, open slots show a green circle, and DJ names are cut at 24 characters so they cannot wrap and shift the rows. Times are still Discord timestamps in each reader's time zone.

### Added

- Owner-only commands `/admins add user`, `/admins remove user` and `/admins list` to manage the admin list from Discord. Answers are private; owners cannot be removed and bot accounts cannot be added.
- Admin removals are now written to the admin log channel, also when done on the admin page. Admin log entries for adding and removing say who did it. The admin page now also refuses to add a bot account.
- Booking rules: a slot limit per DJ and event (default 2) and a cancellation cutoff (default 60 minutes before the start). The limit is checked inside the booking transaction, events at the limit are not offered, invites to such DJs are skipped, and cancelling inside the cutoff is refused. Admin bookings, admin cancellations and Notion hand edits are not limited. Existing installs start with both rules on after the update.
- Setup wizard step "Booking rules" (before the summary) with two toggle buttons and a popup for the numbers; the summary and `/config show` list both rules.
- Admin page "Booking rules" to switch the rules and change the numbers.
- DJ Management lineup message: an optional channel (new setup wizard step, right before the summary) gets one lineup message per fully booked event, with start time, slot length, genre, location, note, and per slot the DJ name and stream links. The bot edits it when bookings, DJ profiles or event details change, shows open slots with a note when the event is no longer fully booked, and never posts a second message for an event or deletes one.
- "Send me more information" button on that message, admins only: sends the complete current lineup as DMs (Discord name, VRChat name, stream links and logo per DJ). Other users get a private refusal.
- Setup question for existing fully booked events: post all or only new ones. `/config show` lists the DJ Management channel.
- Database migration 0020 (`management_lineup_messages`).
- DJ channel tidy-up: when the bot posts a lineup update and the last message in the DJ channel is its own previous lineup post for the same event, the old post is removed. Posts for other events and posts followed by someone else's message stay; a failed post never removes the old one. Also applies to `/post lineup update`. Database migration 0021 (`dj_channel_lineup_posts`).
- DM menu after flows: about 2 seconds after a booking, cancel, invite answer or DJ setup ends (done, cancelled, declined or failed), the bot sends the menu again, so the menu is the last message in the DM. Showing the lineup does not send it. The bot deletes its own previous menu in that DM. Flows that time out without an answer send no menu. Database migration 0022 (`dm_menus`).
- Event announcements: the bot can post an announcement for each event in one announcement channel. The general announcement is posted by rules ("3 days before at 18:00"; switching it on creates that rule when there is none) and the start announcement is posted when the event starts, shows who plays now and who is next, and follows the slot changes. Both are off by default, skip events whose lineup is not fully booked, are edited when the event changes, and say "This event was canceled." for canceled or deleted events. `@everyone` and `@here` never ping, role mentions do.
- Announcements details: the start announcement is also posted late for an event that began before midnight, a general announcement follows the lineup (also a freed slot) until the event starts and is never rewritten as canceled afterwards, and editing a rule never posts retroactively.
- Announcement templates with placeholders (`{title}`, `{date}`, `{lineup}`, `{now_playing}` and more) are edited on the new admin page "Announcements", which also holds the channel, both switches and the rules. **Check and preview** shows the result before saving, and Save runs the same check. Commands: `/announcements show`, `channel`, `general`, `start` and `rule add|edit|remove`. The setup wizard has an optional "Announcement channel" step. Database migration 0023 (`announcement_rules`, `announcement_posts`).
- Security audit of all admin-only entry points (`docs/security-audit-2026-11-23.md`) and guard tests (`tests/test_security_guard.py`) that fail when a command, admin page route, view or popup is added without an admin check.
- Container smoke test (`scripts/smoke-container.sh`): the image is started like in production (PostgreSQL, root-owned mapped backup folder, entrypoint) and checked for the non-root user, a writable backup folder, imports, migrations, a safety export and the start-up up to the Discord login. `scripts/release.sh`, CI and the release workflow run it; no image is published without it and the real-SQL tests.
- Backups made with an older version can now be restored: the bot migrates the file's data to the current version (before, only a file of the exact same revision was accepted). A failed migration puts the data from before the restore back. New real-SQL tests cover every old revision: data survives each migration, every old backup imports and migrates, each migration goes down and up, and failures leave a working database.

### Fixed

- The log "Reply" popup now checks the admin list when it is submitted, not only when it is opened.

## [1.3.0-rc1] - 2026-11-21

### Changed

- Times in the lineup, booking menus and messages are Discord timestamps, so every reader sees their own time zone. Embed titles keep the plain date; the admin page and Notion show plain local times.
- Lineup slot rows are numbered, and buttons show only numbers (`Slot 2`, `Event 1`, `Booking 1`) instead of times or dates.
- PostgreSQL is now the source of truth for events, slots, DJs and settings; this reverses the earlier decision to keep Notion as the source of truth. The bot reads and writes only PostgreSQL, and no Discord reply waits for Notion any more. Notion is an optional live mirror that the bot keeps up to date in the background.
- Notion is optional: without `NOTION_API_KEY` and `NOTION_PARENT_PAGE_ID` the bot runs on PostgreSQL alone, with no Notion calls and no startup failure. Both variables are no longer required.
- Hand edits in Notion (a changed value, a booking, a new DJ, a new event) are checked like the matching command, applied and logged as `Applied from Notion`. A risky edit (a deleted page, a changed key, a failed check, many records missing) is only reported as a warning. A conflict with an unsynced bot change waits for a choice, Keep Postgres or Keep Notion, on the admin page; after 24 hours the bot's version wins.
- A new read-only Notion database, Bot data, mirrors the bot's own state (settings, invite and reminder history, drafts). Hand edits there are overwritten.
- The first start after the update saves a safety backup and imports events, slots and DJs from Notion once. Booking and edits are paused until it works; a failed import can be retried on the admin page.
- Database migration 0018 drops `djs.notion_dj_page_id` (the page ids live in `notion_mirror` since 0017) and the unused `event_shapes` table.

### Added

- Events have a genre, a location and a note. `/create event` takes optional `genre`, `location` and `note`; the new `/edit event` sets or clears them. The lineup shows each only when set. Limits: genre 40, location 80, note 300 characters.
- Schedule rules and defaults: `/schedule show`, `/schedule default`, `/schedule apply` and `/schedule rule add|edit|remove` set the genre or location of every n-th scheduled event (`every` 1 to 52). Two rules that can hit the same date are refused. The setup wizard has steps for the default genre, the default location and the rules.
- Apply flow: after a default or rule change the bot lists the upcoming events that would change; choose Apply to all, Pick events or Keep all as they are. A value set by hand always beats rules and defaults.
- Admin page: a Schedule page with the schedule settings, defaults, rules, upcoming events (genre, location, note, set by hand marks) and the apply list.
- Notion: a `Location` property on the Events database (added automatically). A Notion edit of genre or location counts as set by hand.
- Database migration 0019 (`events.location`, hand markers, `schedule_rules`).
- `BACKUP_DIR` (default `/data/backups`) for the safety backups. The image declares `/data/backups` as a volume and the Unraid template maps it to `/mnt/user/appdata/ravebot/backups`; map it in a plain `docker run` with `-v ravebot-backups:/data/backups`.
- Admin page: download a backup of the whole database and restore it from a file (with a safety backup first), a Notion sync card (last sync, waiting changes, errors, conflicts and warnings with Keep Postgres, Keep Notion and resync choices), resync all to Notion, and rebuild PostgreSQL from Notion after a data loss.
- `/cancel event`: cancel one upcoming event after a confirm step. The event gets the status
  `Canceled` in Notion and its bookings are removed. Booked DJs are told by DM, the log
  channel names DJs to tell by hand (closed DMs, guests without Discord), and the DJ channel
  gets a notice when DJs were booked.

### Fixed

- The container failed to start with `PermissionError: ... /root/.postgresql/postgresql.key`: after dropping to the `ravebot` user, `HOME` still pointed at `/root`. The entrypoint now sets `HOME=/home/ravebot`, and the image creates that folder.
- A booking mirrored to Notion while the Notion poll was reading could be read as a hand edit and freed again; the mirror worker now waits while a poll compares its snapshot.
- First start after the update with the Notion databases missing: the bot now stays paused until the one-time import ran, instead of working on an empty database. **Retry the import from Notion** reads the databases again first.
- Past bookings of a deleted DJ are no longer freed by the Notion poll, and slots of past events are never changed from Notion.
- After a backup restore, events, DJs and slots added in Notion after the backup are moved to the Notion trash with a warning on the first sync instead of being taken in (no re-created records, no DMs).
- A booking for a slot that no longer exists (event deleted, slot count lowered) is refused with a message instead of being stored where nobody sees it.
- The one-time import keeps a booking whose Notion DJ page cannot be read; without one it stops and lists the slot instead of importing it as free.
- A hand-made slot page that names another DJ than the bot's booking is still overwritten, but now with a warning.
- A backup file that unpacks to more than 1 GB is refused.
- The missing-databases notice no longer claims that booking is paused when it is not.
- **Retry the import from Notion** is shown on the admin page also while the Notion databases cannot be reached, and a network error during the retry gives a message instead of an error page.
- **Create new databases** warns in bold, while the one-time import from Notion has not run, that the bot would then start with no events and no bookings.
- A rebuild from Notion right after a backup restore no longer lets the next sync trash pages the rebuild skipped, and its "import done" mark is written in the same transaction as the data.
- The container makes a mapped backup folder writable for its user at every start (Unraid creates new folders as root), so restores, rebuilds and the one-time import no longer fail on the safety backup.
- An admin booking for a slot that no longer exists is refused instead of creating a hidden slot.
- A failed Notion write no longer delays a newer change of the same record; the admin page shows the newest Notion write error.
- Changes made just before a shutdown reach the Notion queue before the database closes.
- Deleting a DJ locks their bookings in a fixed order, so it cannot deadlock with an event edit; restores, rebuilds and **Keep Notion** pause the Notion writer while they change the data.

- Canceled events no longer get outreach invites, reminders or a fully-booked entry, and the
  Notion sync no longer books DJs back into them.
- Booking and cancel menus in DMs: when a menu expires, its buttons are disabled and an
  untouched menu says that it has expired, instead of leaving buttons that end in "This
  interaction failed".

## [1.2.0] - 2026-11-09

### Fixed

- Booking and cancelling: buttons of one DJ can no longer reach another DJ's menu, an old menu can no longer free or continue a newer booking, and a menu that is closed or ignored no longer blocks the DJ until a restart (it ends by itself after 15 minutes). Slow Notion answers no longer make Discord show "This interaction failed" while the booking went through.
- A failed Notion write during a self-cancel keeps the booking in the database too, so Notion and the bot agree; the DJ is asked to try again.
- Background jobs (Notion poll, reminders, invites) keep running after an unexpected error instead of stopping until the next restart.
- A reminder that cannot be delivered is reported once, not on every hourly run, and a database error can no longer send a reminder twice.
- Invites skip DJs who already play at that event.
- Notion: a page that may already exist is never created a second time after a timeout, and one DJ can no longer get two rows in the DJs database.
- Admin booking rejects slot numbers outside the event and tells the DJ who lost the slot.
- Guest DJ ids are never reused; deleting a DJ also removes their reminders, invite history and leftover slot locks.
- Security: the database password is no longer printed when the connection fails, secrets stay out of debug output, and logging out of the admin page ends that session everywhere.
- `/list all djs` works with long DJ lists.
- A DJ now gets one invite at a time. While an invite for any event is unanswered (up to 12 hours, until they accept, decline or it times out), the bot does not invite them to another event. Before, two open events meant two invite DMs to the same DJ in the same round.
- A member with the DJ role but no DJ profile no longer goes through the whole booking flow to fail at the end. Pressing `Book a slot` (on the Lineup Update post or in the DMs) now starts the DJ onboarding first; the DJ presses `Book a slot` again when the profile is saved.

### Changed

- The bot no longer replaces its Notion databases by itself when one is missing (a lost share also looks like that). It keeps running with booking and the lineup paused, posts a note to the admin log, and the admin page offers **Create new databases** after a confirmation.
- Database: DJ names are unique in the database (migration 0016, created only when there are no duplicates yet), status values are checked, and migrations wait for each other and give up on a blocked table after 30 seconds.
- `Book a slot` under a Lineup Update post now opens the slot picker of that event directly, instead of asking for the event first. If that event is fully booked, the DJ is told so. The button in the DM menu still asks for the event.
- Event picker: an overview embed with one block per event (name, date, open slots, start time). The buttons show only the date, in the same style as Show lineup.
- Lineup Update design: the title shows the event name and date, a summary line shows how many slots are booked, and each slot is one line with a bold time (`**20:00**  🎧 DJ name` or `**21:00**  ▫️ *open slot*`).
- Show lineup in a DM: pressing another event's button now switches the same message to that event and keeps the buttons, instead of sending a new message without them. The buttons are labelled with the date and keep working after a restart.

### Added

- DJ list management for admins. The admin page has a new **Manage DJs** page to add, edit and delete DJs, with an optional logo upload. The same works with `/dj add`, `/dj edit` and `/dj remove`. Edits are synced to Notion. Deleting a DJ cancels their bookings in upcoming events after a confirmation.
- Guest DJs: a DJ added without a Discord user ID is a guest. Admins can book them with `/book dj slot guest:<name>` (and cancel with `/cancel dj slot`). The bot never contacts guests: no invites, reminders or booking notices. `/list all djs` marks them.
- `/setup`: the Events popup is split into one step per setting (slots, start time, slot length, interval, first event date, events kept ready, time zone). Each step has a dropdown with common values and an **Other** button for your own value.
- `/setup`: the first scheduled event can start on a chosen date (setting `first_event_date`), for example next Saturday and then every 14 days. Without a date the schedule starts one interval after the day of setup, as before.
- `/setup`: scheduled events can be turned off (setting `event_schedule_enabled`). The bot is then fully set up and creates no events by itself.
- `/create event` takes the optional options `slots`, `start` and `slot_minutes`, so each manual event can have its own shape. Manual events are recorded in the new table `manual_events` (migration `0015`) and are ignored by the event scheduler: they neither count toward the events kept ready nor move the schedule.
- `/config show` lists `Scheduled events` and `First event date`.

## [1.1.0] - 2026-11-07

### Added

- Release automation: the GitHub Actions workflow `release.yml` tests the code, builds the Docker image, publishes it to `ghcr.io/m4xxx/ravebot` (tags `X.Y.Z`, `X.Y`, `latest`) and creates a GitHub release from the changelog when a version tag is pushed; `ci.yml` runs the tests on every push to `main` and every pull request. `scripts/release.sh` prepares a release (version, changelog, commit, tag).
- Unraid template `unraid/ravebot.xml` with all variables, and an Unraid deployment guide that pulls the published image and updates from the Docker tab.
- New README with banner, badges, flow diagram, screenshots and a step-by-step deployment guide. The images are rendered from `docs/assets/src` with `docs/assets/render.sh`.
- Admin page: a web page (port `ADMIN_PAGE_PORT`, default `8080`, password `ADMIN_PAGE_PASSWORD`) to add and remove admins. Off while the password is empty. No HTTPS: local network only. New package `bot/web/`.
- Shared admin list: owners from `ADMIN_USER_IDS` plus admins added on the page, stored in the new table of migration `0014`. Added admins can use every admin command, including `/setup`. While the bot is active, adding an admin writes an admin log entry. A user ID has 17 to 19 digits. Page sessions end when the bot restarts.
- `aiohttp` is now declared as a dependency.
- Guided `/setup` wizard (admins only): welcome and checks, DJ channel, log channel, roles, events with a time zone button, automatic invites, summary with Activate. Run it again (`Change settings`) to edit settings; it then ends with Save. The wizard keeps its state in the new table `setup_drafts` and survives restarts.
- The bot stays idle until it is activated. While idle it sends no posts, invites, reminders or onboarding DMs, answers buttons and DMs with "I can't answer right now. Please try again later." and answers admin commands other than `/setup` and `/config show` with a hint to run `/setup`. At start an idle bot DMs every admin (`ADMIN_USER_IDS` and added admins). A server that already has a DJ channel and a log channel stored counts as active.
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

- Admin commands work on the server only, not in DMs. The key `GUILD_ID` is not read and can be removed.
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
