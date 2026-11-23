"""Announcements cog: /announcements commands and the posting loop (admin only)."""
import datetime
import logging

import disnake
from disnake.ext import commands, tasks

from bot.cogs import SERVER_ONLY
from bot.cogs.admin import AdminCog
from bot.cogs.loop_support import prepare_loop, run_cycle
from bot.context import BotContext
from bot.db import announcements as db_ann
from bot.services import announcement_settings as settings
from bot.services.announcement_template import DEFAULT_TEMPLATES, GENERAL, START
from bot.ui import texts
from bot.ui.gates import refuse_in_maintenance
from bot.ui.setup_options import parse_hhmm
from bot.ui.setup_wizard import missing_permissions

logger = logging.getLogger(__name__)

STATES = ["on", "off"]
MAX_DAYS = 365


def _hhmm(value: datetime.time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _line(rule: db_ann.AnnouncementRule) -> str:
    return texts.announce_rule_line(rule.id, rule.days_before, _hhmm(rule.post_time))


def _parse_time(raw: str) -> datetime.time | None:
    clean = parse_hhmm(raw)
    if clean is None:
        return None
    return datetime.time(int(clean[:2]), int(clean[3:]))


class AnnouncementsCog(commands.Cog):
    """Admin only; the gate is the one AdminCog uses."""

    cog_slash_command_check = AdminCog.cog_slash_command_check
    cog_slash_command_error = AdminCog.cog_slash_command_error

    bot: commands.Bot  # injected by _add_cog

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    def cog_load(self) -> None:
        self._announce_loop.start()

    def cog_unload(self) -> None:
        self._announce_loop.cancel()

    # -- loop -------------------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def _announce_loop(self) -> None:
        await run_cycle("announcements", self._announce_cycle)

    async def _announce_cycle(self) -> None:
        if self.ctx.maintenance:  # an import replaces the data: no writes until it is done
            logger.debug("announcements: maintenance, skipping cycle")
            return
        service = self.ctx.announcement_service
        if service is None:
            return
        await service.tick()

    @_announce_loop.before_loop
    async def _before_announce_loop(self) -> None:
        await prepare_loop(self.bot, self.ctx.db)

    # -- helpers ----------------------------------------------------------------------

    async def _ready(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """False after answering: maintenance, or the bot is not up."""
        if await refuse_in_maintenance(inter, self.ctx):
            return False
        if self.ctx.db is None or inter.guild_id is None:
            await inter.response.send_message(texts.ANNOUNCE_NOT_READY, ephemeral=True)
            return False
        return True

    # -- commands ---------------------------------------------------------------------

    @commands.slash_command(
        name="announcements",
        description="Event announcements (admin only)",
        contexts=SERVER_ONLY,
    )
    async def announcements_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @announcements_base.sub_command(name="show", description="Show the announcement settings")
    async def ann_show(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        current = await settings.load_settings(self.ctx.db, inter.guild_id)
        rules = await db_ann.list_rules(self.ctx.db, inter.guild_id)
        channel = f"<#{current.channel_id}>" if current.channel_id else "not set"
        text = texts.announce_show(
            channel,
            current.general_enabled,
            current.start_enabled,
            [_line(r) for r in rules],
            current.general_template != DEFAULT_TEMPLATES[GENERAL],
            current.start_template != DEFAULT_TEMPLATES[START],
        )
        await inter.followup.send(text, ephemeral=True)

    @announcements_base.sub_command(name="channel", description="Set or clear the channel")
    async def ann_channel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel | None = commands.Param(
            None, description="The channel (empty clears it)"
        ),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        if channel is None:
            await settings.set_channel(self.ctx.db, inter.guild_id, None)
            await inter.followup.send(texts.ANNOUNCE_CHANNEL_CLEARED, ephemeral=True)
            return
        missing = missing_permissions(channel, inter.guild.me)  # type: ignore[union-attr]
        if missing:
            await inter.followup.send(
                texts.announce_channel_unusable(channel.mention, missing), ephemeral=True
            )
            return
        await settings.set_channel(self.ctx.db, inter.guild_id, channel.id)
        await inter.followup.send(texts.announce_channel_set(channel.mention), ephemeral=True)

    async def _switch(
        self, inter: disnake.ApplicationCommandInteraction, kind: str, state: str
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        guild_id = inter.guild_id
        on = state == "on"
        current = await settings.load_settings(self.ctx.db, guild_id)
        was_on = current.general_enabled if kind == GENERAL else current.start_enabled
        if was_on == on:
            await inter.followup.send(texts.announce_switch_already(kind, on), ephemeral=True)
            return
        before = await db_ann.list_rules(self.ctx.db, guild_id) if kind == GENERAL and on else []
        await settings.set_enabled(self.ctx.db, guild_id, kind, on)
        parts = [texts.announce_switch_changed(kind, on)]
        if kind == GENERAL and on and not before:
            after = await db_ann.list_rules(self.ctx.db, guild_id)
            if after:
                parts.append(texts.announce_default_rule_created(_line(after[0])))
        if on and current.channel_id is None:
            parts.append(texts.ANNOUNCE_NOTHING_POSTS)
        await inter.followup.send("\n".join(parts), ephemeral=True)

    @announcements_base.sub_command(
        name="general", description="Turn the general announcement on or off"
    )
    async def ann_general(
        self,
        inter: disnake.ApplicationCommandInteraction,
        state: str = commands.Param(description="On or off", choices=STATES),
    ) -> None:
        await self._switch(inter, GENERAL, state)

    @announcements_base.sub_command(
        name="start", description="Turn the start announcement on or off"
    )
    async def ann_start(
        self,
        inter: disnake.ApplicationCommandInteraction,
        state: str = commands.Param(description="On or off", choices=STATES),
    ) -> None:
        await self._switch(inter, START, state)

    @announcements_base.sub_command_group(name="rule", description="When to post")
    async def rule_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @rule_group.sub_command(name="add", description="Add a posting rule")
    async def rule_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        days: int = commands.Param(description="Days before the event", ge=0, le=MAX_DAYS),
        time: str = commands.Param(description="Time of day, HH:MM"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        problem = self._invalid(days, time)
        if problem:
            await inter.followup.send(problem, ephemeral=True)
            return
        rule = await db_ann.add_rule(self.ctx.db, inter.guild_id, days, _parse_time(time))  # type: ignore[arg-type]
        await inter.followup.send(texts.announce_rule_added(_line(rule)), ephemeral=True)

    @rule_group.sub_command(name="edit", description="Change a posting rule")
    async def rule_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: int = commands.Param(description="The rule id (see /announcements show)"),
        days: int | None = commands.Param(
            None, description="Days before the event", ge=0, le=MAX_DAYS
        ),
        time: str | None = commands.Param(None, description="Time of day, HH:MM"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        if days is None and time is None:
            await inter.response.send_message(
                texts.ANNOUNCE_RULE_EDIT_NEEDS_OPTION, ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True)
        problem = self._invalid(days, time)
        if problem:
            await inter.followup.send(problem, ephemeral=True)
            return
        guild_id = inter.guild_id
        stored = next(
            (r for r in await db_ann.list_rules(self.ctx.db, guild_id) if r.id == id), None
        )
        if stored is None:
            await inter.followup.send(texts.ANNOUNCE_RULE_NOT_FOUND, ephemeral=True)
            return
        new_days = stored.days_before if days is None else days
        new_time = stored.post_time if time is None else _parse_time(time)
        saved = await db_ann.update_rule(self.ctx.db, guild_id, id, new_days, new_time)  # type: ignore[arg-type]
        if saved is None:
            await inter.followup.send(texts.ANNOUNCE_RULE_NOT_FOUND, ephemeral=True)
            return
        await inter.followup.send(texts.announce_rule_edited(_line(saved)), ephemeral=True)

    @rule_group.sub_command(name="remove", description="Remove a posting rule")
    async def rule_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        id: int = commands.Param(description="The rule id (see /announcements show)"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        if not await db_ann.delete_rule(self.ctx.db, inter.guild_id, id):
            await inter.followup.send(texts.ANNOUNCE_RULE_NOT_FOUND, ephemeral=True)
            return
        await inter.followup.send(texts.announce_rule_removed(id), ephemeral=True)

    @staticmethod
    def _invalid(days: int | None, time: str | None) -> str | None:
        if days is not None and not 0 <= days <= MAX_DAYS:
            return texts.ANNOUNCE_RULE_DAYS_INVALID
        if time is not None and _parse_time(time) is None:
            return texts.ANNOUNCE_RULE_TIME_INVALID
        return None
