"""Background loops survive their own failures.

disnake's tasks.loop only retries network errors; any other exception (a 403 from Discord,
a KeyError in a Notion payload) stopped the loop for good. guarded() logs the failure, alerts
the admin log at most once an hour per loop, and lets the next cycle run. The time of the
last successful cycle per loop is shown in /config show; a failed or skipped cycle does
not count as successful.
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


class CycleFailed(Exception):
    """A cycle failure with its own admin-log wording (guarded() still rate-limits it)."""

    def __init__(
        self,
        trigger: str,
        error_code: ErrorCode = ErrorCode.ERR_BACKGROUND_TASK,
        manual_action: str = "See the bot log for the traceback.",
    ) -> None:
        super().__init__(trigger)
        self.trigger = trigger
        self.error_code = error_code
        self.manual_action = manual_action


class CycleSkipped(Exception):
    """The cycle could not do its work and has already reported why (e.g. DB down).

    guarded() neither alerts again nor counts the cycle as OK.
    """


def alert_due(key: str) -> bool:
    """True at most once per ALERT_INTERVAL_SECONDS for key, and marks it as alerted.

    For admin-log alerts a loop would otherwise repeat every cycle.
    """
    now = time.monotonic()
    last = _last_alert.get(key)
    if last is not None and now - last < ALERT_INTERVAL_SECONDS:
        return False
    _last_alert[key] = now
    return True


async def guarded(name: str, cycle: Callable[[], Awaitable[None]], log_service: Any) -> None:
    """Run one loop cycle; never let an exception end the loop."""
    try:
        await cycle()
    except CycleSkipped:
        return
    except Exception as exc:
        logger.exception("Background loop %s failed", name)
        if log_service is not None and alert_due(name):
            if isinstance(exc, CycleFailed):
                code, trigger, manual = exc.error_code, exc.trigger, exc.manual_action
            else:
                code = ErrorCode.ERR_BACKGROUND_TASK
                trigger = f"Background task {name} raised {type(exc).__name__}: {exc}"
                manual = "See the bot log for the traceback."
            try:
                await log_service.post_error(
                    error_code=code,
                    trigger=trigger,
                    action_taken="This cycle was skipped; the loop keeps running.",
                    manual_action=manual,
                )
            except Exception:
                logger.exception("Could not post the %s failure to the admin log", name)
        return
    _last_ok[name] = datetime.datetime.now(datetime.UTC)


def last_ok() -> dict[str, datetime.datetime]:
    """Time of the last successful cycle per loop name."""
    return dict(_last_ok)
