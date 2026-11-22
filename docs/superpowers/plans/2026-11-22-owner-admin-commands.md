# Owner-only Admin Management Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Owner-only `/admins add`, `/admins remove` and `/admins list` slash commands, sharing one service with the admin webpage, with admin log entries for both additions and removals.

**Architecture:** The add/remove rules and texts that live in `AdminPageService` move into a new `AdminManagementService` (`bot/services/admin_management.py`) that both the webpage and a new `AdminsCog` call. The cog has its own owner check (on top of the existing admin gate) and its own error handler, so the shared `AdminCog` error handling does not change. `AdminLogService` gets an `actor` argument and a new "Admin removed" entry.

**Tech Stack:** Python, disnake, asyncpg, aiohttp (existing web service), pytest (asyncio, mocks), ruff.

**Spec:** `docs/superpowers/specs/2026-11-22-owner-admin-commands-design.md`

## Global Constraints

- Only owners (`AdminRegistry.is_owner`, from `ADMIN_USER_IDS`) may use any `/admins` sub command, enforced server side on every invocation. A user who is not an admin gets the generic "You don't have permission to use this command."; an admin who is not an owner gets "Only the bot owners can manage the admin list."
- Owners can never be added to or removed from the runtime list; `AdminRegistry.remove` already refuses owners and must stay that way. Bot accounts cannot be added ("Bots cannot be admins.").
- All answers are ephemeral (caller only). The commands are server only (`contexts=SERVER_ONLY`) and are not idle-exempt (same idle rule as the other admin commands).
- The database is written before memory (the registry does this); a failed save changes nothing in memory and the caller is told with a fixed text. A failing log post never fails or undoes an add or remove.
- Log entries name who did it: the command path passes the owner's display name, the webpage path passes "the admin page"; without an actor the existing text stays as it is today.
- The admin webpage keeps working as today (same routes, same messages).
- No em dash (U+2014) in user-facing strings under `bot/` (`tests/test_no_em_dash.py`).
- Commits authored as M4XXX <44500247+M4XXX@users.noreply.github.com>, no AI attribution or co-author lines. Check staged files for secrets before each commit.
- Verification from `output/ravebot`: `uv run pytest -q`, `uv run ruff check .`.

## Review Focus

