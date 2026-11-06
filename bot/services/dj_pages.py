"""Make sure a DJ has a row in the Notion DJs database."""
import logging

import asyncpg

from bot.db.djs import (
    get_dj_by_discord_id,
    get_notion_dj_page_id,
    set_notion_dj_page_id,
)
from bot.notion.client import NotionClient, NotionWriteError

logger = logging.getLogger(__name__)


async def ensure_dj_page(
    db: asyncpg.Pool,
    notion: NotionClient,
    discord_id: int,
    discord_name: str,
) -> str | None:
    """Return the DJ's Notion page id, creating the row from the djs table if needed.

    Returns None (never raises) if the DJ has no djs row or Notion fails.
    """
    stored = await get_notion_dj_page_id(db, discord_id)
    if stored:
        return stored
    row = await get_dj_by_discord_id(db, discord_id)
    if row is None:
        logger.warning("No djs row for user %s; cannot create Notion DJ page", discord_id)
        return None
    try:
        page_id = await notion.upsert_dj(
            discord_id,
            row["dj_name"],
            discord_name,
            vrchat_name=row["vrchat_name"],
            logo_url=row["logo_url"],
            link=row["stream_url_primary"],
            alt_link=row["stream_url_backup"],
        )
    except NotionWriteError:
        logger.warning("Notion DJ page upsert failed for user %s", discord_id, exc_info=True)
        return None
    await set_notion_dj_page_id(db, discord_id, page_id)
    return page_id
