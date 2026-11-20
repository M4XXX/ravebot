# /cancel event: design

Date: 2026-11-16

## Intent

Admins can cancel a DJ's booking (`/cancel dj slot`) but not a whole event. Today the only
way is to set the Status to `Canceled` by hand in Notion, and then nobody is told: booked DJs
keep their bookings in the bot's database and get no message.

`/cancel event` lets an admin cancel one upcoming event from Discord. The bot marks the event
as canceled, removes its bookings and tells everyone who needs to know.

## Goals (decided by the user)

- A command for admins that cancels one upcoming event. It works for scheduled events and for
  events made with `/create event`.
- A confirm step before anything happens (like `/dj remove`).
- The event gets Status `Canceled` in Notion. It is not deleted.
- The bookings of the event are removed from the Notion slot rows and from `slot_locks`.
- Every booked DJ gets a DM: the event is canceled, and so is their booking.
- A DJ who cannot be reached by DM gets an entry in the log channel saying the bot could not
  tell them and someone must tell them by hand. Guest DJs (no Discord account, negative id)
  count as not reachable and get this entry too.
- No DJ was booked: one entry in the log channel, nothing in the DJ channel.
- One or more DJs were booked: the log channel entry plus an announcement in the DJ channel
  that the event is canceled.
- The schedule keeps its existing behavior: a canceled event does not count as "kept ready",
  so the next replenish run adds one event at the end of the schedule. The canceled date
  stays empty.

## Non-goals

- No command to undo a cancel. Setting the Status back by hand in Notion brings the event
  back with empty slots; the bot does not restore bookings.
- No reaction to an event that is set to `Canceled` by hand in Notion (no DMs, no posts). Only
  the command notifies.
- No deleting of events, no editing of events.
- No change to the Notion schema and no migration.

## Anti-goals

- Nothing may be sent or changed before the admin confirms.
- A DJ must never be told the event is canceled while the event is still active: the Status
  write comes first, and if it fails nothing else happens.
- A failed DM must never be silent.
- A canceled event must never trigger outreach, a reminder or a "fully booked" entry.

## Command and confirm step

`/cancel event event:<event ID>` in `bot/cogs/admin.py`, as a sub command of the existing
`cancel` base (next to the `dj` group). Admin only and server only through the existing cog
check. The `event` option uses `future_event_autocomplete` (events dated today or later, not
Done/Canceled).

Flow:
1. Defer privately. Load the events. Unknown event, past event or an event that is already
   Done/Canceled: private error, stop.
2. Answer privately with the confirm view (two minutes, only the admin who ran the command,
   same pattern as `_ConfirmDelete`):

   ```
   Cancel the event 2026-11-28 Rave Night?
   These DJs are booked and will be told by DM:
   - DJ Alpha, slot 1 (22:00)
   - DJ Beta, slot 3 (23:00)

   [Cancel event]  [Keep event]
   ```

   With no bookings the second part reads "No DJ is booked." Guests are listed with a note
   that they have no Discord account and must be told by hand. The list is capped at 15 lines
   like `/dj remove`.
3. "Keep event" or timeout: nothing changes, the message says so.
4. "Cancel event": run the service below and replace the message with the result.

The bookings are read again when the button is clicked. The list in the confirm message is
only a preview; a booking made in between is canceled and notified too.

## Service

New `EventCancelService` in `bot/services/event_cancel.py`, attached to `BotContext` like the
other services. One public method, `cancel_event(admin_name, event_id)`, returning a result
with: success flag, message for the admin, DJs notified, DJs not reached, slots that could not
be cleaned up.

Booked DJs of the event = holders in `slot_locks` for the event's page id, plus DJs in the
event's Notion slot rows (`EventInfo.slots`) that have no lock (covers guests and hand edits
not yet synced). One entry per DJ with all their slots.

Steps, in this order:
1. Load events, find the event (case-insensitive ID match), check it is today or later and
   not Done/Canceled. Otherwise return an error.
