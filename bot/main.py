"""Bot entry point — migrations, BotContext init, cog loading, bot.run().

Startup sequence:
  1. Load config from env vars (exits with descriptive error if any required var missing)
  2. Configure logging
  3. Create BotContext with config
  4. Run Alembic migrations (subprocess: alembic upgrade head) — exits on failure
  5. Open asyncpg pool — exits if PostgreSQL unreachable
  6. Assign pool to BotContext.db
  7. Create disnake Bot with correct intents
  8. Start bot (Discord connects only AFTER DB is ready)
  9. On shutdown: close pool gracefully

Future stories extend the sequence:
  - Story 3.x: add Notion poll(), read_slots(), write_booking() to NotionClient

[Source: architecture.md#Implementation Sequence]
[Source: architecture.md#Decision Impact Analysis]
"""
import asyncio
import logging
import signal

import disnake
from disnake.ext import commands
from dotenv import load_dotenv

from bot import active, selfcheck, slots
from bot import guild as served
from bot.config import Config
from bot.context import BotContext
from bot.db.pool import create_pool, run_migrations

load_dotenv()

logger = logging.getLogger(__name__)


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
    # Never ping from bot messages: mirrored DMs may contain @everyone or role mentions
    return commands.Bot(
        command_prefix=commands.when_mentioned,  # no prefix commands; silences the intent warning
        intents=intents,
        allowed_mentions=disnake.AllowedMentions.none(),
    )


TRANSCRIPT_FLUSH_SECONDS = 5.0


async def graceful_shutdown(bot: commands.Bot, ctx: BotContext) -> None:
    """Post the queued DM transcripts while Discord is still reachable, then disconnect."""
    if ctx.log_service is not None:
        try:
            await asyncio.wait_for(
                ctx.log_service.flush_transcripts(), timeout=TRANSCRIPT_FLUSH_SECONDS
            )
        except Exception:
            logger.warning("Shutting down with DM transcripts still queued", exc_info=True)
    await bot.close()


async def main() -> None:
    """Async entry point — configure, run migrations, open pool, start bot."""
    config = Config.from_env()
    served.configure(config.guild_id)
    _setup_logging(config.log_level)

    ctx = BotContext(config=config)

    # Story 1.2: run migrations then open DB pool before Discord connects
    await run_migrations(config.postgres_url)
    ctx.db = await create_pool(config)

    bot = create_bot()

    # Story 1.3: init AdminLogService and load cogs
    from bot.cogs.admin import AdminCog
    from bot.cogs.booking import BookingCog
    from bot.cogs.events import EventsCog
    from bot.cogs.onboarding import OnboardingCog
    from bot.cogs.outreach_cog import OutreachCog
    from bot.log import AdminLogService
    from bot.notion.client import NotionClient

    ctx.log_service = AdminLogService(bot=bot, db=ctx.db)

    # Story 1.5: init NotionClient after log_service is ready
    slots.configure(config.notion_layout, frozenset(config.notion_bookable_statuses))
    ctx.notion_client = NotionClient(
        api_key=config.notion_api_key,
        database_id=config.notion_database_id,
        log_service=ctx.log_service,
        layout=config.notion_layout,
        new_event_status=(config.notion_bookable_statuses or (None,))[0],
    )

    # Story 3.2: init LineupService after DB and log_service are ready
    from bot.services.lineup import LineupService
    ctx.lineup_service = LineupService(bot=bot, db=ctx.db, log_service=ctx.log_service)

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

    bot.add_cog(OnboardingCog(ctx, bot))
    bot.add_cog(BookingCog(ctx))
    bot.add_cog(OutreachCog(ctx, bot))
    bot.add_cog(AdminCog(ctx))
    bot.add_cog(EventsCog(ctx, bot))

    # Buttons in DMs sent before a restart keep working (static custom_ids, timeout=None)
    from bot.ui.views import FallbackMenuView, OnboardingIntroView, StartOverView

    bot.add_view(FallbackMenuView(ctx))
    bot.add_view(OnboardingIntroView(ctx, user_id=0, guild_id=0))
    bot.add_view(StartOverView(ctx))

    @bot.event
    async def on_ready() -> None:  # noqa: RUF029
        logger.info("Bot ready")
        gid = served.resolve(bot)
        if gid is None:
            logger.error(
                "Serving no server (member of %d, GUILD_ID=%s) — staying idle",
                len(bot.guilds),
                config.guild_id,
            )
        else:
            mode = "ACTIVE" if await active.is_active(ctx.db, gid) else "PASSIVE"
            logger.info("Serving guild %s in %s mode", gid, mode)
            bot.loop.create_task(selfcheck.run(bot, ctx, gid))
        # Story 1.5: validate Notion schema now that Discord is connected
        # (admin log channel may already be configured for error posting)
        if ctx.notion_client is not None:
            notion = ctx.notion_client

            async def validate_notion() -> None:
                # The required columns depend on the layout (tds: one per slot)
                required = await slots.required_properties(ctx.db, gid)
                await notion.validate_schema(required_properties=required)

            bot.loop.create_task(validate_notion())

    # In the container the bot is PID 1, which ignores SIGTERM unless it has a handler:
    # docker stop then killed it after 10 s, skipping the cleanup below.
    loop = asyncio.get_running_loop()
    shutdown_tasks: set[asyncio.Task[None]] = set()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("%s received — shutting down", sig.name)
        task = loop.create_task(graceful_shutdown(bot, ctx))
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:  # Windows event loops have no signal handlers
            pass

    try:
        await bot.start(config.discord_bot_token)
    finally:
        # bot.start() returns as soon as the gateway is closed, while graceful_shutdown is
        # still closing the HTTP session; asyncio.run would cancel it half-way.
        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)
        elif not bot.is_closed():
            await bot.close()
        if ctx.db is not None:
            await ctx.db.close()
            logger.info("asyncpg pool closed.")
        if ctx.notion_client is not None:
            await ctx.notion_client.close()
            logger.info("NotionClient closed.")


if __name__ == "__main__":
    asyncio.run(main())