- A non-owner admin calling each of the three sub commands (including by a crafted interaction through the cog's check): refused, no registry or database change (Task 2 tests).
- A normal user (not an admin) calling them: the generic denial, nothing revealed (Task 2 tests).
- Removing an owner, removing yourself as an owner, adding an owner or an existing admin, adding a bot: all refused with clear texts (Tasks 1 and 2).
- A database failure on add or remove: nothing changes in memory and the caller is told (Task 1 test).
- A failing log post: the change still stands and the caller still gets the success text (Task 1 test).
- `list` with many admins: split into messages of at most 1900 characters, never over Discord's limit (Task 2 test).
- Webpage add/remove behave exactly as before and now also log a removal entry (Task 1 tests).

---

## File Structure

- Create `bot/services/admin_management.py`: shared add/remove/describe logic and texts.
- Modify `bot/web/service.py`: `add_admin` / `remove_admin` delegate to the shared service.
- Modify `bot/log.py`, `bot/ui/texts.py`: `actor` argument, `log_admin_removed`, texts.
- Create `bot/cogs/admins_cog.py`: the `/admins` group, the owner check, the error handler.
- Modify `bot/main.py`: build the shared service, register the cog.
- Modify docs: `docs/user-guide.md`, `docs/developer-guide.md`, `CHANGELOG.md`.
- Tests: `tests/services/test_admin_management.py`, `tests/cogs/test_admins_cog.py`, additions to `tests/web/test_service.py` and `tests/test_log.py`.

---

### Task 1: Shared service, log entries, webpage delegation

**Files:**
- Create: `bot/services/admin_management.py`
- Modify: `bot/web/service.py` (`add_admin`, `remove_admin`, `_log_added`), `bot/log.py`, `bot/ui/texts.py`
- Test: `tests/services/test_admin_management.py`, `tests/web/test_service.py`, `tests/test_log.py`

**Interfaces:**
- Produces in `bot/services/admin_management.py`:
  - `@dataclass(frozen=True) class AdminChange: ok: bool; message: str`
  - `class AdminManagementService` with `__init__(self, ctx: BotContext, bot: Any)` (needs `ctx.admins`; raises RuntimeError without it like `AdminPageService`), and:
    - `async add(self, user_id: int, actor: str | None) -> AdminChange` (already admin or owner: "This person is already an admin."; Discord lookup `bot.fetch_user`: NotFound "No Discord user with this ID.", other errors "Could not reach Discord. Try again."; a bot account: "Bots cannot be admins."; `registry.add` failure: "Could not save the change. Try again." with nothing changed in memory; success: `AdminChange(True, "Added <name>.")` and one `log_admin_added(name, user_id, actor)` call, best effort and only while the bot is active as `_log_added` does today)
    - `async remove(self, user_id: int, actor: str | None) -> AdminChange` (owner: "Owners can only be changed with ADMIN_USER_IDS."; not an added admin: "This person is not an added admin."; save failure as above; success: "Removed <name>." and one `log_admin_removed(name, user_id, actor)` call, best effort)
    - `async list_admins(self) -> list[tuple[int, str | None, bool]]` (user id, display name or None, is_owner; owners first then added, in registry order)
    - `async display_name(self, user_id: int) -> str | None` (cached member or user, then `fetch_user`, None when unknown; never raises)
  - `AdminLogService.log_admin_added(name: str, user_id: int, actor: str | None = None)` and new `log_admin_removed(name: str, user_id: int, actor: str | None = None)`; texts `LOG_ADMIN_REMOVED_TITLE = "Admin removed"`, `log_admin_added_text(name, user_id, actor=None)` (unchanged text without actor, with actor: "<name> (ID n) was added as an admin by <actor>.") and `log_admin_removed_text(name, user_id, actor=None)` ("<name> (ID n) was removed as an admin." / "... by <actor>."). Names and actor go through `_safe_dj_name`-style escaping like the other entries.
- `AdminPageService.add_admin(raw_id)` / `remove_admin(raw_id)` keep their signatures and results (`ActionResult`), parse the id as today (`FORMAT_MESSAGE` on a bad id) and delegate to the shared service with `actor="the admin page"`. The existing messages must stay identical (webpage tests keep passing).

- [ ] **Step 1: Write failing tests:** service tests with fake registry (use `AdminRegistry` with a fake db like `tests/services/test_admin_registry.py`) and a fake bot: add success logs once with the actor; add of an existing admin, an owner, a bot account, an unknown user, a Discord error each return the exact text and log nothing; a failing `registry.add` leaves `registry.is_admin(uid)` False and returns the save-failed text; a raising `log_admin_added` still returns success; remove of an owner, a non-admin, a failing database (still an admin afterwards) and success (logged with actor, no longer admin); `list_admins` order and owner flag; `display_name` falls back to None; log text tests for with/without actor and the removed text; `tests/test_log.py` entry tests for both entries (title, color, escaping of `@everyone` and markdown in names). Web tests: `add_admin` and `remove_admin` still return the same messages, and a successful webpage removal now writes one "Admin removed" log entry with actor "the admin page".
- [ ] **Step 2: Run** `uv run pytest tests/services/test_admin_management.py tests/test_log.py tests/web/test_service.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the service, log methods and texts; make the two `AdminPageService` methods delegate; remove the duplicated logic and `_log_added` from the web service.
- [ ] **Step 4: Run** the same tests plus `uv run pytest tests/web tests/services -q`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: shared admin management service with removal log entry`).

---

### Task 2: The `/admins` commands

**Files:**
- Create: `bot/cogs/admins_cog.py`
- Modify: `bot/main.py` (build `AdminManagementService(ctx, bot)` where the admin page service gets its dependencies, keep it on `ctx` or pass it to the cog; register the cog with `_add_cog`)
- Test: `tests/cogs/test_admins_cog.py`

