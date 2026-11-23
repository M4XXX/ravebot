"""Bot entry point — migrations, BotContext init, Notion setup, cog loading, bot.start().

Startup sequence:
  1. Load config from env vars (exits with descriptive error if any required var missing)
  2. Configure logging
  3. Create BotContext with config
  4. Run Alembic migrations (subprocess: alembic upgrade head) — exits on failure
  5. Open asyncpg pool — exits if PostgreSQL unreachable
  6. Create disnake Bot with correct intents and the AdminLogService
  7. Notion (only when both NOTION_* are set): NotionApi -> ensure_schema -> ensure_views
     -> NotionClient. The schema and view config keys live under guild_id 0 (global),
     so this runs before Discord connects. Exits with a clear message if the parent
     page is unusable.
  8. Upgrade import (bot/sync/notion_import.py): on the first start after the switch to
     Postgres as the source of truth, a safety export, then events, slots and DJs are
     imported from Notion once. Until it succeeds the bot is in maintenance (bookings and
     edits refused) and the mirror worker stays off. The result is posted in on_ready.
     When Postgres is empty (no DJs, no events) and Notion holds events, a rebuild from
     Notion (bot/sync/rebuild.py) runs instead and marks the upgrade done (R19).
  9. Create the services and load the cogs
 10. Start bot (Discord connects only AFTER DB and Notion are ready)
 11. With a Notion client and the import done: start the Notion mirror worker
     (bot/sync/worker.py), queue the DJs whose logo is not in Notion yet and set
     ctx.sync_cycle (bot/sync/cycle.py), which the 5-minute Notion poll runs
 12. Startup cases (bot/sync/rebuild.py), only with Notion and once the upgrade import is
     done: Postgres empty and Notion holds events -> rebuild from Notion (result posted in
     on_ready); Postgres filled and Notion empty -> queue everything for Notion
 13. On shutdown: stop the mirror worker, close pool and Notion API client

[Source: architecture.md#Implementation Sequence]
[Source: architecture.md#Decision Impact Analysis]
"""
import asyncio
import logging
import sys

import disnake
from disnake.ext import commands
from dotenv import load_dotenv

from bot.config import Config
from bot.context import BotContext
from bot.db.config import GLOBAL_GUILD_ID, set_value
from bot.db.pool import create_pool, run_migrations
from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.client import NotionClient
from bot.notion.schema import (
    RECREATE_APPROVED_KEY,
    NotionDatabasesMissing,
    NotionSchemaError,
    ensure_schema,
)
from bot.notion.views import ensure_views
from bot.services import activation
from bot.services.dj_sync import queue_unsynced_logos
from bot.sync.cycle import SyncCycle
from bot.sync.notion_import import (
    pause_if_import_pending,
    post_upgrade_result,
    run_upgrade_import_if_needed,
)
from bot.sync.rebuild import (
    post_rebuild_result,
    run_startup_rebuild_if_needed,
    run_startup_recovery,
)
from bot.sync.worker import MirrorWorker
from bot.timezone import load_stored_timezone
from bot.ui import texts
from bot.ui.gates import STARTUP_ADMIN_DM
from bot.web import start_admin_page, stop_admin_page

load_dotenv()

logger = logging.getLogger(__name__)

# Notion schema/view config keys are global (not per Discord guild).
NOTION_CONFIG_GUILD_ID = GLOBAL_GUILD_ID

NOTION_SETUP_FAILED = (
    "Cannot use the Notion parent page: share the page with your integration "
    "(page menu > Connections) and check NOTION_API_KEY and NOTION_PARENT_PAGE_ID."
)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def create_bot() -> commands.Bot:
    """Create the disnake Bot with required intents.

    members=True is required for on_member_update (DJ role detection — FR1).
    """
    intents = disnake.Intents.default()
    intents.members = True  # Required for on_member_update (FR1 — DJ role detection)
    return commands.Bot(command_prefix="!", intents=intents)


