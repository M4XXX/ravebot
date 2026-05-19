"""Admin cog — slash commands for bot configuration (Story 1.4).

FR34: /config set dj-channel — configure DJ lineup post channel
FR35: /config set log-channel — configure admin log channel
FR41: All commands restricted to ADMIN_USER_IDS via cog_slash_command_check

Additional admin commands (FR31–33, FR36–41) are implemented in later stories.

disnake 2.12.0 slash command hierarchy:
  /config                   ← @commands.slash_command()
    set                     ← .sub_command_group()
      dj-channel            ← .sub_command()
      log-channel           ← .sub_command()

[Source: architecture.md#Admin Whitelist: disnake Check Decorator]
[Source: epics.md#Story 1.4 Acceptance Criteria]
"""
import logging

import disnake
from disnake.ext import commands

from bot.context import BotContext
from bot.db import config as db_config

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog):
    """Admin slash commands — whitelist-gated via cog_slash_command_check."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    def cog_slash_command_check(
        self, inter: disnake.ApplicationCommandInteraction
    ) -> bool:
        """Enforce admin whitelist for all slash commands in this cog.

        Reads admin_user_ids from Config (loaded from ADMIN_USER_IDS env var at startup).
        Returning False raises CheckFailure — handled in cog_slash_command_error.
        Note: disnake 2.12.0 defines cog_slash_command_check as a synchronous method.
        [Source: architecture.md#Admin Whitelist: disnake Check Decorator]
        [Source: epics.md#FR41, NFR6]
        """
        return inter.author.id in self.ctx.config.admin_user_ids

    async def cog_slash_command_error(
        self,
        inter: disnake.ApplicationCommandInteraction,
        error: Exception,
    ) -> None:
        """Respond with a permission denial when cog_slash_command_check returns False.

        Non-CheckFailure errors are logged and a generic ephemeral reply is sent
        so the user is never left without a response.
        [Source: epics.md#Story 1.4 AC3 — user sees "You don't have permission to use this command"]
        """
        if isinstance(error, commands.CheckFailure):
            await inter.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True,
            )
        else:
            logger.error(
                "Unexpected error in AdminCog slash command: %s", error, exc_info=True
            )
            if not inter.response.is_done():
                await inter.response.send_message(
                    "Something went wrong — please try again.",
                    ephemeral=True,
                )

    @commands.slash_command(name="config", description="Bot configuration commands (admin only)")
    async def config_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Top-level /config command group — entry point for all config sub-commands."""
        pass

    @config_base.sub_command_group(name="set", description="Set a configuration value")
    async def config_set_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Sub-command group for /config set <key>."""
        pass

    @config_set_base.sub_command(
        name="dj-channel",
        description="Set the channel where DJ lineup posts are sent",
    )
    async def config_set_dj_channel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(
            description="The DJ lineup channel"
        ),
    ) -> None:
        """Store dj_channel in the config table and confirm to the admin.

        Config key: "dj_channel" — used by lineup post logic (Story 3.2+).
        [Source: epics.md#FR34, Story 1.4 AC1]
        """
        guild_id = inter.guild_id
        assert guild_id is not None  # slash commands always have guild context
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_dj_channel")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "dj_channel", str(channel.id))
        await inter.response.send_message(
            f"DJ channel set to {channel.mention}", ephemeral=True
        )

    @config_set_base.sub_command(
        name="log-channel",
        description="Set the channel where admin log messages are sent",
    )
    async def config_set_log_channel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(
            description="The admin log channel"
        ),
    ) -> None:
        """Store log_channel in the config table and confirm to the admin.

        Config key: "log_channel" — read by AdminLogService._get_log_channel() on every post.
        Persistence across restarts is automatic: AdminLogService always queries DB live.
        [Source: epics.md#FR35, Story 1.4 AC2]
        [Source: bot/log.py — _get_log_channel() reads 'log_channel' from config table]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_log_channel")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "log_channel", str(channel.id))
        await inter.response.send_message(
            f"Log channel set to {channel.mention}", ephemeral=True
        )
