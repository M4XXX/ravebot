# DJ Management lineup message

Date: 2026-11-22

## Intent

DJ Management staff currently keep a list of events and slots and look up each
DJ's streaming link in a second list, then copy it into the video player while
that DJ plays. A third person builds the event poster from the lineup. The DJ
registration data (names, VRChat name, streaming links, logo) is already in the
bot's database, so the bot can hand staff everything in one place.

When an event becomes fully booked, the bot posts a lineup message in a
dedicated DJ Management channel. The message is kept current as the lineup
changes. A button lets an admin receive the full details by DM.

## Goals

- Optional setup step to choose the DJ Management channel. Skipping it disables the feature; nothing else changes.
- One message per event in that channel, posted when the event becomes fully booked.
- The message shows the lineup with, under each DJ, the streaming link and the alternative streaming link, so staff can copy them.
- The message stays current: it is edited, not reposted, when the lineup or a DJ's data changes.
- A "Send me more information" button DMs the clicking admin the full lineup details.
- Only admins can get data from the button.

## Non-goals

- No changes to how events are booked or when an event counts as fully booked.
- No poster generation.
- No new admin concept: no extra role or permission setting.
- No replacement for the existing admin log entry "fully booked"; it stays as is.

## Anti-goals (failure even if it technically works)

- A non-admin receiving DJ data (names, links, logos) through the button or any other path.
- Duplicate lineup messages for one event (restart, double trigger, refill).
- A stale message that shows a DJ who is no longer booked, or old links.
- Streaming links in a form staff cannot copy cleanly (see Message).

## Decided by the user

- Trigger: an event is ready and fully booked.
- Message content: event, lineup, and under each DJ the streaming link and alternative streaming link, copied from the DJ registration data.
- The poster maker needs the same information.
- Button "Send me more information": the bot sends the clicker, in DMs, the complete lineup: DJ name, Discord name, VRChat name, streaming links and logo per DJ.
- The message is edited to stay current.
- Button access: admins only, using the bot's existing admin check (owners from the environment plus admins added on the web page; `AdminRegistry.is_admin`). No normal user receives the information.

## Constraints from the codebase

- Fully booked detection exists: `bot/services/fully_booked.py` (`is_fully_booked`, `check_fully_booked`, `recheck_fully_booked`) with the once-per-fill flag in table `fully_booked_events` (`bot/db/fully_booked.py`). It is called from the poll cycle and after every booking or cancellation. Finished and canceled events are skipped. The new feature hooks into this flow.
- Setup wizard: `bot/ui/setup_wizard.py`. Steps are listed in `STEPS`; a new step needs a key before `"summary"`, a branch in `render_step`, and persisted config. Channel steps already exist (see `_channel_check`, `_choose_channel`) and check the bot's permissions in the channel.
- Admin check: `AdminRegistry.is_admin(user_id)`.
- Idle and maintenance gates: DJ-facing views use `GatedView` / `refuse_in_maintenance` in `bot/ui/gates.py`. The new button must respect them.
- Data: table `djs` has `vrchat_name`, `logo_url`, `stream_url_primary`, `stream_url_backup`, `logo_image` (processed logo bytes). Events carry genre, location, notes, date, start, slot length (`EventInfo`).
- Persistent buttons must survive restarts (the wizard registers persistent views with `bot.add_view`).
- Schema changes go through Alembic migrations in `migrations/versions/`.

## Behavior

### Setup

- New optional wizard step "DJ Management channel", same style as the existing channel steps, with a clear way to skip or clear it. The summary page lists the choice.
- The channel value is stored with the other setup config.
- If the channel is later unset or becomes unusable (deleted, no permission), the feature quietly does nothing for new events and logs a problem to the admin log. It never blocks booking.

### Existing fully booked events (decided by the user)

- When an admin sets or changes the DJ Management channel in setup, and upcoming events are already fully booked without a lineup message, the bot asks the admin whether to post a message for all of them or only for events that become fully booked from now on.
- "All": one message per such event (upcoming only; finished and canceled events are skipped), posted once, in event date order.
- "Only new": those events never get a message unless they open up and fill again.
- The question is part of the setup flow (buttons), shown right after the channel choice. If no such events exist, it is not asked. The answer is not stored as a setting; it only triggers or skips the one-time backfill.

