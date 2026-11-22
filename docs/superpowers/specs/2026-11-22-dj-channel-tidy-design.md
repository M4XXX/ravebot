# DJ channel tidy-up: replace the old lineup post

Date: 2026-11-22

## Intent

The bot posts a lineup update into the DJ channel whenever the upcoming event changes, and an admin can post one by hand (`/post lineup update`). When the bot posts a new lineup post for an event and the previous lineup post of that same event is still the last message in the channel, the old post is just noise: the new one replaces it. The user wants the bot to delete its own old post in exactly that case.

## Goals

- When the bot posts a lineup update for event E into the DJ channel, and the last message in the channel is the bot's own previous lineup post for the same event E, delete that old post after the new one was sent.
- Applies to the automatic lineup posts and to the by-hand post (`/post lineup update`): both go through `LineupService._send`.
- Never delete anything else: only a message the bot itself recorded as its lineup post, only when it is still the last message of the channel, only for the same event.
- A failed send never costs the old post (delete only after the new post succeeded).
- Works across restarts.

## Non-goals

- No deletion of lineup posts of other events, of other bot messages, or of anything a person wrote.
- No change to the DJ Management channel (its lineup messages are edited in place and never deleted).
- No cleanup of older history (only the immediately preceding post is considered).
- No new permission requirement: a bot can delete its own messages without any extra permission and needs no "Read Message History".

## Anti-goals (failure even if it technically works)

- Deleting a message of a person, a different bot message or a lineup post of another event.
- Deleting the old post when someone wrote in the channel after it (it is then no longer the last message).
- Losing both posts (deleting before the new post is confirmed sent).
- A deletion failure (message already gone, no permission) breaking the posting or raising into callers.

## Decided by the user

- The bot may delete its old lineup post when it posts a new lineup post and the last message in the channel is its own lineup post for the exact same event.
- Only its own lineup post, only for the same event.
- Also for posts made by hand by an admin.

## Calls made without the user (the design's own)

- "Last message in the channel" is decided with the channel's `last_message_id` (kept up to date by Discord's gateway events), compared with the id of the post the bot stored. It needs no history permission and no API call. If `last_message_id` is unknown, nothing is deleted.
- The bot stores one record per DJ channel: the channel id, the event key and the message id of its latest lineup post (new table, one row per channel, replaced on every lineup post). After a restart the record is still there and the channel's last message id comes from Discord, so the rule keeps working.
- The check happens before sending the new post (the old post is the last message only before our new post exists); the delete happens after the send succeeded.
- A lineup post that is the last message but belongs to another event is left alone and the record is replaced by the new post.
- If the new post fails to send, the record is not changed.

- The cache-only "last message" check is the accepted approach. After a reconnect Discord sends the current last message id with the channel; the only window where it can lag is messages written while the bot was disconnected before it reconnected. Likewise a person writing in the few milliseconds between the check and the send is accepted (the check is done as late as possible, right before the send). Both limits are documented in the developer guide.
- Concurrency: `refresh` and `post_event` already call `_send` under the service's `asyncio.Lock`, so lineup posts are serialized and no extra lock is added.
- The event identity is `EventInfo.page_id` (the stable event key; the footer shows the display name `event_id` only for people).
- Rows of a DJ channel that is no longer configured stay in the table (one small row per channel, no cleanup). A message deleted by someone else leaves a stale record that simply never matches the channel's last message id again.
- The by-hand command's replies do not change.

## Constraints from the codebase

- `bot/services/lineup.py` `LineupService._send(guild_id, event, report_error)` is the single place that posts a lineup embed to the DJ channel (config key `dj_channel`); `refresh`, `post_event` and the debounced refresh all use it. The by-hand command calls `lineup_service.post_event`.
- `bot/ui/embeds.py` `LineupUpdateEmbed` puts the event id in the footer; the stored record uses the event key (`EventInfo.page_id`).
- Persistence: Postgres via Alembic migration (next revision `0021`, `down_revision = "0020"`) and a small repository module under `bot/db/` (follow `bot/db/management_lineup.py`).
- Tests: `tests/services/test_lineup.py` for the service, `tests/db` for the repository, a migration test like `tests/test_migration_0020.py`.
- Texts and docs: no em dash (U+2014) in user-facing strings; update the user guide (section 7.4 lineup posts), developer guide and CHANGELOG.

## Behavior

1. `_send` resolves the DJ channel as today.
2. It loads the stored record of that channel. It decides `replace_old = record exists and record.event_id == event.page_id and channel.last_message_id == record.message_id`.
3. It sends the new post (embed and view as today). On failure: return as today, record unchanged.
4. On success it stores the new record (channel, event key, new message id).
5. If `replace_old`, it deletes the old message with `channel.get_partial_message(record.message_id).delete()`; `NotFound` and other errors are logged at debug/warning level and ignored; it never raises.

## Left to the builder

- Table and module names, the exact repository functions, and test fakes.
- Whether the delete runs inline or as a small awaited step after the record is stored (it must not delay or fail the post).
- Docs wording.
