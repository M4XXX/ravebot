# Event announcements: design

## Intent
Admins want the bot to announce upcoming events in a Discord channel without doing it by hand. Two kinds of announcement exist:

- **General announcement**: information before the event (title, description, location, genre, lineup, notes), posted by rules the admin defines ("3 days before at 18:00").
- **Event start announcement**: posted when the event starts. It says the event is running and shows which DJ plays right now, and is edited as the slots change.

Later the announcements will also get a generated poster as an attachment. That is not part of this work; leave room for one optional image attachment in the code that builds and sends a message, nothing more.

## Decisions (from the owner)
- One optional **announcement channel**, set in `/setup` and on the admin page. Without a channel nothing is posted.
- Both types have their **own on/off switch, off by default**. Admins switch them with commands and on the admin page. They are configured on the admin page or with commands.
- **Description means the event's notes column.** No new event field.
- Each type has its **own template**; there is a default template for each. Templates are edited on the admin page.
- A template is a Discord message the admin pasted and changed: normal Discord markdown with **placeholders** for event data.
- **Rules belong to the general announcement only.** An admin can create any number of rules. Each rule has *days before the event* and *time of day*. Rules can be edited and deleted. **No rules means no general posts.** When the general announcement is switched on and has no rules, a default rule is created: 3 days before the event.
- The event start announcement has no rules: it is posted when the event starts.
- **An announcement is skipped when the lineup is not completely filled** (every slot booked, `is_fully_booked`) at the time it would be posted. This applies to both types, so an event whose lineup is incomplete at its start gets no start announcement either. Skipped means skipped: it is not retried later for that rule.
- **Mentions:** role mentions in a template work. `@everyone` and `@here` never ping (allowed mentions: roles only).

## My calls (the owner has not decided these; they are marked in the plan and easy to change)
- Time of day of the default rule: **18:00** in the bot's time zone.
- A rule posts **once per event**. If the bot was offline at the post time, the rule posts late as long as the event has not started; after the start it is dropped.
- A posted general announcement is **edited** when the event's details or lineup change before the event starts, so it stays correct. If the lineup becomes incomplete after posting, the post stays as it is (no deletion).
- Canceled events (`STATUS_CANCELED`) and finished events get no new announcement.
- Event start announcement: posted at the event's start time (as a new message), edited at every slot change to show the DJ playing now and, if there is one, the next DJ; after the last slot ends it is edited once more to say the event has ended and then never touched again. A bot restart must not post it twice.
- Times in messages are Discord timestamps (`<t:unix:F>`, `<t:unix:R>`) so each reader sees their own zone.
- If the template is invalid or too long for Discord (2000 characters after filling), the post is not sent and an admin log entry says why; the default template is the fallback only for an empty template.

## More calls, from the builder check (mine, not the owner's)
- **When is a rule due?** A rule posts for an event only when its post time lies after the moment the rule was created or the type was switched on (whichever is later), and the event has not started. Switching the feature on or adding a rule therefore never floods the channel with posts for the past. The default 3-day rule on an event only 2 days away does not post for that event.
- **Several rules:** each rule posts its own message. The post record is keyed by event and rule. Editing a rule (days or time) affects only events it has not posted for yet. Deleting a rule leaves its existing posts. A change of the event's details or lineup edits all existing posts of that event, as long as it has not started.
- **Event start announcement timing:** it is posted at the event's start time. If the bot was offline then, it still posts, until the event has ended (the end is the start of the last slot plus the slot length; slots are back to back with a fixed length). If the start time changes before the post exists, the new time counts; once posted, the post follows the new slot times. One post per event; a restart never posts a second one.
- **Channel changed:** posts already made stay where they are and are still edited there (the record keeps the channel id); new posts go to the new channel. A post deleted by hand is not reposted and not edited again.
- **Canceled or deleted event:** its existing announcements (both types) are edited to a short fixed text ("This event was canceled."), and nothing new is posted for it.
- **Invalid or too long template:** nothing is sent and the next check tries again, so fixing the template fixes the post as long as the event has not started. The admin log gets one entry per template version, not one per check.

## Placeholders
Filled from the event. An unknown placeholder stays visible as typed (so a typo is noticed) and the admin page warns about it when saving.

| Placeholder | Content |
|---|---|
| `{title}` | event name (`event_id`) |
| `{date}` | start as Discord timestamp, full (`<t:..:F>`) |
| `{countdown}` | start as relative timestamp (`<t:..:R>`) |
| `{description}`, `{notes}` | the event's notes (empty text when none) |
| `{location}`, `{genre}` | as stored (empty text when none) |
| `{lineup}` | one line per slot: slot start timestamp and DJ name, or "open slot"; names escaped like the lineup post |
| `{slots}`, `{booked}` | number of slots, number of booked slots |

Only for the event start template: `{now_playing}` (DJ name of the running slot), `{next_up}` (next DJ, empty after the last slot), `{status}` (a short live/ended line). Using them in the general template is allowed and renders the same values (before the start `{now_playing}` is empty).

A placeholder whose value is empty leaves no stray blank line (the builder decides the exact rule; the aim is that a missing genre does not leave an empty `Genre:` line when the admin wrote the label and placeholder on one line).

## Surfaces
- **Setup wizard:** a new optional step (like the DJ Management channel step) to choose the announcement channel or skip it. The summary and `/config show` list it and the two switches.
- **Commands (admins only, same checks as the other admin commands):**
  - `/announcements general on|off`, `/announcements start on|off`
  - `/announcements channel` to set or clear the channel
  - `/announcements show` to list channel, switches, rules and the template names
  - rule commands for add, edit and remove of general rules
  - templates are edited on the admin page only (long text).
- **Admin page:** a new page "Announcements": channel, both switches, the general rules (add, edit, delete: days before and time), and a template editor for each type with the placeholder list, a preview filled with the next event (or sample data) and a reset to default. The page is owner-only like the rest.
- **Security:** all of this is admin-only; the security guard tests (`tests/test_security_guard.py`) must keep passing, and new commands and routes must be covered by them.

## Constraints
- PostgreSQL is the source of truth; Notion is not involved. New settings in the `config` table or new tables with a migration; migrations need a working `downgrade`. The old-backup import and migration tests (`tests/pg/test_old_backups_and_migrations.py`) pick the new migration up automatically.
- The backup export includes new tables automatically; check that the real-SQL truncate list in `tests/pg/conftest.py` gets them.
- Posting survives restarts: what was posted for which event and rule, and the message id of each post, is stored (so edits and no double posting work).
- Follow the existing patterns (management lineup service for posting and editing messages, reminders for time based work, schedule rules for the rules page, setup wizard steps, admin page routes).
- Posting and editing never raise into other flows: failures are logged and shown in the admin log channel once.

## Non-goals
Poster generation. Per-event custom announcement text. Announcements to more than one channel. Reaction roles, RSVP buttons.

## Anti-goals
Duplicate posts after a restart. A post that pings everyone. A post with a half-filled lineup. Admin-only settings reachable by a normal user.

## Left to the builder
Table layout and names, the scheduler design (a periodic check is fine), exact wording of texts and the default templates (write sensible ones using the placeholders), command option names, the web page layout, how the live announcement tracks the running slot.
