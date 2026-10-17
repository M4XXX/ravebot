"""Background loops survive their own failures.

disnake's tasks.loop only retries network errors; any other exception (a 403 from Discord,
a KeyError in a Notion payload) stopped the loop for good. guarded() logs the failure, alerts
the admin log at most once an hour per loop, and lets the next cycle run. The time of the
last successful cycle per loop is shown in /config show.
"""
import datetime
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bot.ui.embeds import ErrorCode

logger = logging.getLogger(__name__)

ALERT_INTERVAL_SECONDS = 3600

_last_ok: dict[str, datetime.datetime] = {}
_last_alert: dict[str, float] = {}


async def guarded(name: str, cycle: Callable[[], Awaitable[None]], log_service: Any) -> None:
    """Run one loop cycle; never let an exception end the loop."""
    try:
        await cycle()
    except Exception as exc:
        logger.exception("Background loop %s failed", name)
        now = time.monotonic()
        if log_service is not None and now - _last_alert.get(name, -ALERT_INTERVAL_SECONDS) >= (
            ALERT_INTERVAL_SECONDS
        ):
            _last_alert[name] = now
            try:
                await log_service.post_error(
                    error_code=ErrorCode.ERR_BACKGROUND_TASK,
                    trigger=f"Background task {name} raised {type(exc).__name__}: {exc}",
                    action_taken="This cycle was skipped; the loop keeps running.",
                    manual_action="See the bot log for the traceback.",
                )
            except Exception:
                logger.exception("Could not post the %s failure to the admin log", name)
        return
    _last_ok[name] = datetime.datetime.now(datetime.UTC)


def last_ok() -> dict[str, datetime.datetime]:
    """Time of the last successful cycle per loop name."""
    return dict(_last_ok)
