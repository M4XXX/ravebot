"""DJ pages in Notion at booking time (classic layout).

A DJ without a stored page ID (onboarded before Story 4.2, or whose onboarding write
failed) gets one found or created when booked, so the booking appears in the event's DJs.

[Source: epics.md#Story 8.3]
"""
import logging
from typing import TYPE_CHECKING

from bot.db.djs import set_notion_dj_page_id

if TYPE_CHECKING:
    import asyncpg

    from bot.notion.client import NotionClient

logger = logging.getLogger(__name__)


async def ensure_and_store(
    notion_client: "NotionClient",
    db: "asyncpg.Pool",
    discord_user_id: int,
    dj_name: str,
    event_name: str,
) -> str | None:
    """The DJ's Notion page ID, found or created, and stored in djs. Never raises.

    None if there is none; NotionClient.ensure_dj_page() has then posted ERR_NOTION_WRITE.
    A failure to store the ID is only logged: this booking still links the page, and the
    next one finds it again by Discord ID.
    """
    page_id = await notion_client.ensure_dj_page(discord_user_id, dj_name, event_name)
    if page_id is not None:
        try:
            await set_notion_dj_page_id(db, discord_user_id, page_id)
        except Exception:
            logger.exception("Could not store the Notion page ID of DJ %s", discord_user_id)
    return page_id
