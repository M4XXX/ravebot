# Booking rules: slot limit and cancellation cutoff

Date: 2026-11-22

## Intent

Two booking policies were decided on 2026-11-05 (PRD "Booking Policy", epics, Story 10.15): a DJ may hold at most 2 slots per event, and DJs cannot cancel within 60 minutes of the event start. They were built on the old `legacy/master` line and never reached `main`: nothing enforces either rule today (a DJ can book every slot of an event and cancel one minute before the start).

This work puts both rules on `main` again, and this time makes them configurable: each rule can be switched off, and its number can be changed, in the setup wizard and on the admin webpage.

## Goals

- Slot limit: a DJ may hold at most N slots of the same event. Default on, N = 2, N is any whole number from 1 up. No limit across events.
- Cancellation cutoff: a DJ cannot cancel through the bot within M minutes before the event start. Default on, M = 60, M is any whole number from 0 up (0 means cancelling is allowed until the start). A DJ who tries too late gets a message that names the cutoff and points to an admin. A cancellation that is still allowed works as before.
- Each rule has an on/off switch. Off means: no slot limit at all, or DJs may cancel at any time.
- Both rules are set in a new step of the setup wizard (first run and re-run) and on a new admin webpage page called "Booking rules". They are also listed in the settings overview that `/config show` prints.
- The rules apply immediately after saving. No restart.

## Non-goals

- No per-DJ or per-event exceptions. No separate limit values for different events.
- No retroactive action: lowering the limit does not remove or move existing bookings.
- No change to who may book (roles, passive mode, maintenance) and no change to how slots are locked beyond the limit check.
- No slash command to set the rules (the setup wizard and the admin page are the two places). The legacy command `/config set cancel-cutoff-minutes` is not brought back.

## Anti-goals (failure even if it technically works)

- Two simultaneous bookings by the same DJ in one event both succeeding although the limit is 1 or 2 (the limit must be checked inside the slot-locking transaction).
- A rule that is switched off still refusing anything.
- A DJ being offered a slot or event they would be refused afterwards (events where the DJ is at the limit must not be offered).
- A DJ-facing cancellation path that bypasses the cutoff, or an admin cancellation that is blocked by it.
- A confusing refusal text (it must name the number that applies, taken from the current setting, not a hard-coded 2 or 60).

## Decided by the user

