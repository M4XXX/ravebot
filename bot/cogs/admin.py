"""Admin cog — slash commands for bot configuration, intervention, and booking override.

FR34: /config set dj-channel — configure DJ lineup post channel
FR35: /config set log-channel — configure admin log channel
FR32: /intervene start -- take over bot DM with a DJ
FR33: /intervene send -- send message to DJ as bot during intervention
FR33: /intervene end -- return DM control to the bot
Story 6.3: /admin book -- force-book a DJ into an event slot
Story 6.3: /admin cancel -- force-cancel a DJ's event booking
Story 6.4: /admin dj set-active -- set a DJ's active booking status
FR41: All commands restricted to ADMIN_USER_IDS via cog_slash_command_check

disnake 2.12.0 slash command hierarchy:
  /config                   <- @commands.slash_command()
    set                     <- .sub_command_group()
      dj-channel            <- .sub_command()
      log-channel           <- .sub_command()
  /intervene                <- @commands.slash_command()
    start                   <- .sub_command()
    send                    <- .sub_command()
    end                     <- .sub_command()
  /admin                    <- @commands.slash_command()
    book                    <- .sub_command()
    cancel                  <- .sub_command()
    dj                      <- .sub_command_group()
      set-active            <- .sub_command()

[Source: architecture.md#Admin Whitelist: disnake Check Decorator]
[Source: epics.md#Story 1.4 Acceptance Criteria]
[Source: epics.md#Story 6.2 Acceptance Criteria]
[Source: epics.md#Story 6.3 Acceptance Criteria]
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

    # ---------------------------------------------------------------------------
    # Story 6.2: /intervene command group — admin DM takeover (FR32, FR33)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        name="intervene", description="Admin DM intervention commands (admin only)"
    )
    async def intervene_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Top-level /intervene command group — entry point for all intervention sub-commands.

        [Source: epics.md#Story 6.2 -- FR32, FR33]
        """
        pass

    @intervene_base.sub_command(
        name="start", description="Take over the bot's DM conversation with a DJ"
    )
    async def intervene_start(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to intervene with"),
    ) -> None:
        """Pause automated DMs to the DJ and activate admin intervention mode.

        Inserts a row in the interventions table via InterventionService.
        All outreach, reminders, and flow prompts are suppressed until /intervene end.

        [Source: epics.md#Story 6.2 AC1]
        [Source: bot/services/intervention.py -- InterventionService.start()]
        """
        if self.ctx.intervention_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await self.ctx.intervention_service.start(dj.id, dj.display_name)
        await inter.response.send_message(
            f"Intervention active for {dj.mention}. All automated DMs paused. "
            "Use `/intervene send` to message them.",
            ephemeral=True,
        )

    @intervene_base.sub_command(
        name="send", description="Send a message to a DJ as the bot during intervention"
    )
    async def intervene_send(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="Target DJ"),
        message: str = commands.Param(description="Message to send to the DJ"),
    ) -> None:
        """Forward a message to the DJ's DM as if it were a bot message.

        Logs the sent message to the admin channel as [ADMIN -> @DJName] content.

        [Source: epics.md#Story 6.2 AC2]
        [Source: bot/services/intervention.py -- InterventionService.send_message()]
        """
        if self.ctx.intervention_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        sent = await self.ctx.intervention_service.send_message(dj.id, dj.display_name, message)
        if sent:
            await inter.response.send_message(
                f"Message sent to {dj.mention}.", ephemeral=True
            )
        else:
            await inter.response.send_message(
                f"Could not deliver message to {dj.mention} — DMs may be blocked.",
                ephemeral=True,
            )

    @intervene_base.sub_command(
        name="end", description="Return DM control to the bot for a DJ"
    )
    async def intervene_end(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to stop intervening with"),
    ) -> None:
        """Remove the intervention record and resume normal automated behaviour.

        Deletes the row from the interventions table via InterventionService.

        [Source: epics.md#Story 6.2 AC4]
        [Source: bot/services/intervention.py -- InterventionService.end()]
        """
        if self.ctx.intervention_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await self.ctx.intervention_service.end(dj.id, dj.display_name)
        await inter.response.send_message(
            f"Intervention ended for {dj.mention} — automated flows resumed.",
            ephemeral=True,
        )

    # ---------------------------------------------------------------------------
    # Story 6.3: /admin command group — booking & cancellation override
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        name="admin", description="Admin booking management commands (admin only)"
    )
    async def admin_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Top-level /admin command group — entry point for booking override sub-commands.

        [Source: epics.md#Story 6.3]
        """
        pass

    @admin_base.sub_command(
        name="book", description="Force-book a DJ into an event slot (bypasses confirmation)"
    )
    async def admin_book(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to book"),
        event: str = commands.Param(description="Event name (must match Notion, case-insensitive)"),
        slot: int = commands.Param(ge=1, description="Slot number (1 = first slot of event)"),
    ) -> None:
        """Force-book a DJ into a slot, writing directly to Notion without confirmation.

        Uses AdminBookingService.book_slot_override().
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.3 AC1, AC4]
        [Source: bot/services/admin_booking.py -- AdminBookingService.book_slot_override()]
        """
        if self.ctx.admin_booking_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        success, msg = await self.ctx.admin_booking_service.book_slot_override(
            admin_name=inter.author.name,
            dj_id=dj.id,
            dj_name=dj.display_name,
            event_name=event,
            slot_num=slot,
        )
        if success:
            await inter.response.send_message(
                f"Done — {dj.mention} booked into slot {slot} of '{event}'.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(msg, ephemeral=True)

    @admin_base.sub_command(
        name="cancel", description="Force-cancel a DJ's event booking"
    )
    async def admin_cancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ whose booking to cancel"),
        event: str = commands.Param(description="Event name (must match Notion, case-insensitive)"),
        slot: int = commands.Param(ge=1, description="Slot number (1 = first slot of event)"),
    ) -> None:
        """Force-cancel a DJ's booking, removing it from Notion and slot_locks.

        Uses AdminBookingService.cancel_slot_override().
        Returns 'No booking found' if the slot is not held by this DJ (AC3).
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.3 AC2, AC3, AC4]
        [Source: bot/services/admin_booking.py -- AdminBookingService.cancel_slot_override()]
        """
        if self.ctx.admin_booking_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        success, msg = await self.ctx.admin_booking_service.cancel_slot_override(
            admin_name=inter.author.name,
            dj_id=dj.id,
            dj_name=dj.display_name,
            event_name=event,
            slot_num=slot,
        )
        if success:
            await inter.response.send_message(
                f"Done — {dj.mention}'s booking in slot {slot} of '{event}' cancelled.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(msg, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.4: /admin dj sub-command-group — DJ status management
    # ---------------------------------------------------------------------------

    @admin_base.sub_command_group(
        name="dj", description="DJ status management (admin only)"
    )
    async def admin_dj(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Sub-command group for /admin dj <action>.

        [Source: epics.md#Story 6.4]
        """
        pass

    @admin_dj.sub_command(
        name="set-active",
        description="Set a DJ's active booking pool status",
    )
    async def admin_dj_set_active(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to update"),
        active: bool = commands.Param(
            description="true = add to booking pool, false = remove"
        ),
    ) -> None:
        """Set active_booking in DB and sync the Active Booking Discord role.

        Uses AdminBookingService.set_dj_active_status().
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.4 AC1, AC2, AC3, AC4]
        [Source: bot/services/admin_booking.py -- AdminBookingService.set_dj_active_status()]
        """
        if self.ctx.admin_booking_service is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        success, msg = await self.ctx.admin_booking_service.set_dj_active_status(
            inter.guild_id,
            dj.id,
            dj.display_name,
            active,
        )
        if success:
            if active:
                reply = f"{dj.mention} is now active in the booking pool."
            else:
                reply = f"{dj.mention} has been removed from the booking pool."
            await inter.response.send_message(reply, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
