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
Story 7.3: /config set slots-per-event -- default slot count of events without Notion Slots (FR37)
Story 7.3: /config set event-interval-days -- days between auto-created events (FR37)
Story 7.3: /config set slot-duration -- slot duration in minutes (FR37)
Story 7.3: /config set start-time -- default event start time HH:MM UTC (FR37)
Story 7.3: /config set event-schedule -- event recurrence cron expression (FR37)
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
    onboard                    <- .sub_command()
    onboard-preview            <- .sub_command()
  /ravebot-status              <- @commands.slash_command()
  /list-djs                    <- @commands.slash_command()
  /add-event                   <- @commands.slash_command()

[Source: architecture.md#Admin Whitelist: disnake Check Decorator]
[Source: epics.md#Story 1.4 Acceptance Criteria]
[Source: epics.md#Story 6.2 Acceptance Criteria]
[Source: epics.md#Story 6.3 Acceptance Criteria]
[Source: epics.md#Story 6.5 Acceptance Criteria]
[Source: epics.md#Story 7.1 Acceptance Criteria]
[Source: epics.md#Story 7.2 Acceptance Criteria]
"""
import logging
from datetime import date

import disnake
from disnake.ext import commands

from bot import active, loops, schedule, slots
from bot import guild as served
from bot.context import BotContext
from bot.db import config as db_config
from bot.db.bookings import get_booked_djs_for_event
from bot.db.djs import get_all_djs, get_dj_by_discord_id
from bot.notion import props
from bot.notion.client import NotionWriteError
from bot.ui.ack import auto_ack, reply
from bot.ui.embeds import EmbedColors

logger = logging.getLogger(__name__)

# Admin commands are hidden from members without Manage Server and only usable in servers.
# The ADMIN_USER_IDS whitelist (cog_slash_command_check) still applies on top.
ADMIN_COMMAND = {
    "default_member_permissions": disnake.Permissions(manage_guild=True),
    "contexts": disnake.InteractionContextTypes(guild=True),
}

NOT_SERVED_TEXT = "This bot doesn't serve this server (GUILD_ID), so nothing was changed."


class NotServedHere(commands.CheckFailure):
    """An admin command was used in a server this bot is in but does not serve."""


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
        A whitelisted admin in another server the bot is in gets NotServedHere: config,
        onboarding DMs and bookings there would act outside the served server's settings.
        Note: disnake 2.12.0 defines cog_slash_command_check as a synchronous method.
        [Source: architecture.md#Admin Whitelist: disnake Check Decorator]
        [Source: epics.md#FR41, NFR6]
        """
        if inter.author.id not in self.ctx.config.admin_user_ids:
            return False
        if not served.serves(inter.client, inter.guild_id):
            raise NotServedHere()
        return True

    async def _refuse_if_passive(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """Tell the admin and return True when the bot is passive (bot.active).

        Used by every command that contacts DJs or writes bookings/events; read-only
        commands and /config keep working while passive.
        """
        if await active.is_active(self.ctx.db, served.resolve(inter.client)):
            return False
        await reply(inter, active.PASSIVE_NOTICE, ephemeral=True)
        return True

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
        if isinstance(error, NotServedHere):
            await reply(inter, NOT_SERVED_TEXT, ephemeral=True)
        elif isinstance(error, commands.CheckFailure):
            await reply(inter, 
                "You don't have permission to use this command.",
                ephemeral=True,
            )
        else:
            logger.error(
                "Unexpected error in AdminCog slash command: %s", error, exc_info=True
            )
            # reply() answers by follow-up when the command was deferred ("thinking…"),
            # which otherwise stays on screen forever
            try:
                await reply(inter,
                    "Something went wrong. Please try again.",
                    ephemeral=True,
                )
            except disnake.HTTPException:
                logger.exception("Could not tell the admin about the failed command")

    @commands.slash_command(
        **ADMIN_COMMAND, name="config", description="Bot configuration commands (admin only)"
    )
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "dj_channel", str(channel.id))
        await reply(inter, 
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "log_channel", str(channel.id))
        await reply(inter, 
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "outreach_days_before", str(days))
        await reply(inter, 
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "outreach_cooldown_hours", str(hours)
        )
        await reply(inter, 
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
            ge=0, description="Future events to keep scheduled; 0 = off (default)"
        ),
    ) -> None:
        """Store event_lookahead_count in config table.

        Consumed by EventsCog._auto_create_events() on every poll cycle (default: 0 = off).
        [Source: epics.md#Story 7.2 AC3 — FR37]
        [Source: bot/cogs/events.py:116 — event_lookahead_count consumer]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_lookahead_count")
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "event_lookahead_count", str(count)
        )
        text = (
            "Automatic event creation is off."
            if count == 0
            else f"Event look-ahead set to {count} future events."
        )
        await reply(inter, text, ephemeral=True)

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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(
            self.ctx.db, guild_id, "outreach_max_contacts", str(max_contacts)
        )
        await reply(inter, 
            f"Max outreach contacts per event set to {max_contacts}.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set slots-per-event (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="slots-per-event",
        description="Set the slot count of events whose Notion Slots is empty",
    )
    async def config_set_slots_per_event(
        self,
        inter: disnake.ApplicationCommandInteraction,
        count: int = commands.Param(
            ge=1, description="Number of slots per event (default: 6)"
        ),
    ) -> None:
        """Store slots_per_event in config table.

        The slot count of every event whose Notion Slots property is empty (bot.slots), and
        the Slots value written to events the bot creates (_auto_create_events).
        [Source: epics.md#Story 7.3 AC1 — FR37]
        [Source: bot/cogs/admin.py — slots_per_event consumer (status_cmd)]
        [Source: bot/cogs/events.py — slots_per_event consumer (_auto_create_events)]
        """
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_slots_per_event")
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "slots_per_event", str(count))
        await reply(inter, 
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "event_interval_days", str(days))
        await reply(inter, 
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "slot_duration_minutes", str(minutes))
        await schedule.refresh(self.ctx.db, guild_id)
        await reply(inter, 
            f"Slot duration set to {minutes} minutes.", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # Story 7.3: /config set start-time (FR37)
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="start-time",
        description="Set the event start time (in the configured timezone, default UTC)",
    )
    async def config_set_start_time(
        self,
        inter: disnake.ApplicationCommandInteraction,
        time_str: str = commands.Param(
            description="Start time in HH:MM format, in the configured timezone (e.g. 21:00)"
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
            await reply(inter, 
                "Invalid format. Use HH:MM, for example 21:00.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "default_start_time", time_str.strip())
        sched = await schedule.refresh(self.ctx.db, guild_id)
        await reply(inter, 
            f"Default event start time set to {time_str.strip()} ({sched.tz.key}).", ephemeral=True
        )

    # ---------------------------------------------------------------------------
    # /config set timezone - time zone for start-time and all displayed slot times
    # ---------------------------------------------------------------------------

    @config_set_base.sub_command(
        name="active",
        description="Turn RaveBot on or off (off = passive: no DMs, outreach, reminders or posts)",
    )
    async def config_set_active(
        self,
        inter: disnake.ApplicationCommandInteraction,
        state: str = commands.Param(
            choices=["on", "off"], description="on = start contacting DJs, off = passive"
        ),
    ) -> None:
        """Switch between passive (default) and active operation for this server."""
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_active")
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await active.set_active(self.ctx.db, guild_id, state == "on")
        logger.info("bot_active set to %s by %s", state, inter.author.id)
        text = (
            "RaveBot is now ACTIVE. Onboarding DMs, outreach, reminders and lineup posts are on."
            if state == "on"
            else "RaveBot is now PASSIVE and won't contact anyone."
        )
        await reply(inter, text, ephemeral=True)

    @config_set_base.sub_command(
        name="dm-log",
        description="Mirror DJ DMs into the log channel (on) or keep them private (off)",
    )
    async def config_set_dm_log(
        self,
        inter: disnake.ApplicationCommandInteraction,
        state: str = commands.Param(
            choices=["on", "off"], description="on = mirror, off = private"
        ),
    ) -> None:
        """Store dm_log; AdminLogService.post_dm_transcript skips mirroring when it is off."""
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_dm_log")
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "dm_log", state)
        await reply(inter, f"DM log mirroring is {state}.", ephemeral=True)

    @config_set_base.sub_command(
        name="timezone",
        description="Set the time zone for the event start time (IANA name, default UTC)",
    )
    async def config_set_timezone(
        self,
        inter: disnake.ApplicationCommandInteraction,
        name: str = commands.Param(
            description="IANA time zone, e.g. Europe/Berlin, America/New_York, UTC"
        ),
    ) -> None:
        """Store the time zone of the start time; each night then starts at that local time."""
        guild_id = inter.guild_id
        assert guild_id is not None
        if self.ctx.db is None:
            logger.error("AdminCog: DB pool is None in config_set_timezone")
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        tz = schedule.parse_timezone(name)
        if tz is None:
            await reply(inter, 
                "Unknown time zone. Use an IANA name like Europe/Berlin.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "timezone", tz.key)
        await schedule.refresh(self.ctx.db, guild_id)
        await reply(inter, f"Time zone set to {tz.key}.", ephemeral=True)

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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await db_config.set_value(self.ctx.db, guild_id, "event_recurrence_cron", cron)
        await reply(inter, 
            f"Event recurrence schedule set to: {cron}", ephemeral=True
        )

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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        keys: list[tuple[str, str, str]] = [
            ("outreach_days_before", "Outreach Window (days before event)", "14"),
            ("outreach_cooldown_hours", "Outreach Cooldown (hours)", "12"),
            ("event_lookahead_count", "Event Look-Ahead Count", "0 = off"),
            ("outreach_max_contacts", "Max Outreach Contacts", "unlimited"),
            ("dj_channel", "DJ Channel ID", "(not set)"),
            ("log_channel", "Admin Log Channel ID", "(not set)"),
            ("slots_per_event", "Slots Per Event", "6"),
            ("event_interval_days", "Event Interval (days)", "7"),
            ("slot_duration_minutes", "Slot Duration (minutes)", "60"),
            ("default_start_time", "Default Start Time", "20:00"),
            ("timezone", "Timezone", "UTC"),
            ("bot_active", "Active", "off"),
            ("dm_log", "DM Log Mirroring", "on"),
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

        health = loops.last_ok()
        lines = []
        for name in ("notion_poll", "reminders", "outreach"):
            when = health.get(name)
            lines.append(f"{name}: <t:{int(when.timestamp())}:R>" if when else f"{name}: not yet")
        embed.add_field(name="Background loops (last OK)", value="\n".join(lines), inline=False)
        await reply(inter, embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.2: /intervene command group — admin DM takeover (FR32, FR33)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        **ADMIN_COMMAND,
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
    @auto_ack(ephemeral=True)
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
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.intervention_service is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await self.ctx.intervention_service.start(dj.id, dj.display_name)
        await reply(inter, 
            f"Intervention active for {dj.mention}. All automated DMs paused. "
            "Use `/intervene send` to message them.",
            ephemeral=True,
        )

    @intervene_base.sub_command(
        name="send", description="Send a message to a DJ as the bot during intervention"
    )
    @auto_ack(ephemeral=True)
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
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.intervention_service is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        sent = await self.ctx.intervention_service.send_message(dj.id, dj.display_name, message)
        if sent:
            await reply(inter, 
                f"Message sent to {dj.mention}.", ephemeral=True
            )
        else:
            await reply(inter, 
                f"Could not deliver the message to {dj.mention}. Their DMs may be blocked.",
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        await self.ctx.intervention_service.end(dj.id, dj.display_name)
        await reply(inter, 
            f"Intervention ended for {dj.mention}. The bot handles their DMs again.",
            ephemeral=True,
        )

    # ---------------------------------------------------------------------------
    # Story 6.3: /admin command group — booking & cancellation override
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        **ADMIN_COMMAND,
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
    @auto_ack(ephemeral=True)
    async def admin_book(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to book"),
        event: str = commands.Param(
            description="Event date (YYYY-MM-DD) or its exact name in Notion"
        ),
        slot: int = commands.Param(ge=1, description="Slot number (1 = first slot of event)"),
    ) -> None:
        """Force-book a DJ into a slot, writing directly to Notion without confirmation.

        Uses AdminBookingService.book_slot_override().
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.3 AC1, AC4]
        [Source: bot/services/admin_booking.py -- AdminBookingService.book_slot_override()]
        """
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.admin_booking_service is None or self.ctx.db is None:
            await reply(inter,
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        # A member without a finished profile could be booked, but then only got the
        # onboarding intro when they DMed about it, and could not cancel their own slot.
        dj_row = await get_dj_by_discord_id(self.ctx.db, dj.id)
        if dj_row is None or not dj_row["onboarding_complete"]:
            await reply(inter,
                f"{dj.mention} hasn't finished onboarding yet. Invite them with "
                "`/admin onboard` first, then book them.",
                ephemeral=True,
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
            done = f"Done, {dj.mention} is booked into slot {slot} of '{event}'."
            await reply(inter, f"{done}\n{msg}" if msg else done, ephemeral=True)
        else:
            await reply(inter, msg, ephemeral=True)

    @admin_base.sub_command(
        name="cancel", description="Force-cancel a DJ's event booking"
    )
    @auto_ack(ephemeral=True)
    async def admin_cancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ whose booking to cancel"),
        event: str = commands.Param(
            description="Event date (YYYY-MM-DD) or its exact name in Notion"
        ),
        slot: int = commands.Param(ge=1, description="Slot number (1 = first slot of event)"),
    ) -> None:
        """Force-cancel a DJ's booking, removing it from Notion and slot_locks.

        Uses AdminBookingService.cancel_slot_override().
        Returns 'No booking found' if the slot is not held by this DJ (AC3).
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.3 AC2, AC3, AC4]
        [Source: bot/services/admin_booking.py -- AdminBookingService.cancel_slot_override()]
        """
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.admin_booking_service is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
            await reply(inter, 
                f"Done, {dj.mention}'s booking in slot {slot} of '{event}' is canceled.",
                ephemeral=True,
            )
        else:
            await reply(inter, msg, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.4: /admin dj sub-command-group — DJ status management
    # ---------------------------------------------------------------------------

    @admin_base.sub_command(
        name="onboard",
        description="Send the onboarding DM to one member with the DJ role",
    )
    @auto_ack(ephemeral=True)
    async def admin_onboard(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="Member with the DJ role"),
    ) -> None:
        """Invite one existing DJ: the role-change trigger never fires for them.

        Only for a single, explicitly chosen member — never in bulk. Refused while passive.
        """
        if await self._refuse_if_passive(inter):
            return
        if not any(r.name.lower() == "dj" for r in dj.roles):
            await reply(inter, 
                f"{dj.display_name} doesn't have the DJ role, so nothing was sent.", ephemeral=True
            )
            return
        existing = await get_dj_by_discord_id(self.ctx.db, dj.id) if self.ctx.db else None
        if existing is not None and existing["onboarding_complete"]:
            await reply(inter, 
                f"{dj.display_name} is already registered, so nothing was sent.", ephemeral=True
            )
            return
        onboarding = inter.client.get_cog("OnboardingCog")
        if onboarding is None:
            await reply(inter, "Onboarding is not loaded.", ephemeral=True)
            return
        result = await onboarding.send_onboarding_dm(dj)
        text = {
            "sent": f"Onboarding DM sent to {dj.display_name}.",
            "dms_closed": f"{dj.display_name} has DMs from server members turned off.",
        }.get(result, f"Onboarding DM to {dj.display_name}: {result}")
        await reply(inter, text, ephemeral=True)

    @admin_base.sub_command(
        name="onboard-preview",
        description="List members with the DJ role who are not registered yet (sends nothing)",
    )
    async def admin_onboard_preview(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Read-only: who would /admin onboard be for? Works while passive."""
        guild = inter.guild
        if guild is None or self.ctx.db is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        registered = {
            row["discord_user_id"]
            for row in await get_all_djs(self.ctx.db)
            if row.get("onboarding_complete", True)
        }
        pending = sorted(
            (m for m in guild.members
             if not m.bot and m.id not in registered
             and any(r.name.lower() == "dj" for r in m.roles)),
            key=lambda m: m.display_name.lower(),
        )
        if not pending:
            await reply(inter, 
                "Every member with the DJ role is registered.", ephemeral=True
            )
            return
        shown = ", ".join(m.display_name for m in pending[:50])
        more = f" … and {len(pending) - 50} more" if len(pending) > 50 else ""
        await reply(inter, 
            f"{len(pending)} member(s) with the DJ role are not registered: {shown}{more}",
            ephemeral=True,
        )

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
    @auto_ack(ephemeral=True)
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
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.admin_booking_service is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
                text = f"{dj.mention} is now active in the booking pool."
            else:
                text = f"{dj.mention} has been removed from the booking pool."
            await reply(inter, text, ephemeral=True)
        else:
            await reply(inter, msg, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.5: /status — next event overview (FR38)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        **ADMIN_COMMAND, name="ravebot-status", description="Show the next upcoming event status"
    )
    @auto_ack(ephemeral=True)
    async def status_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        """Fetch next event from Notion and display slot-by-slot booking status.

        Uses MISSION_GOLD embed. Slot count from config (default 6).
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.5 AC1, AC2, AC5 — FR38]
        """
        if self.ctx.notion_client is None or self.ctx.db is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        try:
            events = await self.ctx.notion_client.poll_events()
        except Exception:
            logger.exception("status_cmd: poll_events() failed")
            await reply(inter, 
                "Could not load the events from Notion. Please try again.", ephemeral=True
            )
            return

        if not events:
            await reply(inter, 
                "No upcoming events found in Notion.", ephemeral=True
            )
            return

        event = events[0]
        event_id = event["id"]

        # Parse event name
        try:
            event_name = props.title_text(event)
        except (KeyError, IndexError, TypeError):
            event_name = "Unknown Event"

        # Parse event start datetime
        event_start = schedule.event_start(event)
        event_unix = int(event_start.timestamp()) if event_start is not None else 0

        # Get slots_per_event from config (default 6)
        assert inter.guild_id is not None
        await slots.annotate(self.ctx.db, inter.guild_id, [event])
        slots_per_event = slots.state(event).total

        # Get booked slots from DB
        booked = await get_booked_djs_for_event(self.ctx.db, event_id)
        booked_by_slot: dict[int, str] = {b["slot_index"]: b["dj_name"] for b in booked}

        # Build embed
        embed = disnake.Embed(
            title=f"🌌 {event_name}",
            color=EmbedColors.MISSION_GOLD,
        )
        date_display = f"<t:{event_unix}:f>" if event_unix else "Unknown"
        embed.add_field(name="Event Date", value=date_display, inline=False)
        embed.add_field(name="Total Slots", value=str(slots_per_event), inline=True)
        embed.add_field(name="Booked", value=str(len(booked_by_slot)), inline=True)
        embed.add_field(name="Open", value=str(slots_per_event - len(booked_by_slot)), inline=True)

        for slot_index in range(slots_per_event):
            if event_start is not None:
                slot_unix = schedule.slot_unix(event, slot_index)
                slot_time = f"<t:{slot_unix}:f>"
            else:
                slot_time = f"Slot {slot_index + 1}"
            dj_name = booked_by_slot.get(slot_index, "Open")
            embed.add_field(
                name=f"Slot {slot_index + 1} · {slot_time}",
                value=dj_name,
                inline=True,
            )

        await reply(inter, embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 6.5: /list-djs — DJ pool overview (FR39)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        **ADMIN_COMMAND,
        name="list-djs", description="List all registered DJs and their booking status"
    )
    @auto_ack(ephemeral=True)
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
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        djs = await get_all_djs(self.ctx.db)

        if not djs:
            await reply(inter, "No DJs registered yet.", ephemeral=True)
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
        await reply(inter, embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------------
    # Story 7.1: /add-event — manually create a Notion event (FR40)
    # ---------------------------------------------------------------------------

    @commands.slash_command(
        **ADMIN_COMMAND,
        name="add-event", description="Manually create a new event in Notion"
    )
    @auto_ack(ephemeral=True)
    async def add_event(
        self,
        inter: disnake.ApplicationCommandInteraction,
        date_str: str = commands.Param(
            name="date", description="Event date in YYYY-MM-DD format"
        ),
    ) -> None:
        """Create a new event page in Notion on the specified future date.

        Validates date format and that it is strictly in the future.
        Event name is auto-generated: "Rave Night {DD Mon YYYY}".
        Uses MISSION_GOLD Discord timestamp in success reply.
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 7.1 AC5, AC6, AC7 — FR40]
        """
        if await self._refuse_if_passive(inter):
            return
        if self.ctx.notion_client is None:
            await reply(inter, 
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        try:
            event_date = date.fromisoformat(date_str)
        except ValueError:
            await reply(inter, 
                "Invalid date. Please give a future date as YYYY-MM-DD.",
                ephemeral=True,
            )
            return

        if event_date <= date.today():
            await reply(inter, 
                "Invalid date. Please give a future date as YYYY-MM-DD.",
                ephemeral=True,
            )
            return

        # One event per night: the plan may name it differently ("Casual Event"), so the
        # date alone decides whether it already exists
        try:
            upcoming = await self.ctx.notion_client.poll_events()
        except Exception:
            logger.exception("add_event: poll_events() failed")
            await reply(inter,
                "Could not load the events from Notion. Please try again.", ephemeral=True
            )
            return
        same_day = [
            e for e in upcoming
            if str(e.get("properties", {}).get("Date", {}).get("date", {}).get("start", ""))[:10]
            == event_date.isoformat()
        ]
        if same_day:
            try:
                existing = props.title_text(same_day[0])
            except (KeyError, IndexError, TypeError):
                existing = "an event"
            await reply(inter,
                f"There is already {existing!r} on {event_date.isoformat()}, "
                "so nothing was created.",
                ephemeral=True,
            )
            return

        event_name = f"Rave Night {event_date.strftime('%d %b %Y')}"
        try:
            await self.ctx.notion_client.create_event(event_name, event_date)
        except NotionWriteError:
            logger.exception("add_event: create_event() failed for %s", event_date)
            await reply(inter, 
                "Failed to create the event in Notion. Please try again.", ephemeral=True
            )
            return

        event_unix = schedule.slot_unix(
            {"properties": {"Date": {"date": {"start": event_date.isoformat()}}}}, 0
        )
        await reply(inter, 
            f"Event created in Notion for <t:{event_unix}:f>.", ephemeral=True
        )