**Interfaces:**
- Consumes: `AdminManagementService`, `AdminChange` from Task 1; `AdminCog.cog_slash_command_check` and the idle rule from `bot/cogs/admin.py`; `SERVER_ONLY` from `bot/cogs`.
- Produces: `class NotOwner(commands.CheckFailure)`; `class AdminsCog(commands.Cog)` with `__init__(self, ctx: BotContext, service: AdminManagementService)`; `cog_slash_command_check(inter)` that first runs the same admin and idle check as `AdminCog` (reuse `AdminCog.cog_slash_command_check`) and then raises `NotOwner` when `not ctx.admins.is_owner(inter.author.id)`; `cog_slash_command_error(inter, error)` answering a `CheckFailure` from the admin gate with "You don't have permission to use this command." (only if the response is not done), `NotOwner` with "Only the bot owners can manage the admin list.", anything else with the generic "Something went wrong..." text like `AdminCog`; command group `/admins` (`name="admins"`, description "Manage the admin list (owners only)", `contexts=SERVER_ONLY`) with sub commands `add(user: disnake.User)`, `remove(user: disnake.User)`, `list()`. Each sub command calls `await inter.response.defer(ephemeral=True)` first, then the service with `actor=inter.author.display_name`, and answers with `followup.send(..., ephemeral=True)` (`list` sends one message per chunk of at most 1900 characters, lines `Owner: <name> (ID n)` and `Admin: <name> (ID n)`, name `unknown user` when unknown; an empty added list still shows the owners).

- [ ] **Step 1: Write failing tests** (follow `tests/cogs/test_admin.py` and `tests/cogs/test_schedule_cog.py` for fake interactions and cog checks): for each of the three sub commands: a non-admin user is refused with the generic text and the service is never called; an admin who is not an owner gets the owner text and the registry is unchanged; an owner succeeds; in a DM the shared admin check's server-only answer applies; while idle the admin idle reply applies; answers are ephemeral; `add` passes the actor name and the service result text through; `remove` of an owner and of a non-admin pass the service's refusal text; `list` with 3 owners and 120 added admins is split into messages of at most 1900 characters and every id appears once; the command callbacks themselves never run when the check fails (use the cog check the way disnake calls it). A test that the cog is registered in `bot/main.py` (see how other cogs are asserted in `tests/test_main.py`).
- [ ] **Step 2: Run** `uv run pytest tests/cogs/test_admins_cog.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the cog and register it.
- [ ] **Step 4: Run** `uv run pytest -q` and `uv run ruff check .`. Expected: PASS.
- [ ] **Step 5: Commit** (`feat: owner-only /admins add, remove and list commands`).

---

### Task 3: Docs and final verification

**Files:**
- Modify: `docs/user-guide.md` (commands section 7.3: the three `/admins` commands, owners only; the admin page section 7.7: removals now logged, plus a sentence that owners are those in `ADMIN_USER_IDS`), `docs/developer-guide.md` (the shared service, the cog's owner check and error handler, the log entries with actor), `CHANGELOG.md` (`### Added` under `[Unreleased]`).

- [ ] **Step 1: Update** the documents (no em dash).
- [ ] **Step 2: Run** `uv run pytest -q`, `uv run ruff check .`. Expected: all pass.
- [ ] **Step 3: Manual checklist** (needs a live guild; report as not run if unavailable): as owner add and remove a test account, see both log entries and the private answers; as a non-owner admin run `/admins list` (refused); as a normal user (refused); try removing an owner; remove an admin on the webpage and see the log entry.
- [ ] **Step 4: Commit** (`docs: owner-only admin commands`).

---

## Self-review notes

- Spec coverage: three commands and owner-only enforcement (Task 2); add/remove rules, bot accounts, owners protected, save/log failure handling, actor in log entries, webpage sharing the code (Task 1); idle rule and server-only (Task 2); docs (Task 3).
- Names used across tasks: `AdminManagementService`, `AdminChange`, `NotOwner`, `AdminsCog`, `log_admin_removed`, `actor`.
