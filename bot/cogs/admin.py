"""Admin cog: the admin slash commands (admin only, gated while the bot is idle).

Command tree (disnake nesting, spaces are levels):
  /config show
  /create event
  /post lineup update
  /book dj slot
  /cancel dj slot
  /list all djs
  /automatic dj booking
(/setup lives in bot/cogs/setup.py.)

[Source: architecture.md#Admin Whitelist: disnake Check Decorator]
"""
import logging
from datetime import date

import disnake
from disnake.ext import commands

from bot.context import BotContext
from bot.db import config as db_config
from bot.db.bookings import init_event_slots
from bot.db.djs import get_all_djs
from bot.notion.client import DuplicateEventError, NotionWriteError
from bot.services import activation
from bot.services.event_defaults import default_event_shape
from bot.services.lineup import future_events, upcoming_event
from bot.services.outreach import parse_outreach_enabled
from bot.services.roles import DJ_ROLE_KEY, INVITES_ROLE_KEY
from bot.timezone import DEFAULT_TIMEZONE
from bot.timezone import local_today
from bot.ui import texts
from bot.ui.embeds import EmbedColors
from bot.ui.gates import ADMIN_IDLE_REPLY

logger = logging.getLogger(__name__)

# Commands that work while the bot is idle (identified by qualified name)
IDLE_EXEMPT_COMMANDS = frozenset({"setup", "config show"})


def _today() -> date:
    return local_today()


