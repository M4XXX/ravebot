"""Shared helper for sending a direct message to a Discord user."""
import logging
from typing import TYPE_CHECKING

import disnake

if TYPE_CHECKING:
    from disnake.ext import commands

logger = logging.getLogger(__name__)


async def dm_user(bot: "commands.Bot", user_id: int, text: str) -> bool:
    """DM the user; True on success. Never raises: a failed DM is non-fatal."""
    try:
        user = bot.get_user(user_id)
        if user is None:
            user = await bot.fetch_user(user_id)
        await user.send(text)
    except disnake.NotFound:
        logger.warning("DM: user %s not found", user_id)
        return False
    except disnake.Forbidden:
        logger.warning("DM: DMs blocked for user %s", user_id)
        return False
    except Exception:
        logger.exception("DM: unexpected error for user %s", user_id)
        return False
    return True
