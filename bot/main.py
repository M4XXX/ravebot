"""Bot entry point — migrations, BotContext init, Notion setup, cog loading, bot.start().

Startup sequence:
  1. Load config from env vars (exits with descriptive error if any required var missing)
  2. Configure logging
  3. Create BotContext with config
  4. Run Alembic migrations (subprocess: alembic upgrade head) — exits on failure
  5. Open asyncpg pool — exits if PostgreSQL unreachable
  6. Create disnake Bot with correct intents and the AdminLogService
  7. Notion: NotionApi -> ensure_schema -> ensure_views -> NotionClient. The schema
     and view config keys live under guild_id 0 (global), so this runs before
     Discord connects. Exits with a clear message if the parent page is unusable.
  8. Create the services (incl. NotionSyncService) and load the cogs
  9. Start bot (Discord connects only AFTER DB and Notion are ready)
 10. On shutdown: close pool and Notion API client

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
from bot.db.pool import create_pool, run_migrations
from bot.notion.api import NotionApi, NotionWriteError
from bot.notion.client import NotionClient
from bot.notion.schema import NotionSchemaError, ensure_schema
from bot.notion.views import ensure_views
from bot.timezone import load_stored_timezone

load_dotenv()

logger = logging.getLogger(__name__)

# Notion schema/view config keys are global (not per Discord guild).
NOTION_CONFIG_GUILD_ID = 0

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


async def main() -> None:
    """Async entry point — configure, run migrations, open pool, start bot."""
    config = Config.from_env()
    _setup_logging(config.log_level)

    ctx = BotContext(config=config)

    # Story 1.2: run migrations then open DB pool before Discord connects
    await run_migrations(config.postgres_url)
    db = await create_pool(config)
    ctx.db = db

    bot = create_bot()

    # Story 1.3: init AdminLogService and load cogs
    from bot.cogs.admin import AdminCog
    from bot.cogs.booking import BookingCog
    from bot.cogs.events import EventsCog
    from bot.cogs.onboarding import OnboardingCog
    from bot.cogs.outreach_cog import OutreachCog
    from bot.log import AdminLogService

    ctx.log_service = AdminLogService(bot=bot, db=ctx.db)

    # Notion: ensure databases and views exist before Discord connects (Ruling 29)
    notion_api = NotionApi(config.notion_api_key, ctx.log_service)
    try:
        ids = await ensure_schema(
            notion_api, db, NOTION_CONFIG_GUILD_ID, config.notion_parent_page_id
        )
        await ensure_views(notion_api, db, NOTION_CONFIG_GUILD_ID, ids)
    except (NotionSchemaError, NotionWriteError) as exc:
        logger.error("Notion setup failed: %s", exc)
        await db.close()
        await notion_api.close()
        sys.exit(f"{NOTION_SETUP_FAILED} (Cause: {exc})")
    ctx.notion_client = NotionClient(notion_api, ids)

    # Story 3.2: init LineupService after DB and log_service are ready
    from bot.services.lineup import LineupService
    from bot.ui.views import LineupPostView

    post_view = LineupPostView(ctx)
    bot.add_view(post_view)  # persistent: the button keeps working after restarts
    from bot.ui.log_reply import LogReplyView

    bot.add_view(LogReplyView(ctx))  # persistent: Reply works on old log entries
    ctx.lineup_service = LineupService(
        bot=bot,
        view=post_view,
        db=ctx.db,
        log_service=ctx.log_service,
        notion_client=ctx.notion_client,
    )

    # Notion rework: reconcile hand edits in Notion with Postgres on every poll
    from bot.services.notion_sync import NotionSyncService
    ctx.notion_sync_service = NotionSyncService(
        bot=bot,
        db=ctx.db,
        notion_client=ctx.notion_client,
        log_service=ctx.log_service,
    )

    # Story 5.1: init OutreachService after notion_client and log_service are ready
    from bot.services.outreach import OutreachService
    ctx.outreach_service = OutreachService(
        bot=bot,
        db=ctx.db,
        notion_client=ctx.notion_client,
        log_service=ctx.log_service,
    )

    # Story 5.4: init RemindersService after notion_client and log_service are ready
    from bot.services.reminders import RemindersService
    ctx.reminders_service = RemindersService(
        bot=bot,
        db=ctx.db,
        notion_client=ctx.notion_client,
        log_service=ctx.log_service,
    )

    # Story 6.2: init InterventionService
    from bot.services.intervention import InterventionService
    ctx.intervention_service = InterventionService(
        bot=bot, db=ctx.db, log_service=ctx.log_service
    )

    # Story 6.3: init AdminBookingService
    from bot.services.admin_booking import AdminBookingService
    ctx.admin_booking_service = AdminBookingService(
        bot=bot,
        db=ctx.db,
        notion_client=ctx.notion_client,
        log_service=ctx.log_service,
    )

    bot.add_cog(OnboardingCog(ctx))
    bot.add_cog(BookingCog(ctx))
    bot.add_cog(OutreachCog(ctx))
    bot.add_cog(AdminCog(ctx))
    bot.add_cog(EventsCog(ctx))

    @bot.event
    async def on_ready() -> None:  # noqa: RUF029
        logger.info("Bot ready")
        # The time zone is a guild-level setting, so it can only load once guilds are known.
        if bot.guilds:
            try:
                await load_stored_timezone(ctx.db, bot.guilds[0].id)
            except Exception:
                logger.warning("Could not load the stored time zone", exc_info=True)

    try:
        await bot.start(config.discord_bot_token)
    finally:
        if ctx.db is not None:
            await ctx.db.close()
            logger.info("asyncpg pool closed.")
        await notion_api.close()
        logger.info("Notion API client closed.")


if __name__ == "__main__":
    asyncio.run(main())
