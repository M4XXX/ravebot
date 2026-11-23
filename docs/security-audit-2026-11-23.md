# Security audit: admin-only entry points and data leaks (2026-11-23)

Scope: everything a non-admin can reach from outside: slash commands, buttons and popups, the admin page, and what the bot posts or sends.

## Findings

| Area | Result |
|---|---|
| Slash commands | All 9 command groups sit in cogs with their own `cog_slash_command_check` (admin list, idle gate, server-only). `/admins` additionally requires an owner. All commands are limited to servers. No user or message commands exist. |
| Admin page | One session middleware covers every route except `/login`; unsafe methods need a CSRF token. Every registered route was checked without a login: all redirect to `/login`. |
| Admin buttons and popups | The setup wizard, DJ Management "Send me more information", the log "Reply" button, the schedule apply view and the delete/cancel confirmations check `is_admin` (or the admin who started them). |
| `ReplyModal` (log reply popup) | **Fixed.** Its submit did not check the admin list; it only relied on the popup being shown to admins. It now refuses non-admins. |
| DJ-facing views | They act on `inter.author` only or are bound to one user by `GatedView`; none looks up another person's data from a button id. |
| Public output | The public lineup shows slot, time and DJ name only. Names are escaped so a mention in a name does not ping. Discord ids, VRChat names and the other DJ table columns are not posted anywhere public. Stream links appear only in the DJ Management message (admin channel). |
| Answers of admin commands | All are ephemeral, except the setup wizard message and the "already active" entry message, which are posted in the channel on purpose (a private wizard message could not be edited after 15 minutes or after a restart). They show setup values such as channels, time zone and booking rules, no personal data. |

## Open points (not changed)

- The setup wizard message is visible to everyone in the channel where `/setup` ran. Run `/setup` in an admin-only channel.
- The DJ table in Notion is outside the bot: restrict who can open it there.

## Guard tests

`tests/test_security_guard.py` fails when:
- a cog with slash commands has no admin check, a non-admin passes a cog check, `/admins` accepts a non-owner, or a command can run in DMs;
- an admin page route works without a login, or a session without CSRF can post;
- a new view or popup is not classified as admin or user view, or an admin view never calls `is_admin`;
- an admin command code path answers publicly (outside the listed exceptions);
- the public lineup contains a Discord id or an unescaped mention.
