"""AdminLogService — centralised error routing and DJ interaction logging.

All errors surface to the admin log channel within 60 seconds (NFR11).
All 6 error codes are routed through post_error().
DJ interaction transcripts are posted through post_interaction().
Admin intervention messages are posted through post_intervention().

If no log channel is configured, all posts fall back to stdout (warning level).
Never raises — errors in the log service must not crash the bot.

[Source: architecture.md#Error Routing: Centralized AdminLogService]
[Source: epics.md#FR47, NFR11]
"""
import logging

import asyncpg
import disnake
from disnake.ext import commands

from bot import guild as served
from bot.db import config as db_config
from bot.ui.embeds import AdminLogEmbed, ErrorCode, ErrorEmbed

logger = logging.getLogger(__name__)


class AdminLogService:
    """Posts structured embeds to the configured admin log channel.

    The log channel ID is looked up dynamically on each post from the config
    table — so it works correctly whether called before or after Story 1.4
    configures the channel. Falls back to stdout logging if unconfigured.

    [Source: architecture.md#Inter-Component Communication: Direct Calls via Shared BotContext]
    """

    def __init__(self, bot: commands.Bot, db: asyncpg.Pool) -> None:
        self._bot = bot
        self._db = db

    async def _get_log_channel(self) -> disnake.TextChannel | None:
        """Look up the configured admin log channel.

        Queries the config table for key='log_channel' using the first guild the
        bot is in (single-server bot assumption). Returns None if:
          - bot has no guilds yet (pre-on_ready)
          - 'log_channel' key not set in config table
          - stored value is not a valid integer
          - channel not found or not a TextChannel
        """
        guild_id = served.resolve(self._bot)
        if guild_id is None:
            return None
        try:
            raw = await db_config.get_value(self._db, guild_id, "log_channel")
        except Exception as exc:  # asyncpg errors when DB is unavailable
            logger.warning("Failed to read log_channel from config table: %s", exc)
            return None
        if raw is None:
            return None
        try:
            channel_id = int(raw)
        except ValueError:
            logger.warning("Invalid log_channel value in config table: %r", raw)
            return None
        channel = self._bot.get_channel(channel_id)
        if not isinstance(channel, disnake.TextChannel):
            return None
        return channel

    async def post_error(
        self,
        error_code: ErrorCode,
        trigger: str,
        action_taken: str,
        manual_action: str = "No manual action required.",
    ) -> None:
        """Post an ErrorEmbed to the admin log channel.

        Falls back to a stdout warning if no channel is configured (e.g. before
        Story 1.4 adds the /config set log-channel command).
        Never raises — log failures must not crash the bot.

        [Source: epics.md#Story 1.3 AC3]
        [Source: epics.md#NFR11 — all errors surfaced within 60 seconds]
        """
        embed = ErrorEmbed(
            error_code=error_code,
            trigger=trigger,
            action_taken=action_taken,
            manual_action=manual_action,
        )
        channel = await self._get_log_channel()
        if channel is None:
            logger.warning(
                "[%s] trigger=%r action_taken=%r manual=%r",
                error_code,
                trigger,
                action_taken,
                manual_action,
            )
            return
        try:
            await channel.send(embed=embed)
        except disnake.DiscordException as exc:
            logger.error("Failed to post error embed to log channel: %s", exc)

    async def post_interaction(
        self,
        dj_name: str,
        action_type: str,
        outcome: str,
        event: str | None = None,
    ) -> None:
        """Post an AdminLogEmbed for a DJ interaction event.

        Called after every meaningful bot↔DJ interaction: DM sent/received,
        button tapped, booking confirmed, etc.
        Falls back to stdout warning if no channel configured.

        [Source: epics.md#Story 1.3 AC4]
        [Source: epics.md#FR31 — every interaction mirrored to admin log]
        """
        embed = AdminLogEmbed(
            dj_name=dj_name,
            action_type=action_type,
            outcome=outcome,
            event=event,
        )
        channel = await self._get_log_channel()
        if channel is None:
            logger.warning(
                "AdminLog [%s \u2192 %s]: outcome=%r",
                dj_name,
                action_type,
                outcome,
            )
            return
        try:
            await channel.send(embed=embed)
        except disnake.DiscordException as exc:
            logger.error("Failed to post interaction embed to log channel: %s", exc)

    async def post_intervention(
        self,
        sender: str,
        recipient: str,
        content: str,
    ) -> None:
        """Post an intervention transcript entry to the admin log channel.

        Format: "**{sender} → {recipient}:** {content}"
        Plain text (not embed) — keeps the transcript scannable.
        Used by Story 6.2 /intervene flows.

        [Source: epics.md#FR31, Story 6.2 AC]
        """
        message = f"**{sender} \u2192 {recipient}:** {content}"
        channel = await self._get_log_channel()
        if channel is None:
            logger.warning("Intervention log: %s", message)
            return
        try:
            await channel.send(message)
        except disnake.DiscordException as exc:
            logger.error("Failed to post intervention log: %s", exc)

    async def post_admin_override(self, content: str) -> None:
        """Post an admin override action line to the admin log channel.

        Caller constructs the full string, e.g.:
          "[ADMIN OVERRIDE] @AdminName booked @DJName into EventName at <t:...:f>"
          "[ADMIN OVERRIDE] @AdminName cancelled @DJName from EventName at <t:...:f>"

        Plain text (not embed) — consistent with post_dm_transcript and post_intervention.
        Never raises.

        [Source: epics.md#Story 6.3 AC1, AC2]
        """
        channel = await self._get_log_channel()
        if channel is None:
            logger.warning("Admin override log (no channel): %s", content)
            return
        try:
            await channel.send(content)
        except disnake.DiscordException as exc:
            logger.error("Failed to post admin override log: %s", exc)

    async def post_dm_transcript(
        self,
        dj_name: str,
        direction: str,
        content: str,
    ) -> None:
        """Post a raw DM transcript entry to the admin log channel.

        Formats:
          direction="BOT" → "[BOT → @{dj_name}] {content}"
          direction="DJ"  → "[@{dj_name} → BOT] {content}"

        Plain text (not embed) — keeps the transcript scannable.
        Falls back to stdout warning if no channel is configured.
        Never raises.

        [Source: epics.md#Story 6.1 AC1–AC4]
        [Source: epics.md#FR31 — every interaction mirrored to admin log]
        """
        if direction == "BOT":
            entry = f"[BOT \u2192 @{dj_name}] {content}"
        else:
            entry = f"[@{dj_name} \u2192 BOT] {content}"
        channel = await self._get_log_channel()
        if channel is None:
            logger.warning("DM transcript (no log channel): %s", entry)
            return
        try:
            await channel.send(entry)
        except disnake.DiscordException as exc:
            logger.error("Failed to post DM transcript to log channel: %s", exc)
