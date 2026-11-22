# Owner-only admin management commands

Date: 2026-11-22

## Intent

Admins are today managed in two ways: owners (the Discord user ids in the `ADMIN_USER_IDS` environment variable) and admins added at runtime (stored in the database, `AdminRegistry`). Adding and removing runtime admins is only possible on the admin webpage, which is protected by one shared password (`ADMIN_PAGE_PASSWORD`): whoever holds it is the operator, and other admins cannot reach it.

The owner wants to do the same from Discord: slash commands to add, remove and list admins, usable by owners only. A normal admin (and of course a normal user) must not be able to use them.

## Goals

- A command group `/admins` with three sub commands: `add`, `remove`, `list`.
- Only owners (`AdminRegistry.is_owner`) may use any of them. The check is enforced on the server side for every invocation, not only by hiding the command.
- `add`: takes a Discord user (the usual user picker), makes them an admin. Refused when they are already an admin or owner.
- `remove`: takes a Discord user, removes them from the added admins. An owner can never be removed (owners change only through `ADMIN_USER_IDS`). A user who is not an added admin gets a clear message.
- `list`: shows the owners and the added admins (display name and id), marking owners.
- All answers are visible only to the caller (ephemeral).
- Every successful add and remove is posted to the admin log channel (additions already have a log entry on the webpage path; removals get a new one), whichever way the change was made (command or webpage).
- Changes take effect immediately (the registry cache is updated), so a removed admin loses access to every admin command, button and the like at once.

## Non-goals

- No change to who can log in to the admin webpage (shared password) and no per-user web accounts.
- No command to add or remove owners (the environment variable stays the only way).
- No "demote yourself" or "leave admin list" command.
- No notification (DM) to the added or removed person.
- The wider security audit (every command, button, route, data exposure) is a separate project (B).

## Anti-goals (failure even if it technically works)

- A non-owner (admin or not) adding or removing an admin by any path: a command, an autocomplete, or a crafted interaction.
- An owner being removable, directly or indirectly.
- The last owner path disappearing: nothing in these commands can remove an owner, so the bot can never be left without an owner.
- Admin names or ids shown to anyone but the calling owner (answers are ephemeral, the log channel is staff only).
- The in-memory admin list and the database drifting apart (a failed database write must not leave the admin active in memory, and the user must be told).

## Decided by the user

- Owner-only commands to add and remove admins (marked as owner on the admin webpage = environment variable owners).
- Only the owner may use them. The admin webpage stays as it is (it is already reachable only with the shared password).

## Calls made without the user (the design's own)

- Command shape: group `/admins` with `add`, `remove`, `list`, server only (not usable in DMs), like the other admin groups.
- `list` is owner-only as well (it shows names and ids).
- Non-owner answers: a user who is not an admin gets the same generic "You don't have permission to use this command." as for every admin command. An admin who is not an owner gets "Only the bot owners can manage the admin list." (they already know admin commands exist).
- The commands follow the idle rule of the other admin commands (the bot must be set up and active), except that nothing else changes: a bot that is not set up yet has no admin commands.
- Removal log entry: a new admin log entry "Admin removed" mirroring "Admin added" (who removed whom).
- The webpage's removal now also writes that log entry; its additions already log.

- A failing log post never fails or undoes an add or remove (the log call is best effort, like the existing `_log_added`); the database and the in-memory list are changed first.
- Log entries name who did it: the command path passes the calling owner's name, the webpage path passes "the admin page" (it has no Discord identity). Both the "added" and the "removed" entry take this as an optional `actor`; without one the text stays as it is today.
- Names: the commands defer the reply first (`defer(ephemeral=True)`), then look names up the way `AdminPageService.describe` does (cached user first, then a Discord lookup, the id when unknown). `list` is the only place where many lookups happen.
- `/admins` is not idle-exempt: like the other admin commands it needs a set up and active bot (an owner who must fix admin access while the bot is idle uses the admin webpage).
- Owner check failures use their own exception and are answered by the new cog's error handler, so the shared `AdminCog` error handling does not change.
- Bot accounts cannot be added as admins ("Bots cannot be admins."). Removing yourself cannot happen because only owners may call the command and owners are never in the removable list.
- `list` output is split into messages of at most 1900 characters.

## Constraints from the codebase

- `bot/services/admin_registry.py`: `AdminRegistry` (`is_owner`, `is_admin`, `added`, `add`, `remove`, `ensure_loaded`, owners from the environment, added admins from `web_admins`). `remove` refuses owners.
- `bot/web/service.py` `AdminPageService.add_admin` / `remove_admin` hold the current rules and texts (already admin, no such user, owner cannot be removed, not an added admin, save failure) and call `log_service.log_admin_added`. Reuse this behavior rather than writing a second copy: extract the shared part into one service both the command and the webpage use (builder's choice of module).
- `bot/log.py` `AdminLogService.log_admin_added` and `texts.log_admin_added_text` are the model for the new removal entry; use `_safe_dj_name`-style escaping for names.
- `bot/cogs/admin.py` `AdminCog` has the admin check (`cog_slash_command_check`, `cog_slash_command_error`, `IDLE_EXEMPT_COMMANDS`); `bot/cogs/schedule_cog.py` shows how another cog reuses it. The owner check is an additional check on top of the admin check.
- `BotContext.is_admin` / `admins`; owners via `ctx.admins.owners`.
- Texts in `bot/ui/texts.py`, no em dash (U+2014) in user-facing strings under `bot/`.
- Tests under `tests/cogs`, `tests/services`, `tests/web`.

## Behavior

- `/admins add user:<User>`: refuse unless the caller is an owner. Then: user already owner or admin gives "already an admin"; otherwise the user is stored (database first, then the in-memory list), the caller gets "Added <name>." and the admin log gets the "Admin added" entry. A database failure gives a fixed failure text and changes nothing in memory.
- `/admins remove user:<User>`: refuse unless the caller is an owner. Owner target: refused with the "owners can only be changed with ADMIN_USER_IDS" text. Not an added admin: "not an added admin". Otherwise removed (database and memory), the caller gets "Removed <name>." and the admin log gets "Admin removed". A database failure leaves the person an admin (nothing changes in memory) and says so.
- `/admins list`: owners first (marked "owner"), then added admins, name and id per line, split over several lines or messages if long.
- Names come from Discord (display name when known); an unknown id shows as the id.
- The webpage's add and remove keep working as today and now share the same code path, so both write the same log entries.

## Left to the builder

- Module layout (a new cog file, a shared service), command and text wording, option names and descriptions.
- How the owner check is expressed (extra cog check, decorator or an explicit call at the start of each command), as long as every sub command is covered by tests that call it as a non-owner admin and as a normal user.
- Test approach and docs (user guide section on admins, developer guide, CHANGELOG).
