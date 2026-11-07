"""Sync a DJ's profile (and logo image) from Postgres to the Notion DJs database."""
import logging
from typing import TYPE_CHECKING

import disnake

from bot.db.djs import (
    get_dj_by_discord_id,
    list_unsynced_logo_user_ids,
    set_logo_synced,
    set_notion_dj_page_id,
)

if TYPE_CHECKING:
    from bot.context import BotContext

logger = logging.getLogger(__name__)


async def sync_dj(ctx: "BotContext", discord_user_id: int, discord_name: str) -> bool:
    """Upsert the DJ's Notion row from the djs table; never raises.

    After any successful Notion call ``logo_synced`` becomes true (also for a DJ
    without a logo image). On failure it is set to false when a logo image
    exists and left untouched otherwise. Returns False on any failure.
    """
    has_logo = False
    db, notion = ctx.db, ctx.notion_client
    if db is None or notion is None:
        logger.warning("Notion sync unavailable for DJ %s (no db/client)", discord_user_id)
        return False
    try:
        row = await get_dj_by_discord_id(db, discord_user_id)
        if row is None:
            logger.warning("No djs row for user %s; nothing to sync", discord_user_id)
            return False
        has_logo = row["logo_image"] is not None
        page_id = await notion.upsert_dj(
            discord_user_id,
            row["dj_name"],
            discord_name,
            vrchat_name=row["vrchat_name"],
            logo_url=row["logo_url"],
            link=row["stream_url_primary"],
            alt_link=row["stream_url_backup"],
            logo_image=row["logo_image"],
        )
        await set_notion_dj_page_id(db, discord_user_id, page_id)
        await set_logo_synced(db, discord_user_id, True)
        return True
    except Exception:
        logger.warning("Notion sync failed for DJ %s", discord_user_id, exc_info=True)
        if has_logo:
            try:
                await set_logo_synced(db, discord_user_id, False)
            except Exception:
                logger.warning(
                    "Could not reset logo_synced for DJ %s", discord_user_id, exc_info=True
                )
        return False


async def retry_unsynced_logos(ctx: "BotContext", guild: disnake.Guild | None) -> int:
    """Run sync_dj for every unsynced DJ; return the number of successes."""
    db = ctx.db
    if db is None:
        return 0
    try:
        user_ids = await list_unsynced_logo_user_ids(db)
    except Exception:
        logger.warning("Listing unsynced DJs failed", exc_info=True)
        return 0
    done = 0
    for user_id in user_ids:
        try:
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None:
                name = member.display_name
            else:
                row = await get_dj_by_discord_id(db, user_id)
                name = row["dj_name"] if row is not None else ""
            if await sync_dj(ctx, user_id, name):
                done += 1
        except Exception:
            logger.warning("Retry sync failed for DJ %s", user_id, exc_info=True)
    return done
