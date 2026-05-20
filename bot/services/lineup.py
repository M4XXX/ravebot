"""Lineup service — daily-capped lineup post to DJ channel.

Handles FR10 — lineup post construction, daily cap, and edit-in-place deduplication.

[Source: epics.md#Story 3.2]
[Source: architecture.md#Repository Pattern]
"""
import datetime
import logging
from typing import TYPE_CHECKING, Any

import disnake

from bot.db.config import get_value, set_value
from bot.ui.embeds import ErrorCode, MissionRosterEmbed

if TYPE_CHECKING:
    from bot.log import AdminLogService

logger = logging.getLogger(__name__)


class LineupService:
    """Stateful lineup post manager — posts MissionRosterEmbed to DJ channel."""

    def __init__(
        self,
        bot: Any,
        db: Any,
        log_service: "AdminLogService",
    ) -> None:
        self.bot = bot
        self.db = db
        self.log_service = log_service

    def _get_guild_id(self) -> int | None:
        if not self.bot.guilds:
            logger.warning("LineupService._get_guild_id: no guilds — skipping")
            return None
        return self.bot.guilds[0].id

    async def post_update(self, events: list[dict]) -> None:
        """Post (or edit) the lineup embed to the DJ channel.

        Daily cap: at most one post per calendar day (UTC).
        Edit-in-place: re-uses the existing message if available.
        Falls back to delete-and-repost on NotFound/Forbidden.

        [Source: epics.md#Story 3.2 AC1-AC4]
        """
        guild_id = self._get_guild_id()
        if guild_id is None:
            return

        # AC4 — channel must be configured
        dj_channel_id = await get_value(self.db, guild_id, "dj_channel")
        if dj_channel_id is None:
            logger.warning("LineupService: dj_channel not configured — cannot post lineup")
            if self.log_service is not None:
                await self.log_service.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger="Lineup update triggered but dj_channel is not configured.",
                    action_taken="Lineup post skipped.",
                    manual_action="Run /set-dj-channel to configure the DJ channel.",
                )
            return

        # AC2 — daily cap
        today = datetime.date.today().isoformat()
        last_post_date = await get_value(self.db, guild_id, "lineup_last_post_date")
        if last_post_date == today:
            logger.info("LineupService: lineup already posted today — skipping")
            return

        channel = self.bot.get_channel(int(dj_channel_id))
        if channel is None:
            logger.warning("LineupService: channel %s not found in cache", dj_channel_id)
            return

        embed = MissionRosterEmbed(events)

        # AC3 — edit-in-place or delete-and-repost
        msg_id_str = await get_value(self.db, guild_id, "lineup_post_message_id")
        sent_message = None
        if msg_id_str is not None:
            try:
                existing = await channel.fetch_message(int(msg_id_str))
            except (disnake.NotFound, disnake.Forbidden):
                existing = None  # message gone or inaccessible — just send new

            if existing is not None:
                try:
                    await existing.edit(embed=embed)
                    sent_message = existing
                except (disnake.NotFound, disnake.Forbidden):
                    # Edit failed — delete old message then fall through to send
                    try:
                        await existing.delete()
                    except (disnake.NotFound, disnake.Forbidden):
                        pass  # already gone or no delete permission — acceptable

        if sent_message is None:
            sent_message = await channel.send(embed=embed)

        # AC1 — persist state
        await set_value(self.db, guild_id, "lineup_last_post_date", today)
        await set_value(self.db, guild_id, "lineup_post_message_id", str(sent_message.id))
