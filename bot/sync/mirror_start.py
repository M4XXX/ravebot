"""Start the Notion mirror after an operation that ended a pause (rebuild, backup import).

The mirror and the sync cycle never start while the bot is paused (failed upgrade import
or failed startup rebuild), so edits since then were not queued. ``ctx.notion_start_mirror``
(set by main.py only when Notion is configured) starts them the way startup does; then
every record is queued for Notion.
"""
import logging
from typing import Any

__all__ = ["ensure_mirror"]

logger = logging.getLogger(__name__)


async def ensure_mirror(ctx: Any, after: str) -> None:
    """Start the mirror and queue a full resync when it is not running. Never raises.

    ``after`` names the operation for the log ("the rebuild", "the backup import").
    """
    start = getattr(ctx, "notion_start_mirror", None)
    if getattr(ctx, "mirror", None) is not None or start is None:
        return
    try:
        await start()
        cycle = getattr(ctx, "sync_cycle", None)
        if cycle is not None:
            count = await cycle.resync_all()
            logger.info("Notion mirror started after %s; %d records queued", after, count)
    except Exception:
        logger.exception("Starting the Notion mirror after %s failed", after)
