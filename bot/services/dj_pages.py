"""Make sure a DJ has a live row in the Notion DJs database."""
import logging

import asyncpg

from bot.db.djs import (
    get_dj_by_discord_id,
    get_notion_dj_page_id,
    set_notion_dj_page_id,
)
from bot.notion.client import NotionClient

logger = logging.getLogger(__name__)


async def ensure_dj_page(
    db: asyncpg.Pool,
    notion: NotionClient,
    discord_id: int,
    discord_name: str,
) -> str | None:
    """Return the DJ's Notion page id, creating the row from the djs table if needed.

    A stored id is checked first (``dj_page_alive``): a live page gets its
    ``Discord name`` refreshed (best effort) and is returned; a deleted or
    trashed page is replaced by a fresh upsert whose id is stored.
    Returns None (never raises) if the DJ has no djs row or anything fails.
    """
    try:
        return await _ensure(db, notion, discord_id, discord_name)
    except Exception:
        logger.warning("ensure_dj_page failed for user %s", discord_id, exc_info=True)
        return None


async def _ensure(
    db: asyncpg.Pool,
    notion: NotionClient,
    discord_id: int,
    discord_name: str,
) -> str | None:
    stored = await get_notion_dj_page_id(db, discord_id)
    if stored:
        if await notion.dj_page_alive(stored):
            if discord_name:
                try:
                    await notion.refresh_discord_name(stored, discord_name)
                except Exception:
                    logger.warning(
                        "Refreshing the Discord name of DJ page %s failed", stored,
                        exc_info=True,
                    )
            return stored
        logger.info(
            "Stored Notion DJ page %s of user %s is gone; creating a new one",
            stored, discord_id,
        )
    row = await get_dj_by_discord_id(db, discord_id)
    if row is None:
        logger.warning("No djs row for user %s; cannot create Notion DJ page", discord_id)
        return None
    page_id = await notion.upsert_dj(
        discord_id,
        row["dj_name"],
        discord_name,
        vrchat_name=row["vrchat_name"],
        logo_url=row["logo_url"],
        link=row["stream_url_primary"],
        alt_link=row["stream_url_backup"],
    )
    await set_notion_dj_page_id(db, discord_id, page_id)
    return page_id
