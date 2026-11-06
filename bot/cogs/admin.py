"""Admin cog — slash commands for bot configuration, intervention, and booking override.

FR34: /config set dj-channel — configure DJ lineup post channel
FR35: /config set log-channel — configure admin log channel
FR32: /intervene start -- take over bot DM with a DJ
FR33: /intervene send -- send message to DJ as bot during intervention
FR33: /intervene end -- return DM control to the bot
Story 6.3: /admin book -- force-book a DJ into an event slot
Story 6.3: /admin cancel -- force-cancel a DJ's event booking
Story 6.4: /admin dj set-active -- set a DJ's active booking status
Story 6.5: /status -- show next event slot status (FR38)
Story 6.5: /list-djs -- list all registered DJs (FR39)
Story 7.1: /add-event -- manually create a Notion event (FR40)
Story 7.2: /config set outreach-days-before -- outreach window (FR36)
Story 7.2: /config set outreach-cooldown-hours -- outreach cooldown (FR36)
Story 7.2: /config set lookahead-count -- event look-ahead (FR37)
Story 7.2: /config set outreach-max -- max outreach contacts (FR36)
Story 7.2: /config show -- display all config values
Story 7.3: /config set slots-per-event -- slots per auto-created event (FR37)
Story 7.3: /config set event-interval-days -- days between auto-created events (FR37)
Story 7.3: /config set slot-duration -- slot duration in minutes (FR37)
Story 7.3: /config set start-time -- default event start time HH:MM UTC (FR37)
Story 7.3: /config set event-schedule -- event recurrence cron expression (FR37)
/config set outreach -- global switch for automatic outreach invites
FR41: All commands restricted to ADMIN_USER_IDS via cog_slash_command_check

disnake 2.12.0 slash command hierarchy:
  /config                      <- @commands.slash_command()
    set                        <- .sub_command_group()
      dj-channel               <- .sub_command()
      log-channel              <- .sub_command()
      outreach-days-before     <- .sub_command()
      outreach-cooldown-hours  <- .sub_command()
      lookahead-count          <- .sub_command()
      outreach-max             <- .sub_command()
      slots-per-event          <- .sub_command()  [Story 7.3]
      event-interval-days      <- .sub_command()  [Story 7.3]
      slot-duration            <- .sub_command()  [Story 7.3]
      start-time               <- .sub_command()  [Story 7.3]
      event-schedule           <- .sub_command()  [Story 7.3]
      outreach                 <- .sub_command()  [enabled: bool]
    show                       <- .sub_command()
  /intervene                   <- @commands.slash_command()
    start                      <- .sub_command()
    send                       <- .sub_command()
    end                        <- .sub_command()
  /admin                       <- @commands.slash_command()
    book                       <- .sub_command()
    cancel                     <- .sub_command()
    dj                         <- .sub_command_group()
      set-active               <- .sub_command()
  /status                      <- @commands.slash_command()
  /list-djs                    <- @commands.slash_command()
  /add-event                   <- @commands.slash_command()
  /lineup                      <- @commands.slash_command()
    post                       <- .sub_command()  [optional event, autocomplete]

[Source: architecture.md#Admin Whitelist: disnake Check Decorator]
[Source: epics.md#Story 1.4 Acceptance Criteria]
[Source: epics.md#Story 6.2 Acceptance Criteria]
[Source: epics.md#Story 6.3 Acceptance Criteria]
[Source: epics.md#Story 6.5 Acceptance Criteria]
[Source: epics.md#Story 7.1 Acceptance Criteria]
[Source: epics.md#Story 7.2 Acceptance Criteria]
"""
import logging
from datetime import UTC, date, datetime

import disnake
from disnake.ext import commands

from bot.context import BotContext
from bot.db import config as db_config
from bot.db.bookings import init_event_slots
from bot.db.djs import get_all_djs
from bot.notion.client import DuplicateEventError, NotionWriteError
from bot.services.event_defaults import default_event_shape
from bot.services.lineup import future_events, upcoming_event
from bot.services.outreach import parse_outreach_enabled
from bot.ui import texts
from bot.ui.embeds import EmbedColors