- Re-implement both rules (slot limit and cancellation cutoff) on `main`.
- Add both to the setup wizard so they can be disabled or reconfigured there.
- Add both to the admin webpage as options to enable, disable or change the number (the slot limit number may be changed from 2 to another number).
- The admin webpage gets its own page called "Booking rules" (mockup agreed: one card per rule, a switch and a number, one Save button).
- Admins can override the slot limit when booking a DJ by hand (admin booking command and the admin webpage's booking path, if any). The limit applies to every DJ-facing path.

## Calls made without the user (the design's own)

- Defaults when nothing is stored: both rules on, N = 2, M = 60. An existing install therefore starts enforcing both rules after the update, which restores the documented policy.
- Existing bookings above a lowered limit stay. Only new bookings are refused. A DJ above the limit can still cancel (subject to the cutoff).
- Hand edits that book a DJ (Notion hand edits, applied by the sync intake) count as admin overrides: the limit does not refuse them.
- The cutoff applies to DJ-facing cancellation only. Admin cancellations, event cancellations by an admin, and Notion hand edits are never blocked.
- Setup wizard shape: one new step "Booking rules" before the summary, with two toggle buttons (on/off per rule) and two dropdowns (number of slots, minutes) with an "Other" popup for any value, like the existing numeric steps. The summary page and `/config show` list both rules.
- Stored in the existing config table with four keys: `slot_limit_enabled`, `slot_limit_max`, `cancel_cutoff_enabled`, `cancel_cutoff_minutes`.
- What counts toward the limit: only slots the DJ holds (the slot's `discord_user_id` in `slot_locks`). Pending outreach invites, shared or extra DJs listed on a slot, and slots in other events do not count. An invite is not a booking, so a DJ with several open invites can accept them until the limit is reached.
- There are no swap, move, transfer or waitlist flows in the bot today, so the paths listed under "Constraints" are the complete set. A path added later must apply the rules.
- The cutoff is measured from the event start (as in the original decision), not from the DJ's own slot start.
- Rule changes take effect at the next action: open pickers and confirm views are not invalidated, because both rules are checked again when the DJ confirms.
- Limits on the numbers: slot limit 1 to the maximum slots per event (`MAX_SLOTS`, 100); cutoff 0 to 10080 (one week) minutes.

## Constraints from the codebase

- Slot claiming: `bot/db/bookings.py` `acquire_slot_lock` (SELECT ... FOR UPDATE of the slot row inside a transaction) is used by the DJ confirm button (`bot/ui/views.py`, around line 1250) and the Notion intake (`bot/sync/intake.py`, around line 363). Admin override booking uses `force_slot_holder` through `bot/services/admin_booking.py` `book_slot_override`. The legacy implementation counted the DJ's slots inside the same transaction as the claim; do the same so two parallel bookings cannot both pass.
- Booking paths to cover (DJ-facing, limit applies): the DJ booking buttons and slot picker, the event list (events where the DJ is at the limit are not offered), outreach/invite accept and gap-fill (`bot/services/outreach.py`). Admin paths (limit does not apply): `/admin book` and any admin webpage booking, `book_slot_override`, Notion intake.
- Cancellation paths: the DJ cancellation view (`CancellationView` in `bot/ui/views.py`, around line 1316) is DJ-facing and gets the cutoff, checked when the DJ picks the booking and again on confirm. Admin cancellation (`cancel_slot_override`, event cancellation, DJ deletion) is exempt.
- Setup wizard: `bot/ui/setup_wizard.py` (step list `STEPS`, `render_step`, `_layout` for persistent control ids, `prefilled_data`, `DEFAULT_VALUES`, `_write_all`, draft migration marker mechanism, summary). The channel step added on 2026-11-22 (`management_channel`) sits right before `summary`; the new step goes after it. Existing in-flight drafts must keep working (see `migrate_legacy_draft`).
- Admin webpage: `bot/web/app.py` (routes, CSRF via `_csrf_field`, form helpers), `bot/web/pages.py` (inline-CSS server-rendered HTML, no JavaScript), `bot/web/service.py` (`ActionResult`). Follow the schedule page (`/schedule`, `bot/web/schedule_page.py`, `post_schedule_defaults`) for form handling, validation messages and the dashboard link buttons.
- Settings overview: `bot/cogs/admin.py` `/config show` lists the stored settings.
- Settings storage: `bot/db/config.py` (`get_value`, `set_value`, `delete_value`).
- Texts: `bot/ui/texts.py`; no em dash (U+2014) in user-facing strings under `bot/` (`tests/test_no_em_dash.py`).
- Tests: existing tests that book or cancel with fixed past dates and mocked databases must keep working; legacy used a `real_policy` marker so that policy checks stay neutral in older tests. Choose an equivalent approach that does not weaken existing tests.
- Docs: user guide, developer guide, CHANGELOG, and the PRD "Booking Policy" section (`_bmad-output/planning-artifacts/prd.md`, outside the repo: leave it alone and mention in the hand-over that its text says the limit is fixed).

## Behavior

### Slot limit

- Enabled and N = the stored number. On a DJ-facing booking the bot counts the DJ's slots in that event, inside the slot-claim transaction, excluding the slot being claimed. At or above N the booking is refused and nothing changes.
- Refusal text names N, tells the DJ they hold that many slots in this event, and suggests cancelling one or choosing another event.
- The event list and slot picker do not offer events where the DJ is already at the limit. If the DJ is at the limit in every event that still has open slots, they get a message saying so (not an empty list).
- Outreach and gap-fill invitations skip a DJ who is at the limit for the event the invite is about, and an invite accepted after the DJ reached the limit is refused with the same text.

### Cancellation cutoff

- Enabled and M = the stored minutes. A DJ-facing cancellation is refused when now is at or after (event start minus M minutes). The check runs when the DJ chooses the booking to cancel and again on confirm, so a slow DJ cannot slip through.
- Refusal text names the cutoff in plain words ("1 hour", "90 minutes", "the start of the event" for 0) and tells the DJ to contact an admin.
- An event whose start cannot be determined stays cancellable.

### Setup wizard

- New step "Booking rules" before the summary: shows both rules with their current value, two toggle buttons, two dropdowns with common values and an "Other" popup. The summary page lists both rules. A re-run starts from the stored values; a first run starts from the defaults.
- Numbers outside the allowed range are rejected with a message, like the other numeric steps.

### Admin webpage

- New page "Booking rules" (link on the dashboard like Schedule, also reachable from the other pages' navigation if they have one): one card per rule with a checkbox and a number field, one "Save booking rules" button, CSRF-protected like the other forms. Invalid numbers show an error and keep the form values. A saved change shows a confirmation. Only admins reach it (same session check as the other pages).
- The page shows the current stored values (or the defaults when nothing is stored).

### Where the rules show up

- `/config show` lists both rules, e.g. `Slot limit: on, 2 per event` and `Cancellation cutoff: on, 60 minutes before the start`, or `off`.

## Left to the builder

- Module layout (for example a `bot/policy.py` for the readers and texts), exact wording, and function names, as long as the interfaces are covered by tests.
- How the limit count query is written (it must run in the same transaction as the claim).
- How the discrete option lists in the wizard dropdowns look (follow `bot/ui/setup_options.py`).
- Test approach for keeping older tests neutral.
- Docs wording.