2. Set Status to `Canceled` in Notion through a new `NotionClient.set_event_status(event, status)`
   (PATCH of the Status select, with the existing retry and error reporting). If this fails:
   return an error, change nothing else, notify nobody.
3. For every booked slot: remove the DJ from the Notion slot row (`clear_slot`) and release the
   lock (`release_slot_lock`). A failure here does not stop the run: the slot is collected for
   the result and the log entry, and the remaining slots are still handled. The lock is released
   even when the Notion write failed (same rule as `cancel_slot_override`).
4. DM every booked DJ with a Discord account, once per DJ, listing their slot or slots:
   "The event EVENT_ID was canceled by an organizer. Your booking (slot 1, 22:00) is canceled
   too." A failed DM is reported with `post_error(ERR_DM_FAILED, ...)` and
   `texts.dm_failed_todo(name)`, as the other DM paths do.
5. For every guest DJ: a log entry in the same style saying the DJ has no Discord account and
   someone must tell them about the cancel. `dm_user` returns True for guests without sending,
   so guests must be handled before it, not through it.
6. One log channel entry "Event canceled": who canceled it, the event, and the booked DJs
   (or "no DJ was booked"). If step 3 collected failures, an attention entry names the slots
   to check in Notion.
7. If one or more DJs were booked: post the announcement in the DJ channel (config key
   `dj_channel`, as `LineupService` reads it): the event ID and date, and that it is canceled.
   No DJ names in it (my call). If the DJ channel is missing or the post fails: attention entry
   in the log channel, the cancel still counts as done.
8. Call `lineup_service.notify_changed()` so the lineup post moves on to the next upcoming
   event if the canceled one was the upcoming one.

Reply to the admin: "Canceled EVENT_ID." plus counts: DJs told by DM, DJs to tell by hand
(with names), and a warning line if slots could not be cleaned up.

## Canceled events elsewhere

Checked in the code: the lineup, the booking pick lists and the scheduler already skip
`Canceled`. These do not look at the Status today and must skip Done/Canceled events:
- `OutreachService.run_gap_detection`: a canceled event has only open slots and would start
  outreach. It must be skipped, and an active outreach sequence for it must not send further
  messages.
- `RemindersService.send_reminders`: skip, so a slot whose cleanup failed gets no reminder.
- `check_fully_booked` / `recheck_fully_booked`: skip.

Use one shared definition of "finished" statuses instead of the two private copies in
`lineup.py` and `event_replenish.py` (builder's call where it lives).

## Constraints

- Follow existing code style: services return results and never raise to the cog, texts in
  `bot/ui/texts.py`, log entries through `AdminLogService`, names escaped as elsewhere,
  no em dashes (`tests/test_no_em_dash.py`), English only.
- Spelling: the Notion status is `Canceled`; user-facing texts use "canceled" for the event.
  Existing texts for bookings ("cancelled") stay as they are.
- A DJ in the middle of a booking or cancel flow for this event: the flow must not end with a
  booking in a canceled event. Verify how the booking confirm behaves and close the gap if
  there is one (builder's call how).

## Tests

- Service: no bookings (log only, no DJ channel post); one and several booked DJs (DMs, log,
  DJ channel post); DJ with two slots gets one DM; blocked DM gives the ERR_DM_FAILED entry;
  guest DJ gives the by-hand entry and no DM attempt; Status write fails (nothing else
  happens); a slot cleanup fails (others still handled, lock released, attention entry);
  DJ channel missing; event unknown, past, already Canceled or Done.
- Cog: admin check, autocomplete, confirm view (only the invoking admin, Keep, timeout,
  Cancel), preview text with and without bookings and with a guest.
- Notion client: `set_event_status` request shape.
- Outreach, reminders and fully-booked skip Canceled and Done events.
- Replenish: after a cancel one event is added at the end and the canceled date stays empty
  (add only if not already covered).

## Left to the builder

Exact texts, embed colors and titles, custom ids, the result type, where the shared
"finished statuses" constant lives, and whether `EventCancelService` reuses helpers from
`AdminBookingService` or shares them through a small module.
