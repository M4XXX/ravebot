"""Web admin page: start and stop helpers used by bot.main."""
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)

HOST = "0.0.0.0"  # noqa: S104  (the page is reached from the local network)


async def start_admin_page(ctx: "BotContext", bot: Any) -> web.AppRunner | None:
    """Start the admin page. Returns the runner, or None when it is off or cannot start.

    Never raises and never logs the password.
    """
    password = ctx.config.admin_page_password
    if not password:
        logger.info("Admin page is off (ADMIN_PAGE_PASSWORD is not set).")
        return None

    raw_port = ctx.config.admin_page_port
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError(raw_port)
    except ValueError:
        logger.error(
            "Admin page not started: ADMIN_PAGE_PORT must be a number from 1 to 65535, got %r.",
            raw_port,
        )
        return None

    runner: web.AppRunner | None = None
    try:
        from bot.web.app import create_app
        from bot.web.service import AdminPageService

        app = create_app(AdminPageService(ctx, bot), password)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, port)
        await site.start()
    except Exception as exc:
        if isinstance(exc, OSError):
            logger.error(
                "Admin page not started: could not listen on port %s (%s).", port, exc
            )
        else:
            logger.error("Admin page could not start: %s", exc)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                logger.warning("Admin page cleanup failed", exc_info=True)
        return None
    logger.info("Admin page listening on port %d.", port)
    return runner


async def stop_admin_page(runner: web.AppRunner | None) -> None:
    """Stop the admin page if it runs. Never raises."""
    if runner is None:
        return
    try:
        await runner.cleanup()
        logger.info("Admin page stopped.")
    except Exception:
        logger.warning("Stopping the admin page failed", exc_info=True)