class AdminCog(commands.Cog):
    """Admin slash commands — whitelist-gated via cog_slash_command_check."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    async def cog_slash_command_check(
        self, inter: disnake.ApplicationCommandInteraction
    ) -> bool:
        """Enforce admin whitelist for all slash commands in this cog.

        Reads admin_user_ids from Config (loaded from ADMIN_USER_IDS env var at startup).
        Returning False raises CheckFailure — handled in cog_slash_command_error.
        Note: disnake 2.12.0 defines cog_slash_command_check as a synchronous method.
        [Source: architecture.md#Admin Whitelist: disnake Check Decorator]
        [Source: epics.md#FR41, NFR6]
        """
        if inter.author.id not in self.ctx.config.admin_user_ids:
            return False
        command = inter.application_command
        name = getattr(command, "qualified_name", None)
        if name in IDLE_EXEMPT_COMMANDS:
            return True
        # Idle: every other command gets the admin idle text (the answer is sent here)
        guild_id = inter.guild_id
        if guild_id is None:
            guilds = getattr(inter.bot, "guilds", None)
            guild_id = guilds[0].id if guilds else None
        active = (
            self.ctx.db is not None
            and guild_id is not None
            and await activation.is_active(self.ctx.db, guild_id)
        )
        if active:
            return True
        await inter.response.send_message(ADMIN_IDLE_REPLY, ephemeral=True)
        return False

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
            if inter.response.is_done():
                return  # the check already answered (idle reply)
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
                    "Something went wrong. Please try again in a moment.",
                    ephemeral=True,
                )
            else:
                try:
                    await inter.followup.send(
                        "Something went wrong. Please try again in a moment.",
                        ephemeral=True,
                    )
                except disnake.HTTPException:
                    logger.warning(
                        "AdminCog: could not send the error followup", exc_info=True
                    )


    @commands.slash_command(name="config", description="Show the bot settings (admin only)")
    async def config_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """Top-level /config command."""
        pass

    @config_base.sub_command(name="show", description="Show all current bot settings")
    async def config_show(self, inter: disnake.ApplicationCommandInteraction) -> None:
        """List every setting with the stored value or the default, then the /setup hint."""
        guild_id = inter.guild_id
        assert guild_id is not None
        db = self.ctx.db
        if db is None:
            logger.error("AdminCog: DB pool is None in config_show")
            await inter.response.send_message(
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        async def stored(key: str) -> str | None:
            return await db_config.get_value(db, guild_id, key)

        def shown(raw: str | None, default: str) -> str:
            return raw if raw else f"{default} (default)"

        def channel(raw: str | None) -> str:
            return f"<#{raw}>" if raw else "not set"

        def role(raw: str | None, fallback: str) -> str:
            return f"<@&{raw}>" if raw else f"{fallback} (default)"

        raw_enabled = await stored("outreach_enabled")
        active = await activation.is_active(db, guild_id)
        lines = [
            "**Bot settings**",
            f"DJ channel: {channel(await stored('dj_channel'))}",
            f"Log channel: {channel(await stored('log_channel'))}",
            f"DJ role: {role(await stored(DJ_ROLE_KEY), 'DJ')}",
            f"Invites role: {role(await stored(INVITES_ROLE_KEY), 'Active Booking')}",
            f"Time zone: {shown(await stored('timezone'), DEFAULT_TIMEZONE)}",
            f"Slots per event: {shown(await stored('slots_per_event'), '6')}",
            f"Start time (CET): {shown(await stored('default_start_time'), '20:00')}",
            f"Slot length (minutes): {shown(await stored('slot_duration_minutes'), '60')}",
            f"One event every (days): {shown(await stored('event_interval_days'), '7')}",
            f"Future events kept ready: {shown(await stored('event_lookahead_count'), '6')}",
            f"Automatic invites: {'on' if parse_outreach_enabled(raw_enabled) else 'off'}",
            "Start asking (days before an event): "
            f"{shown(await stored('outreach_days_before'), '14')}",
            "Wait between two invites to the same DJ (hours): "
            f"{shown(await stored('outreach_cooldown_hours'), '12')}",
            f"Bot status: {'active' if active else 'idle'}",
        ]
        if not active:
            lines.insert(0, texts.SETUP_NOT_FINISHED)
        lines.append("")
        lines.append(texts.CONFIG_SHOW_HINT)
        await inter.response.send_message("\n".join(lines), ephemeral=True)

    @commands.slash_command(name="create", description="Create things by hand (admin only)")
    async def create_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @create_base.sub_command(
        name="event", description="Manually create a new event in Notion"
    )
    async def create_event(
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
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 7.1 AC5, AC6, AC7 — FR40]
        """
        if self.ctx.notion_client is None or self.ctx.db is None:
            await inter.response.send_message(
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return

        try:
            event_date = date.fromisoformat(date_str)
        except ValueError:
            await inter.response.send_message(
                "Invalid date. Provide a future date in YYYY-MM-DD format.",
                ephemeral=True,
            )
            return

        if event_date <= _today():
            await inter.response.send_message(
                "Invalid date. Provide a future date in YYYY-MM-DD format.",
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
            logger.exception("create_event: create_event() failed for %s", event_date)
            await inter.followup.send(
                "Could not create the event in Notion. Please try again in a moment.",
                ephemeral=True,
            )
            return

        # Pre-initialize slot_locks rows, as the auto-create path does (Story 7.3 AC2)
        try:
            await init_event_slots(self.ctx.db, event.page_id, shape.slot_count)
        except Exception as exc:
            logger.warning(
                "create_event: init_event_slots failed for %r: %s"
                " — slots will be lazily created on booking",
                event_id,
                exc,
            )

        await inter.followup.send(
            f"Event created in Notion for {texts.fmt_slot_when(event, 0)}.", ephemeral=True
        )

    @commands.slash_command(name="post", description="Post to the DJ channel (admin only)")
    async def post_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @post_base.sub_command_group(name="lineup", description="Lineup posts (admin only)")
    async def post_lineup_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @post_lineup_group.sub_command(
        name="update",
        description="Post the lineup of the upcoming event (or a chosen future event)",
    )
    async def post_lineup_update(
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
            logger.exception("post_lineup_update: list_events() failed")
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
            logger.exception("post_lineup_update: post_event failed")
            posted = False

        if posted:
            await inter.followup.send("Lineup posted.", ephemeral=True)
        else:
            await inter.followup.send(
                "Could not post the lineup. Check that the DJ channel is set and the bot "
                "can write there.",
                ephemeral=True,
            )

    @post_lineup_update.autocomplete("event")
    async def post_event_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> list[str]:
        return await self.future_event_autocomplete(inter, user_input)

    async def future_event_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> list[str]:
        """Event IDs of future events matching the typed text (max 25)."""
        if self.ctx.notion_client is None:
            return []
        try:
            events = await self.ctx.notion_client.list_events()
        except Exception:
            logger.warning("event autocomplete: list_events() failed", exc_info=True)
            return []
        needle = user_input.strip().casefold()
        ids = [e.event_id for e in future_events(events, _today())]
        return [i for i in ids if needle in i.casefold()][:25]

    @commands.slash_command(name="book", description="Book DJs (admin only)")
    async def book_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @book_base.sub_command_group(name="dj", description="Book a DJ (admin only)")
    async def book_dj_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @commands.slash_command(name="cancel", description="Cancel bookings (admin only)")
    async def cancel_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @cancel_base.sub_command_group(name="dj", description="Cancel a DJ (admin only)")
    async def cancel_dj_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @book_dj_group.sub_command(
        name="slot", description="Force-book a DJ into an event slot (bypasses confirmation)"
    )
    async def book_dj_slot(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to book"),
        event: str = commands.Param(description="Event ID (pick one from the list)"),
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
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
                f"Done. {dj.mention} is booked into slot {slot} of '{event}'.",
                ephemeral=True,
            )
        else:
            await inter.followup.send(msg, ephemeral=True)

    @book_dj_slot.autocomplete("event")
    async def book_event_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> list[str]:
        return await self.future_event_autocomplete(inter, user_input)

    @cancel_dj_group.sub_command(
        name="slot", description="Force-cancel a DJ's event booking"
    )
    async def cancel_dj_slot(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ whose booking to cancel"),
        event: str = commands.Param(description="Event ID (pick one from the list)"),
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
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
                f"Done. {dj.mention}'s booking in slot {slot} of '{event}' is cancelled.",
                ephemeral=True,
            )
        else:
            await inter.followup.send(msg, ephemeral=True)

    @cancel_dj_slot.autocomplete("event")
    async def cancel_event_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> list[str]:
        return await self.future_event_autocomplete(inter, user_input)

    @commands.slash_command(name="automatic", description="Automatic bookings (admin only)")
    async def automatic_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @automatic_base.sub_command_group(name="dj", description="DJ settings (admin only)")
    async def automatic_dj_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @automatic_dj_group.sub_command(
        name="booking",
        description="Turn automatic booking invites on or off for a DJ",
    )
    async def automatic_dj_booking(
        self,
        inter: disnake.ApplicationCommandInteraction,
        dj: disnake.Member = commands.Param(description="The DJ to update"),
        enabled: bool = commands.Param(
            description="on = add to the booking pool, off = remove"
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
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
            )
            return
        success, msg = await self.ctx.admin_booking_service.set_dj_active_status(
            inter.guild_id,
            dj.id,
            dj.display_name,
            enabled,
        )
        if success:
            if enabled:
                reply = f"{dj.mention} is now active in the booking pool."
            else:
                reply = f"{dj.mention} has been removed from the booking pool."
            await inter.response.send_message(reply, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)

    @commands.slash_command(name="list", description="Lists (admin only)")
    async def list_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @list_base.sub_command_group(name="all", description="Lists (admin only)")
    async def list_all_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @list_all_group.sub_command(
        name="djs", description="List all registered DJs and their booking status"
    )
    async def list_all_djs(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ) -> None:
        """Query djs table and display all registered DJs with active status.

        Uses NEBULA_WHITE embed (plain list style).
        Protected by cog_slash_command_check — admin-only.

        [Source: epics.md#Story 6.5 AC3, AC4, AC5 — FR39]
        """
        if self.ctx.db is None:
            await inter.response.send_message(
                "The bot is not ready yet. Please try again in a moment.", ephemeral=True
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
