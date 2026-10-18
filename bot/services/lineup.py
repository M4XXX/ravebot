"""Lineup service — daily-capped lineup post to DJ channel.

Handles FR10 — lineup post construction, daily cap, and edit-in-place deduplication.

[Source: epics.md#Story 3.2]
[Source: architecture.md#Repository Pattern]
"""
import datetime
import logging
from typing import TYPE_CHECKING, Any

import disnake

from bot import guild as served
from bot import loops, slots
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
        guild_id = served.resolve(self.bot)
        if guild_id is None:
            logger.warning("LineupService._get_guild_id: no served guild — skipping")
        return guild_id

    async def post_update(self, events: list[dict]) -> bool:
        """Post (or edit) the lineup embed to the DJ channel.

        Returns True when the channel shows the current lineup, False when it could not be
        updated (not configured, channel missing, permissions, or the daily cap on new posts).
        The caller keeps its change snapshot only on True, so the update is retried.

        An existing lineup message is always edited in place. The daily cap applies only
        to sending a NEW message (when there is none or it can no longer be edited).
        """
        guild_id = self._get_guild_id()
        if guild_id is None:
            return False

        events = slots.active(events)  # nights called off in Notion are left out
        # AC4 — channel must be configured
        await slots.annotate(self.db, guild_id, events)
        dj_channel_id = await get_value(self.db, guild_id, "dj_channel")
        if dj_channel_id is None:
            logger.warning("LineupService: dj_channel not configured — cannot post lineup")
            # every poll cycle retries the post; tell the organizers once an hour, not each time
            if self.log_service is not None and loops.alert_due(f"lineup:no_channel:{guild_id}"):
                await self.log_service.post_error(
                    error_code=ErrorCode.ERR_NOTION_WRITE,
                    trigger="Lineup update triggered but dj_channel is not configured.",
                    action_taken="Lineup post skipped.",
                    manual_action="Run /config set dj-channel to configure the DJ channel.",
                )
            return False

        channel = self.bot.get_channel(int(dj_channel_id))
        if channel is None:
            logger.warning("LineupService: channel %s not found in cache", dj_channel_id)
            if self.log_service is not None and loops.alert_due(f"lineup:gone:{guild_id}"):
                await self.log_service.post_error(
                    error_code=ErrorCode.ERR_BACKGROUND_TASK,
                    trigger=f"Lineup channel {dj_channel_id} not found (deleted, or no access).",
                    action_taken="Lineup post skipped; retried every poll cycle.",
                    manual_action="Run /config set dj-channel, or give the bot View Channel.",
                )
            return False

        embed = MissionRosterEmbed(events)
        today = datetime.date.today().isoformat()

        # AC3 — edit-in-place (no daily cap)
        msg_id_str = await get_value(self.db, guild_id, "lineup_post_message_id")
        if msg_id_str is not None:
            try:
                existing = await channel.fetch_message(int(msg_id_str))
            except (disnake.NotFound, disnake.Forbidden):
                existing = None  # message gone or inaccessible — a new one is needed
            if existing is not None:
                try:
                    await existing.edit(embed=embed)
                    return True
                except (disnake.NotFound, disnake.Forbidden):
                    # Edit failed — delete old message then fall through to send
                    try:
                        await existing.delete()
                    except (disnake.NotFound, disnake.Forbidden):
                        pass  # already gone or no delete permission — acceptable

        # AC2 — daily cap applies to new messages only
        if await get_value(self.db, guild_id, "lineup_last_post_date") == today:
            logger.info("LineupService: a new lineup post was already sent today — retry later")
            return False
        try:
            sent_message = await channel.send(embed=embed)
        except disnake.HTTPException as exc:
            logger.warning("LineupService: could not post the lineup: %s", exc)
            return False

        # AC1 — persist state
        await set_value(self.db, guild_id, "lineup_last_post_date", today)
        await set_value(self.db, guild_id, "lineup_post_message_id", str(sent_message.id))
        return True