logger = logging.getLogger(__name__)


def _today() -> date:
    return datetime.now(UTC).date()


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
            else:
                try:
                    await inter.followup.send(
                        "Something went wrong — please try again.",
                        ephemeral=True,
                    )
                except disnake.HTTPException:
                    logger.warning(
                        "AdminCog: could not send the error followup", exc_info=True
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
    # Story 7.2: /config set outreach-days-before (FR36)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="outreach-days-before",
        description="Set the number of days before an event to start outreach",
    )
    async def config_set_outreach_days_before(
        self,
        inter: disnake.ApplicationCommandInteraction,
        days: int = commands.Param(
            ge=1, description="Days before event to begin outreach (default: 14)"
        ),
    ) -> None:
        """Store outreach_days_before in config table.

        Consumed by OutreachService on every outreach check cycle (default: 14).
        [Source: epics.md#Story 7.2 AC1 — FR36]
        [Source: bot/services/outreach.py:84 — outreach_days_before consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_outreach_days_before")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "outreach_days_before", str(days))
        await inter.response.send_message(
            f"Outreach window set to {days} days before event.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.2: /config set outreach-cooldown-hours (FR36)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="outreach-cooldown-hours",
        description="Set the cooldown in hours between outreach contacts",
    )
    async def config_set_outreach_cooldown_hours(
        self,
        inter: disnake.ApplicationCommandInteraction,
        hours: int = commands.Param(
            ge=1, description="Hours between outreach contacts (default: 12)"
        ),
    ) -> None:
        """Store outreach_cooldown_hours in config table.

        Consumed by OutreachService cooldown check (default: 12).
        [Source: epics.md#Story 7.2 AC2 — FR36]
        [Source: bot/services/outreach.py:90 — outreach_cooldown_hours consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_outreach_cooldown_hours")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "outreach_cooldown_hours", str(hours)
        )
        await inter.response.send_message(
            f"Outreach cooldown set to {hours} hours between contacts.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.2: /config set lookahead-count (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="lookahead-count",
        description="Set the number of future events the bot maintains in Notion",
    )
    async def config_set_lookahead_count(
        self,
        inter: disnake.ApplicationCommandInteraction,
        count: int = commands.Param(
            ge=1, description="Number of future events to keep scheduled (default: 6)"
        ),
    ) -> None:
        """Store event_lookahead_count in config table.

        Consumed by EventsCog._auto_create_events() on every poll cycle (default: 6).
        [Source: epics.md#Story 7.2 AC3 — FR37]
        [Source: bot/cogs/events.py:116 — event_lookahead_count consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_lookahead_count")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "event_lookahead_count", str(count)
        )
        await inter.response.send_message(
            f"Event look-ahead set to {count} future events.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.2: /config set outreach-max (FR36)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="outreach-max",
        description="Set the maximum number of DJs contacted per event outreach cycle",
    )
    async def config_set_outreach_max(
        self,
        inter: disnake.ApplicationCommandInteraction,
        max_contacts: int = commands.Param(
            ge=1, description="Max DJs to contact per event outreach cycle"
        ),
    ) -> None:
        """Store outreach_max_contacts in config table.

        Not yet consumed by services (reserved for future outreach capping logic).
        [Source: epics.md#Story 7.2 AC4 — FR36]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_outreach_max")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "outreach_max_contacts", str(max_contacts)
        )
        await inter.response.send_message(
            f"Max outreach contacts per event set to {max_contacts}.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set slots-per-event (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="slots-per-event",
        description="Set the number of slots per auto-created event",
    )
    async def config_set_slots_per_event(
        self,
        inter: disnake.ApplicationCommandInteraction,
        count: int = commands.Param(
            ge=1, description="Number of slots per event (default: 6)"
        ),
    ) -> None:
        """Store slots_per_event in config table.

        Consumed by status_cmd (admin.py) and _auto_create_events (events.py) for slot count.
        [Source: epics.md#Story 7.3 AC1 — FR37]
        [Source: bot/cogs/admin.py — slots_per_event consumer (status_cmd)]
        [Source: bot/cogs/events.py — slots_per_event consumer (_auto_create_events)]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_slots_per_event")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "slots_per_event", str(count))
        await inter.response.send_message(
            f"New events will be created with {count} slots.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set event-interval-days (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="event-interval-days",
        description="Set the number of days between auto-created events",
    )
    async def config_set_event_interval_days(
        self,
        inter: disnake.ApplicationCommandInteraction,
        days: int = commands.Param(
            ge=1, description="Days between auto-created events (default: 7)"
        ),
    ) -> None:
        """Store event_interval_days in config table.

        Consumed by EventsCog._auto_create_events() for event spacing (default: 7).
        [Source: epics.md#Story 7.3 AC3 — FR37]
        [Source: bot/cogs/events.py:122 — event_interval_days consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_event_interval_days")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "event_interval_days", str(days))
        await inter.response.send_message(
            f"Auto-created events will be spaced {days} days apart.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set slot-duration (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="slot-duration",
        description="Set the duration of each event slot in minutes",
    )
    async def config_set_slot_duration(
        self,
        inter: disnake.ApplicationCommandInteraction,
        minutes: int = commands.Param(
            ge=1, description="Slot duration in minutes (default: 60)"
        ),
    ) -> None:
        """Store slot_duration_minutes in config table.

        Consumed by RemindersService._compute_slot_time() (reminders.py:112, default: 60).
        [Source: epics.md#Story 7.3 AC4 — FR37]
        [Source: bot/services/reminders.py:112 — slot_duration_minutes consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_slot_duration")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "slot_duration_minutes", str(minutes))
        await inter.response.send_message(
            f"Slot duration set to {minutes} minutes.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set start-time (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="start-time",
        description="Set the default event start time (UTC)",
    )
    async def config_set_start_time(
        self,
        inter: disnake.ApplicationCommandInteraction,
        time_str: str = commands.Param(
            description="Start time in HH:MM format (UTC, e.g. 20:00)"
        ),
    ) -> None:
        """Store default_start_time in config table (HH:MM UTC).

        Consumed by RemindersService._compute_slot_time() (reminders.py:111, default: '20:00').
        Validates HH:MM format and range before storing.
        [Source: epics.md#Story 7.3 AC5 — FR37]
        [Source: bot/services/reminders.py:111 — default_start_time consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_start_time")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        parts = time_str.strip().split(":")
        valid = (
            len(parts) == 2
            and parts[0].isdigit()
            and parts[1].isdigit()
            and 0 <= int(parts[0]) <= 23
            and 0 <= int(parts[1]) <= 59
        )
        if not valid:
            await inter.response.send_message(
                "Invalid format — use HH:MM (e.g. 20:00).", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "default_start_time", time_str.strip())
        await inter.response.send_message(
            f"Default event start time set to {time_str.strip()} UTC.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set event-schedule (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="event-schedule",
        description="Set the event recurrence schedule as a cron expression",
    )
    async def config_set_event_schedule(
        self,
        inter: disnake.ApplicationCommandInteraction,
        cron: str = commands.Param(
            description="Cron expression (e.g. '0 20 * * 6' = every Saturday at 20:00)"
        ),
    ) -> None:
        """Store event_recurrence_cron in config table.

        Not yet consumed by any service — stored for future auto-creation scheduling.
        [Source: epics.md#Story 7.3 AC6 — FR37]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_event_schedule")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "event_recurrence_cron", cron)
        await inter.response.send_message(
            f"Event recurrence schedule set to: {cron}", ephemeral=True
        )

    @config_set_base.sub_command(
        name="outreach",
        description="Turn automatic outreach invites on or off",
    )
    async def config_set_outreach(
        self,
        inter: disnake.ApplicationCommandInteraction,
        enabled: bool = commands.Param(description="True to send invites, false to stop"),
    ) -> None:
        """Store outreach_enabled ("true"/"false"); only the outreach loop reads it."""
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_outreach")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "outreach_enabled", "true" if enabled else "false"
        )
        if enabled:
            reply = "Automatic outreach is on."
        else:
            reply = "Automatic outreach is off. Booking, reminders and DMs keep working."
        await inter.response.send_message(reply, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 7.2: /config show — display all current config values
    # ---------------------------------------------------------------------------

    @config_base.sub_command(
        name="show",
        description="Show all current bot configuration values",
    )
    async def config_show(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        """Display all runtime config values in a formatted NEBULA_WHITE embed.

        Reads 11 keys from config table; shows default for unset keys.
        [Source: epics.md#Story 7.2 AC5]
        [Source: epics.md#Story 7.3 AC1,AC3,AC4,AC5,AC6 — 5 slot config keys added]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_show")
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return

        keys: list[tuple[str, str, str]] = [
            ("outreach_days_before", "Outreach Window (days before event)", "14"),
            ("outreach_cooldown_hours", "Outreach Cooldown (hours)", "12"),
            ("event_lookahead_count", "Event Look-Ahead Count", "6"),
            ("outreach_max_contacts", "Max Outreach Contacts", "unlimited"),
            ("dj_channel", "DJ Channel ID", "(not set)"),
            ("log_channel", "Admin Log Channel ID", "(not set)"),
            ("slots_per_event", "Slots Per Event", "6"),
            ("event_interval_days", "Event Interval (days)", "7"),
            ("slot_duration_minutes", "Slot Duration (minutes)", "60"),
            ("default_start_time", "Default Start Time (UTC)", "20:00"),
            ("event_recurrence_cron", "Event Recurrence Cron", "(not set)"),
        ]

        embed = disnake.Embed(
            title="⚙️ Bot Configuration",
            color=EmbedColors.NEBULA_WHITE,
        )
        for key, label, default in keys:
            raw = await db_config.get_value(self.ctx.db, guild_id, key)
            embed.add_field(
                name=label,
                value=raw if raw is not None else f"(default: {default})",
                inline=True,
            )
        raw_enabled = await db_config.get_value(self.ctx.db, guild_id, "outreach_enabled")
        embed.add_field(
            name="Automatic outreach",
            value="on" if parse_outreach_enabled(raw_enabled) else "off",
            inline=True,
        )

        await inter.response.send_message(embed=embed, ephemeral=True)

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
        # Notion work can exceed Discord's 3 s response deadline: defer first.
        await inter.response.defer(ephemeral=True)
        success, msg = await self.ctx.admin_booking_service.book_slot_override(
            admin_name=inter.author.name,
            dj_id=dj.id,
            dj_name=dj.display_name,
            event_name=event,
            slot_num=slot,
        )
        if success:
            await inter.followup.send(
                f"Done — {dj.mention} booked into slot {slot} of '{event}'.",
                ephemeral=True,
            )
        else:
            await inter.followup.send(msg, ephemeral=True)

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
        # Notion work can exceed Discord's 3 s response deadline: defer first.
        await inter.response.defer(ephemeral=True)
        success, msg = await self.ctx.admin_booking_service.cancel_slot_override(
            admin_name=inter.author.name,
            dj_id=dj.id,
            dj_name=dj.display_name,
            event_name=event,
            slot_num=slot,
        )
        if success:
            await inter.followup.send(
                f"Done — {dj.mention}'s booking in slot {slot} of '{event}' cancelled.",
                ephemeral=True,
            )
        else:
            await inter.followup.send(msg, ephemeral=True)

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

    # ---------------------------------------------------------------------------
    # Story 6.5: /status — next event overview (FR38)
    # ---------------------------------------------------------------------------

    @commands.slash_command(name="status", description="Show the next upcoming event status")
    async def status_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        """Fetch next event from Notion and display slot-by-slot booking status.

        Uses MISSION_GOLD embed. Slot counts and holders come from the event's
        Notion slot rows.
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.5 AC1, AC2, AC5 — FR38]
        """
        if self.ctx.notion_client is None or self.ctx.db is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return

        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.exception("status_cmd: list_events() failed")
            await inter.response.send_message(
                "Could not fetch events from Notion — please try again.", ephemeral=True
            )
            return

        if not events:
            await inter.response.send_message(
                "No upcoming events found in Notion.", ephemeral=True
            )
            return

        event = events[0]
        event_name = event.event_id
        event_unix = int(event.slot_start(0).timestamp())

        # Slot rows are the truth: booked = rows with a DJ (a DJ with two slots counts two)
        slots = sorted(event.slots, key=lambda s: s.number)
        total_slots = len(slots)
        booked_count = sum(1 for s in slots if s.dj_page_id is not None)

        # Build embed
        embed = disnake.Embed(
            title=f"🌌 {event_name}",
            color=EmbedColors.MISSION_GOLD,
        )
        date_display = f"<t:{event_unix}:f>"
        embed.add_field(name="Event Date", value=date_display, inline=False)
        embed.add_field(name="Total Slots", value=str(total_slots), inline=True)
        embed.add_field(name="Booked", value=str(booked_count), inline=True)
        embed.add_field(name="Open", value=str(total_slots - booked_count), inline=True)

        for slot in slots:
            slot_unix = int(event.slot_start(slot.number - 1).timestamp())
            slot_time = f"<t:{slot_unix}:f>"
            if slot.dj_page_id is None:
                dj_name = "Open"
            else:
                dj_name = slot.dj_name or "Unknown DJ"
            embed.add_field(
                name=f"Slot {slot.number} · {slot_time}",
                value=dj_name,
                inline=True,
            )

        await inter.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.5: /list-djs — DJ pool overview (FR39)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        name="list-djs", description="List all registered DJs and their booking status"
    )
    async def list_djs(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        """Query djs table and display all registered DJs with active status.

        Uses NEBULA_WHITE embed (consistent with AdminLogEmbed style).
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.5 AC3, AC4, AC5 — FR39]
        """
        if self.ctx.db is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return

        djs = await get_all_djs(self.ctx.db)

        if not djs:
            await inter.response.send_message("No DJs registered yet.", ephemeral=True)
            return

        lines = [
            f"<@{dj['discord_user_id']}> · {dj['dj_name']} · "
            f"{'Active' if dj['active_booking'] else 'Inactive'}"
            for dj in djs
        ]
        embed = disnake.Embed(
            title="📋 Registered DJs",
            description="\n".join(lines),
            color=EmbedColors.NEBULA_WHITE,
        )
        embed.set_footer(text=f"{len(djs)} DJ(s) registered")
        await inter.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 7.1: /add-event — manually create a Notion event (FR40)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        name="add-event", description="Manually create a new event in Notion"
    )
    async def add_event(
        self,
        inter: disnake.ApplicationCommandInteraction,
        date_str: str = commands.Param(
            name="date", description="Event date in YYYY-MM-DD format"
        ),
    ) -> None:
        """Create a new event page in Notion on the specified future date.

        Validates date format and that it is strictly in the future.
        Event ID is auto-generated: "YYYY-MM-DD Rave Night"; start, slot length and
        slot count come from default_event_shape() (config).
        Uses MISSION_GOLD Discord timestamp in success reply.
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 7.1 AC5, AC6, AC7 — FR40]
        """
        if self.ctx.notion_client is None or self.ctx.db is None:
            await inter.response.send_message(
                "Bot is not ready — please try again.", ephemeral=True
            )
            return

        try:
            event_date = date.fromisoformat(date_str)
        except ValueError:
            await inter.response.send_message(
                "Invalid date — please provide a future date in YYYY-MM-DD format.",
                ephemeral=True,
            )
            return

        if event_date <= date.today():
            await inter.response.send_message(
                "Invalid date — please provide a future date in YYYY-MM-DD format.",
                ephemeral=True,
            )
            return

        event_name = "Rave Night"
        event_id = f"{event_date.isoformat()} {event_name}"
        assert inter.guild_id is not None
        # Creating the event and its slot rows can exceed Discord's 3 s deadline.
        await inter.response.defer(ephemeral=True)
        shape = await default_event_shape(self.ctx.db, inter.guild_id)
        try:
            event = await self.ctx.notion_client.create_event(
                event_id, event_date, shape.start, shape.slot_count, shape.slot_minutes
            )
        except DuplicateEventError:
            await inter.followup.send(
                "An event with that ID already exists.", ephemeral=True
            )
            return
        except NotionWriteError:
            logger.exception("add_event: create_event() failed for %s", event_date)
            await inter.followup.send(
                "Failed to create event in Notion — please try again.", ephemeral=True
            )
            return

        # Pre-initialize slot_locks rows, as the auto-create path does (Story 7.3 AC2)
        try:
            await init_event_slots(self.ctx.db, event.page_id, shape.slot_count)
        except Exception as exc:
            logger.warning(
                "add_event: init_event_slots failed for %r: %s"
                " — slots will be lazily created on booking",
                event_id,
                exc,
            )

        event_unix = int(event.slot_start(0).timestamp())
        await inter.followup.send(
            f"Event created in Notion for <t:{event_unix}:f>.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # /lineup post: re-post the lineup of any future event
    # ---------------------------------------------------------------------------

    @commands.slash_command(name="lineup", description="Lineup posts (admin only)")
    async def lineup_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Top-level /lineup command group."""
        pass

    @lineup_base.sub_command(
        name="post",
        description="Post the lineup of the upcoming event (or a chosen future event)",
    )
    async def lineup_post(
        self,
        inter: disnake.ApplicationCommandInteraction,
        event: str | None = commands.Param(
            default=None,
            description="Event ID (default: the upcoming event)",
        ),
    ) -> None:
        """Post a new lineup message. Admin-only through the cog check."""
        if (
            self.ctx.notion_client is None
            or self.ctx.db is None
            or self.ctx.lineup_service is None
        ):
            await inter.response.send_message(
                "Bot is not ready. Please try again.", ephemeral=True
            )
            return

        await inter.response.defer(ephemeral=True)

        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.exception("lineup_post: list_events() failed")
            await inter.followup.send(
                "Could not fetch events from Notion. Please try again.", ephemeral=True
            )
            return

        today = _today()
        if event is None:
            chosen = upcoming_event(events, today)
            if chosen is None:
                await inter.followup.send(texts.NO_LINEUP, ephemeral=True)
                return
        else:
            wanted = event.strip().casefold()
            chosen = next(
                (e for e in future_events(events, today) if e.event_id.casefold() == wanted),
                None,
            )
            if chosen is None:
                await inter.followup.send(
                    "That event is not available. Pick one from the list.", ephemeral=True
                )
                return

        try:
            posted = await self.ctx.lineup_service.post_event(chosen)
        except Exception:
            logger.exception("lineup_post: post_event failed")
            posted = False

        if posted:
            await inter.followup.send("Lineup posted.", ephemeral=True)
        else:
            await inter.followup.send(
                "Could not post the lineup. Check that the DJ channel is set and the bot "
                "can write there.",
                ephemeral=True,
            )

    @lineup_post.autocomplete("event")
    async def lineup_post_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> list[str]:
        """Event IDs of future events matching the typed text (max 25)."""
        if self.ctx.notion_client is None:
            return []
        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.warning("lineup_post autocomplete: list_events() failed", exc_info=True)
            return []
        needle = user_input.strip().casefold()
        ids = [e.event_id for e in future_events(events, _today())]
        return [i for i in ids if needle in i.casefold()][:25]