def _add_cog(bot: commands.Bot, cog: commands.Cog) -> None:
    """Add a cog with a real `bot` attribute (disnake does not set one).

    Set before add_cog because the cog loops start in cog_load and read self.bot.
    """
    cog.bot = bot  # type: ignore[attr-defined]
    bot.add_cog(cog)


async def notify_admins_if_idle(bot: commands.Bot, db: object, admin_ids: object) -> None:
    """While the bot is idle, DM every admin that setup is missing. Never raises."""
    try:
        if await activation.bot_is_active(bot, db):
            return
    except Exception:
        logger.warning("Could not read the activation state", exc_info=True)
        return
    for admin_id in sorted(admin_ids):  # type: ignore[call-overload]
        try:
            user = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            await user.send(STARTUP_ADMIN_DM)
        except Exception:
            logger.warning("Could not send the setup DM to admin %s", admin_id, exc_info=True)


async def _start_mirror(ctx: BotContext, bot: commands.Bot) -> None:
    """Start the Notion mirror worker once a Notion client exists (never without Notion).

    Not during maintenance: until the upgrade import succeeded ctx.mirror stays None,
    so changed() queues nothing and Notion is not written. Once started, every DJ
    whose logo is not in Notion yet is queued, so the worker retries the upload.
    The sync cycle (hand edits from Notion) is set up together with the worker.
    """
    if (
        ctx.mirror is None
        and ctx.notion_client is not None
        and ctx.db is not None
        and ctx.maintenance is None
    ):
        ctx.mirror = MirrorWorker(
            ctx.db,
            ctx.notion_client,
            ctx.log_service,
            name_resolver=lambda uid: getattr(bot.get_user(uid), "display_name", None),
        )
        ctx.mirror.start()
        await queue_unsynced_logos(ctx)
    if ctx.mirror is not None and ctx.sync_cycle is None:
        ctx.sync_cycle = SyncCycle(ctx, bot)


