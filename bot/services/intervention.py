"""Intervention service -- Story 6.2.

Handles: FR32-33 -- DM proxy state, message forwarding logic.
Admin can take over a DJ's DM conversation and return control to the bot.

All state persists in the interventions PostgreSQL table -- service is stateless.

[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
[Source: architecture.md#Cog Responsibility Boundary]
"""
import logging
from typing import TYPE_CHECKING, Any

import disnake

from bot.db.interventions import end_intervention, start_intervention

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)


class InterventionService:
    """Stateless intervention proxy -- manages admin DM takeover state in PostgreSQL.

    Constructor follows the same pattern as OutreachService and RemindersService.
    [Source: bot/services/outreach.py -- constructor pattern]
    """

    def __init__(
        self,
        bot: Any,
        db: Any,
        log_service: "AdminLogService",
    ) -> None:
        self.bot = bot
        self.db = db
        self.log_service = log_service

    async def start(self, discord_user_id: int, dj_name: str) -> None:
        """Mark DJ as under intervention and log to admin channel.

        Inserts a row in the interventions table (idempotent).
        Posts an intervention log entry so admins know intervention began.

        [Source: epics.md#Story 6.2 AC1]
        """
        await start_intervention(self.db, discord_user_id)
        if self.log_service is not None:
            await self.log_service.post_intervention(
                sender="ADMIN",
                recipient=f"@{dj_name}",
                content="Intervention started -- automated DMs paused.",
            )
        logger.info(
            "Intervention started for DJ %s (user_id=%s)", dj_name, discord_user_id
        )

    async def send_message(
        self,
        discord_user_id: int,
        dj_name: str,
        message_text: str,
    ) -> bool:
        """Send a DM to the DJ on behalf of the admin and log it.

        Uses get_user (cache) then fetch_user (API) to obtain the Discord user.
        Catches Forbidden and NotFound gracefully -- never raises.

        Returns True if the DM was delivered, False otherwise (user not found or
        DMs blocked), so the caller can give the admin accurate feedback.

        [Source: epics.md#Story 6.2 AC2]
        """
        user = self.bot.get_user(discord_user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(discord_user_id)
            except disnake.NotFound:
                logger.warning(
                    "intervene send: user %s not found in Discord", discord_user_id
                )
                return False

        try:
            await user.send(message_text)
            if self.log_service is not None:
                await self.log_service.post_intervention(
                    sender="ADMIN",
                    recipient=f"@{dj_name}",
                    content=message_text,
                )
            logger.info(
                "Intervention message sent to DJ %s (user_id=%s)", dj_name, discord_user_id
            )
            return True
        except disnake.Forbidden:
            logger.error(
                "intervene send: DMs blocked for DJ %s (user_id=%s)",
                dj_name,
                discord_user_id,
            )
            return False

    async def end(self, discord_user_id: int, dj_name: str) -> None:
        """Remove intervention record and log resumption to admin channel.

        Deletes the row from the interventions table (no-op if not present).
        Posts an intervention log entry so admins know automated flows resumed.

        [Source: epics.md#Story 6.2 AC4]
        """
        await end_intervention(self.db, discord_user_id)
        if self.log_service is not None:
            await self.log_service.post_intervention(
                sender="BOT",
                recipient="ADMIN",
                content=f"Intervention ended for @{dj_name} -- automated flows resumed.",
            )
        logger.info(
            "Intervention ended for DJ %s (user_id=%s)", dj_name, discord_user_id
        )