### Posting

- When an event becomes fully booked (same moment the existing fully-booked announcement fires), the bot posts the lineup message in the DJ Management channel and stores the message id per event in a new table.
- At most one message per event. A second trigger for the same event edits, never reposts.

### Message content

- Title: event name/id and date.
- Event details: start time and slot length (local times as elsewhere in the bot), genre, location, notes, when set.
- Lineup in slot order: slot number and time, DJ name, then the streaming link and the alternative streaming link, each on its own line.
- Links are shown as plain text (not hidden behind markdown link labels), so they copy cleanly. Missing link: shown as "none". Do not embed-preview them if that makes copying harder (builder's call).
- Message size: events can exceed one Discord message or embed limit. The builder splits across several embeds or messages in a way that keeps one editable unit per event. The builder decides how.
- Buttons: "Send me more information".

### Staying current

- The message is edited whenever the event's lineup changes (booking, cancellation, admin booking, swap) or a booked DJ's name, VRChat name or streaming links change, and when event details (time, genre, location, notes) change.
- If the event stops being fully booked, the message stays and is edited to show the open slots plus a note "no longer fully booked". When the slots are filled again, the same message returns to the normal view and is not reposted. (The user's choice was to edit and keep the message current; the cancellation display is the design's own call.)
- Event canceled, or finished: the message is edited to say so, or left as is; builder's call. It is never deleted by the bot.
- If the stored message was deleted by hand, the next trigger posts a new one and stores its id.

### "Send me more information" button

- The button check runs at click time, not at posting time. `AdminRegistry.is_admin(clicker)` decides.
- Non-admin: private (ephemeral) reply that they have no permission. No data is sent.
- Admin: an ephemeral confirmation, then DMs to the clicker with the complete current lineup. Per DJ: slot and time, DJ name, Discord name, VRChat name, streaming link, alternative streaming link, and the logo as an attached image (from `logo_image`; fall back to `logo_url` or "no logo").
- Data is read from the database at click time, so it is current even if the message was not yet refreshed.
- If DMs are closed, the admin gets a private note saying so.
- The button respects the idle and maintenance gates.
- Works after a restart (persistent button, tied to the event by the message).

### Privacy

- Discord names, VRChat names and logos appear only in the admin DMs, not in the channel message. The channel message has DJ names and streaming links only. The channel itself should be a staff-only channel; the setup step says so.

## Left to the builder

- Table design and migration for message ids.
- Embed layout, how long lineups are split, and exact wording (follow existing texts and embed styles in `bot/ui/texts.py` and `bot/ui/embeds.py`).
- How edits are triggered (hook into the existing recheck points versus a periodic refresh), as long as the message is current soon after a change.
- Rate-limit handling for edits and DMs.
- Tests, following the existing test layout in `tests/`.
- Docs: update the user guide, developer guide and changelog as the project does for other features.

## Calls made without the user (marked as the design's own)

- Long lineups: one logical lineup may span several Discord messages. The table stores the channel id and a list of message ids per event; edit, repost-if-deleted and "at most one per event" work per chunk. The button sits on the first message only.
- Button lookup: the button's custom_id embeds the event key, so it resolves the event after a restart without a message-id lookup.
- Message ids survive the fully-booked flag being cleared. A refill edits the stored message(s); it never posts a second lineup.
- Channel change: existing messages keep being edited in the channel stored with them. Only events without a stored message post to the currently configured channel. No automatic move or repost.
- Profile edits: DJ profile edit paths trigger a refresh of the lineup messages of that DJ's upcoming events, in addition to the existing recheck points and the poll cycle. Edits are queued per event to stay within Discord rate limits.
- DMs: one header DM, then one DM per DJ (logo attached to that DM). A failed DM stops the run and tells the admin privately how far it got.
- The message is only created for events that have been fully booked at least once.

- Cancellation display: edit to show open slots with a note.
- Discord names and VRChat names are in the DMs only, not in the channel message.
- Logo sent as an attached image in the DM.
- Unusable channel: quiet no-op plus an admin log problem entry.
