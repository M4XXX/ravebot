# DM menu after flows: plan

Spec: docs/superpowers/specs/2026-11-23-dm-menu-after-flows-design.md. TDD, one commit per task, ruff clean, no em dashes (tests/test_no_em_dash.py).

## Task 1: storage and MenuService
- `migrations/versions/0022_dm_menus.py` (+ `tests/test_migration_0022.py` like 0021) and `bot/db/dm_menus.py` (`get_menu`, `set_menu` upsert, `delete_menu`).
- `bot/services/menu.py`: `MenuService(bot, ctx, delay=2.0)` with `schedule(user_id)` (debounced task), `post_now(channel, user_id)` (cancel pending, delete old, send `texts.MENU` + `FallbackMenuView`, store id), shared DJ state lookup (`active`, `onboarded`), skip rules from the spec. `ctx.menu_service` field in `bot/context.py`; construct in `bot/main.py`.
- Tests in `tests/services/test_menu.py`: debounce, delay, skips (lock held, open conversation, idle, guest), old menu deleted, NotFound ignored, failed send leaves the old record.

## Task 2: triggers
- `FlowLock`: optional `on_release` callback fired only when a held lock is freed by `release()` or `release_if()`; `release_if(token, notify=True)`; `FlowView._expire` passes notify False for an untouched flow. `BotContext.get_user_lock` wires `on_release` to `menu_service.schedule(user_id)` when the service exists.
- `Reply.menu_after: bool = False`, set for onboarding done, declined, cancelled (onboarding and edit). `send_reply` calls `ctx.menu_service.schedule(user_id)` after sending such a reply.
- `bot/cogs/onboarding.py`: `on_message` uses `menu_service.post_now` for the free-text menu.
- Tests: lock release triggers once, expiry does not, lineup status does not, booking and cancel confirm paths trigger, reply flags.

## Task 3: docs
CHANGELOG, developer guide (table, service, triggers), user guide (menu returns after flows).
