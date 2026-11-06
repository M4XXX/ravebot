"""AdminLogService — centralised error routing and DJ interaction logging.

All errors surface to the admin log channel within 60 seconds (NFR11).
All 6 error codes are routed through post_error().
Admin intervention messages are posted through post_intervention().

If no log channel is configured, all posts fall back to stdout (warning level).
Never raises — errors in the log service must not crash the bot.

[Source: architecture.md#Error Routing: Centralized AdminLogService]
[Source: epics.md#FR47, NFR11]
"""
import logging
from collections.abc import Callable
from typing import Literal

import asyncpg
import disnake
from disnake.ext import commands

from bot.db import config as db_config
from bot.notion.models import EventInfo
from bot.ui import texts
from bot.ui.embeds import (
    EmbedColors,
    ErrorCode,
    LogEntryEmbed,
    _safe_dj_name,
)

logger = logging.getLogger(__name__)

# ERR_SLOT_CONFLICT and ERR_BUTTON_TIMEOUT are deliberately absent: logger only.
_ERROR_TITLES: dict[ErrorCode, str] = {
    ErrorCode.ERR_DM_FAILED: "Could not DM a DJ",
    ErrorCode.ERR_NOTION_WRITE: "Notion write failed",
    ErrorCode.ERR_DB_UNAVAILABLE: "Database unavailable",
    ErrorCode.ERR_OUTREACH_EXHAUSTED: "No more DJs to ask",
}


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
        if not self._bot.guilds:
            return None
        guild_id = self._bot.guilds[0].id
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
        title = _ERROR_TITLES.get(error_code)
        if title is None:
            logger.warning(
                "[%s] trigger=%r action_taken=%r manual=%r",
                error_code,
                trigger,
                action_taken,
                manual_action,
            )
            return
        what = texts.escape_text(trigger.strip())
        action = texts.escape_text(action_taken.strip())
        if action:
            if what and what[-1] not in ".!?":
                what += "."
            what = f"{what} {action}".strip()
        todo = None if manual_action == texts.LOG_NO_MANUAL_ACTION else manual_action
        await self.log_attention(title, what, todo)

    # --- Typed entries -------------------------------------------------

    async def _send(
        self, embed: disnake.Embed, view: disnake.ui.View | None = None
    ) -> disnake.Message | None:
        """Post one entry. Never raises; falls back to the logger."""
        try:
            channel = await self._get_log_channel()
            if channel is None:
                logger.warning(
                    "Log entry (no log channel): %s | %s", embed.title, embed.description
                )
                return None
            kwargs: dict = {
                "embed": embed,
                "allowed_mentions": disnake.AllowedMentions.none(),
            }
            if view is not None:
                kwargs["view"] = view
            return await channel.send(**kwargs)
        except Exception as exc:
            logger.error("Failed to post log entry %r: %s", embed.title, exc)
            return None

    async def _post_built(
        self,
        build: Callable[[], disnake.Embed],
        view: disnake.ui.View | None = None,
    ) -> disnake.Message | None:
        """Build an entry and post it. Never raises, not even when building fails."""
        try:
            embed = build()
        except Exception:
            logger.exception("Failed to build a log entry")
            return None
        return await self._send(embed, view)

    async def log_booked(
        self,
        dj_name: str,
        event: EventInfo,
        slot_index: int,
        via: Literal["DM", "Notion"],
    ) -> None:
        await self._post_built(
            lambda: LogEntryEmbed(
                texts.LOG_BOOKED_TITLE,
                texts.log_booked_text(_safe_dj_name(dj_name), event, slot_index, via),
                EmbedColors.SUCCESS_GREEN,
            )
        )

    async def log_cancelled(self, dj_name: str, event: EventInfo, slot_index: int) -> None:
        await self._post_built(
            lambda: LogEntryEmbed(
                texts.LOG_CANCELLED_TITLE,
                texts.log_cancelled_text(_safe_dj_name(dj_name), event, slot_index),
                EmbedColors.MISSION_GOLD,
            )
        )

    async def log_changed_by_organizer(
        self,
        organizer: str,
        action: Literal["booked", "removed"],
        dj_name: str,
        event: EventInfo,
        slot_index: int,
    ) -> None:
        await self._post_built(
            lambda: LogEntryEmbed(
                texts.LOG_ORGANIZER_TITLE,
                texts.log_organizer_text(
                    _safe_dj_name(organizer),
                    action,
                    _safe_dj_name(dj_name),
                    event,
                    slot_index,
                ),
                EmbedColors.MISSION_GOLD,
            )
        )

    async def log_changed_in_notion(
        self, dj_name: str, event: EventInfo, slot_index: int
    ) -> None:
        await self._post_built(
            lambda: LogEntryEmbed(
                texts.LOG_NOTION_TITLE,
                texts.log_notion_text(_safe_dj_name(dj_name), event, slot_index),
                EmbedColors.MISSION_GOLD,
            )
        )

    async def log_fully_booked(self, event: EventInfo) -> None:
        await self._post_built(
            lambda: LogEntryEmbed(
                texts.LOG_FULLY_BOOKED_TITLE,
                texts.log_fully_booked_text(event),
                EmbedColors.SUCCESS_GREEN,
            )
        )

    async def log_new_dj(self, dj_name: str, vrchat_name: str | None) -> None:
        def build() -> disnake.Embed:
            vrchat = _safe_dj_name(vrchat_name) if vrchat_name else None
            return LogEntryEmbed(
                texts.LOG_NEW_DJ_TITLE,
                texts.log_new_dj_text(_safe_dj_name(dj_name), vrchat),
                EmbedColors.SUCCESS_GREEN,
            )

        await self._post_built(build)

    async def log_attention(self, title: str, what: str, todo: str | None = None) -> None:
        await self._post_built(lambda: LogEntryEmbed(title, what, EmbedColors.IMPERIAL_RED, todo))

    async def log_user_message(
        self,
        user: disnake.abc.User,
        text: str,
        attachment_names: list[str],
        dj_name: str | None,
        view: disnake.ui.View | None = None,
    ) -> disnake.Message | None:
        def build() -> disnake.Embed:
            display = _safe_dj_name(getattr(user, "display_name", None) or user.name)
            dj = _safe_dj_name(dj_name) if dj_name else None
            body = texts.log_message_text(
                texts.escape_text(text),
                [texts.escape_text(name) for name in attachment_names],
            )
            return LogEntryEmbed(
                texts.log_message_title(display, dj),
                body,
                EmbedColors.DEEP_SPACE_BLUE,
            )

        return await self._post_built(build, view)

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
