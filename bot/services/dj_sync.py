"""Queue DJ changes for the Notion mirror (Postgres is the source of truth).

The mirror worker (bot/sync/worker.py) writes the DJ's Notion row from the djs
table in the background, and uploads the logo while ``logo_synced`` is false.
"""
import logging
from typing import TYPE_CHECKING

from bot.db.djs import list_unsynced_logo_user_ids
from bot.sync.worker import changed

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)


async def sync_dj(ctx: "BotContext", discord_user_id: int) -> bool:
    """Queue the DJ for the Notion mirror after a Postgres write; returns True.

    Never calls Notion and never raises. The mirror reads everything it writes from
    the djs row.
    """
    changed(ctx, "dj", str(discord_user_id))
    return True


async def queue_unsynced_logos(ctx: "BotContext") -> int:
    """Queue every DJ whose logo is not in Notion yet; returns how many.

    Run once the mirror worker runs, so a logo upload that failed earlier is
    retried by the worker. Nothing to do (0) without the mirror.
    """
    if ctx.db is None or ctx.mirror is None:
        return 0
    try:
        user_ids = await list_unsynced_logo_user_ids(ctx.db)
    except Exception:
        logger.warning("Listing DJs with unsynced logos failed", exc_info=True)
        return 0
    for user_id in user_ids:
        changed(ctx, "dj", str(user_id))
    return len(user_ids)