async def main() -> None:
    """Async entry point — configure, run migrations, open pool, start bot."""
    config = Config.from_env()
    _setup_logging(config.log_level)

    ctx = BotContext(config=config)

    # Story 1.2: run migrations then open DB pool before Discord connects
    await run_migrations(config.postgres_url)
    db = await create_pool(config)
    ctx.db = db
    from bot.services.admin_registry import AdminRegistry

    ctx.admins = AdminRegistry(config.admin_user_ids, db)
    await ctx.admins.load()

    bot = create_bot()

    # Story 1.3: init AdminLogService and load cogs
    from bot.cogs.admin import AdminCog
    from bot.cogs.admins_cog import AdminsCog
    from bot.cogs.booking import BookingCog
    from bot.cogs.events import EventsCog
    from bot.cogs.onboarding import OnboardingCog
    from bot.cogs.outreach_cog import OutreachCog
    from bot.cogs.schedule_cog import ScheduleCog
    from bot.cogs.setup import SetupCog
    from bot.log import AdminLogService

    ctx.log_service = AdminLogService(bot=bot, db=ctx.db)

    notion_api: NotionApi | None = None
    if config.notion_enabled:
        # Notion: ensure databases and views exist before Discord connects (Ruling 29)
        notion_api = NotionApi(config.notion_api_key, ctx.log_service)

        async def setup_notion() -> NotionClient:
            ids = await ensure_schema(
                notion_api, db, NOTION_CONFIG_GUILD_ID, config.notion_parent_page_id
            )
            await ensure_views(notion_api, db, NOTION_CONFIG_GUILD_ID, ids)
            return NotionClient(notion_api, ids)

        async def recover_notion() -> str | None:
            """Admin page: create a new set of databases now. Returns an error text or None."""
            await set_value(db, NOTION_CONFIG_GUILD_ID, RECREATE_APPROVED_KEY, "1")
            try:
                client = await setup_notion()
            except (NotionSchemaError, NotionWriteError) as exc:
                logger.error("Creating new Notion databases failed: %s", exc)
                await set_value(db, NOTION_CONFIG_GUILD_ID, RECREATE_APPROVED_KEY, "0")
                return str(exc)
            ctx.attach_notion(client)
            ctx.notion_problem = None
            if not await run_startup_rebuild_if_needed(ctx):
                await run_upgrade_import_if_needed(ctx)
            await post_upgrade_result(ctx)
            await _start_mirror(ctx, bot)
            await run_startup_recovery(ctx)
            await post_rebuild_result(ctx)
            logger.info("New Notion databases created after an admin approved it")
            return None

        try:
            ctx.notion_client = await setup_notion()
        except NotionDatabasesMissing as exc:
            # Do not replace the databases on our own (a 404 can be a lost share): keep
            # running without Notion and let an admin decide on the admin page.
            logger.error("Notion databases missing, waiting for an admin: %s", exc.detail)
            ctx.notion_problem = exc.detail
        except (NotionSchemaError, NotionWriteError) as exc:
            logger.error("Notion setup failed: %s", exc)
            await db.close()
            await notion_api.close()
            sys.exit(f"{NOTION_SETUP_FAILED} (Cause: {exc})")
        async def connect_notion() -> str | None:
            """Admin page retry: read the stored databases again. Error text or None."""
            try:
                client = await setup_notion()
            except NotionDatabasesMissing as exc:
                return exc.detail
            except (NotionSchemaError, NotionWriteError) as exc:
                return str(exc)
            ctx.attach_notion(client)
            ctx.notion_problem = None
            return None

        ctx.notion_recover = recover_notion
        ctx.notion_connect = connect_notion
        # Databases missing before the one-time import: paused until it ran (decision 8).
        await pause_if_import_pending(ctx)
        # An empty Postgres with events in Notion is rebuilt instead (R19).
        if not await run_startup_rebuild_if_needed(ctx):
            await run_upgrade_import_if_needed(ctx)
        await _start_mirror(ctx, bot)
        await run_startup_recovery(ctx)  # never raises

        async def start_mirror() -> None:
            await _start_mirror(ctx, bot)

        # A rebuild that ends a pause (failed upgrade or startup rebuild) starts the
        # mirror and sync cycle through this hook (bot/sync/rebuild.py).
        ctx.notion_start_mirror = start_mirror

    # Story 3.2: init LineupService after DB and log_service are ready
    from bot.services.lineup import LineupService
    from bot.ui.views import LineupPostView

    post_view = LineupPostView(ctx)
    bot.add_view(post_view)  # persistent: the button keeps working after restarts
    from bot.ui.log_reply import LogReplyView

    bot.add_view(LogReplyView(ctx))  # persistent: Reply works on old log entries
    from bot.ui.management_lineup import ManagementInfoView

    # persistent: the lineup info button; the lineup service gets this same instance
    management_info_view = ManagementInfoView(ctx)
    bot.add_view(management_info_view)
    from bot.ui.setup_wizard import registered_views

    for wizard_view in registered_views(ctx):  # persistent: the wizard works after restarts
        bot.add_view(wizard_view)
    # DJ onboarding conversation and profile: the service needs the bot for roles and names
    from bot.services.dj_conversation import DjConversationService
    from bot.ui import dj_profile
    from bot.ui.views import FallbackMenuView

    ctx.dj_conversations = DjConversationService(ctx, client=bot)
    from bot.services.menu import MenuService

    ctx.menu_service = MenuService(bot, ctx)
    for dj_view in dj_profile.registered_views(ctx):  # persistent: old DM buttons keep working
        bot.add_view(dj_view)
    bot.add_view(FallbackMenuView(ctx, all_buttons=True))  # persistent: menus after restarts
    from bot.ui.views import LineupEventPickView

    # persistent: the event buttons under a lineup work after restarts
    bot.add_view(LineupEventPickView([], ctx, all_buttons=True))
    ctx.lineup_service = LineupService(
        bot=bot,
        view=post_view,
        db=ctx.db,
        log_service=ctx.log_service,
    )
    from bot.services.management_lineup import ManagementLineupService

    ctx.management_lineup_service = ManagementLineupService(
        bot=bot,
        db=ctx.db,
        log_service=ctx.log_service,
        view=management_info_view,
    )
    # A hand-deleted lineup message is re-posted on the next refresh
    bot.add_listener(
        ctx.management_lineup_service.on_raw_message_delete, "on_raw_message_delete"
    )
    bot.add_listener(
        ctx.management_lineup_service.on_raw_bulk_message_delete, "on_raw_bulk_message_delete"
    )
    from bot.services.schedule import ScheduleService

    ctx.schedule_service = ScheduleService(ctx)

    # Story 5.1: init OutreachService after log_service is ready
    from bot.services.outreach import OutreachService
    ctx.outreach_service = OutreachService(
        bot=bot,
        db=ctx.db,
        log_service=ctx.log_service,
    )

    # Story 5.4: init RemindersService after log_service is ready
    from bot.services.reminders import RemindersService
    ctx.reminders_service = RemindersService(
        bot=bot,
        db=ctx.db,
        log_service=ctx.log_service,
    )

    # Story 6.3: init AdminBookingService
    from bot.services.admin_booking import AdminBookingService
    ctx.admin_booking_service = AdminBookingService(
        bot=bot,
        db=ctx.db,
        log_service=ctx.log_service,
        ctx=ctx,
    )

    from bot.services.dj_admin import DjAdminService
    ctx.dj_admin_service = DjAdminService(ctx, bot)

    from bot.services.event_cancel import EventCancelService
    ctx.event_cancel_service = EventCancelService(ctx, bot)

    _add_cog(bot, OnboardingCog(ctx))
    _add_cog(bot, BookingCog(ctx))
    _add_cog(bot, OutreachCog(ctx))
    _add_cog(bot, AdminCog(ctx))
    _add_cog(bot, EventsCog(ctx))
    _add_cog(bot, SetupCog(ctx))
    _add_cog(bot, ScheduleCog(ctx))

    from bot.services.admin_management import AdminManagementService
    _add_cog(bot, AdminsCog(ctx, AdminManagementService(ctx, bot)))

    startup_dm_sent = False

    @bot.event
    async def on_ready() -> None:  # noqa: RUF029
        nonlocal startup_dm_sent
        logger.info("Bot ready")
        # The time zone is a guild-level setting, so it can only load once guilds are known.
        if bot.guilds:
            try:
                await load_stored_timezone(ctx.db, bot.guilds[0].id)
            except Exception:
                logger.warning("Could not load the stored time zone", exc_info=True)
        # on_ready fires again on every reconnect: the admin DM goes out once per process
        if not startup_dm_sent:
            startup_dm_sent = True
            # Recover from a failed startup load so cogs see the added admins.
            try:
                if ctx.admins is not None:
                    await ctx.admins.ensure_loaded()
            except Exception:
                logger.warning("Could not load the added admins", exc_info=True)
            await notify_admins_if_idle(bot, ctx.db, ctx.admin_ids())
            if ctx.notion_problem and ctx.log_service is not None:
                try:
                    await ctx.log_service.log_attention(
                        texts.LOG_NOTION_MISSING_TITLE,
                        texts.log_notion_missing_text(
                            ctx.notion_problem, paused=ctx.maintenance is not None
                        ),
                        texts.LOG_NOTION_MISSING_TODO,
                    )
                except Exception:
                    logger.warning("Could not post the missing Notion databases", exc_info=True)
            await post_upgrade_result(ctx)
            await post_rebuild_result(ctx)

    runner = await start_admin_page(ctx, bot)
    try:
        await bot.start(config.discord_bot_token)
    finally:
        await stop_admin_page(runner)
        if ctx.mirror is not None:
            await ctx.mirror.stop()
            logger.info("Notion mirror worker stopped.")
        if ctx.db is not None:
            await ctx.db.close()
            logger.info("asyncpg pool closed.")
        if notion_api is not None:
            await notion_api.close()
            logger.info("Notion API client closed.")


if __name__ == "__main__":
    asyncio.run(main())
