"""Schedule cog: /schedule show, default, apply and rule add/edit/remove (admin only)."""
import datetime
import logging

import disnake
from disnake.ext import commands

from bot.cogs import SERVER_ONLY
from bot.cogs.admin import AdminCog
from bot.context import BotContext
from bot.db import schedule_rules as db_rules
from bot.services import activation
from bot.services.schedule import RuleConflict
from bot.services.schedule_rules import Rule
from bot.ui import texts
from bot.ui.gates import refuse_in_maintenance
from bot.ui.schedule_views import ApplyChangesView

logger = logging.getLogger(__name__)

ANCHOR_CHOICES = 25
FIELDS = ["genre", "location"]


class ScheduleCog(commands.Cog):
    """Admin only; the gate is the one AdminCog uses."""

    cog_slash_command_check = AdminCog.cog_slash_command_check
    cog_slash_command_error = AdminCog.cog_slash_command_error

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    # -- helpers ----------------------------------------------------------------------

    async def _ready(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """False after answering: maintenance, or the service is not up."""
        if await refuse_in_maintenance(inter, self.ctx):
            return False
        if self.ctx.db is None or self.ctx.schedule_service is None:
            await inter.response.send_message(texts.SCHEDULE_NOT_READY, ephemeral=True)
            return False
        return True

    async def _anchor_labels(self, guild_id: int) -> dict[datetime.date, str]:
        dates = await self.ctx.schedule_service.upcoming_dates(guild_id, ANCHOR_CHOICES)
        return {d.date: d.label for d in dates}

    @staticmethod
    def _label(labels: dict[datetime.date, str], day: datetime.date) -> str:
        return labels.get(day) or texts.schedule_date_label(texts.PROJECTED_EVENT_NAME, day)

    def _line(self, rule: Rule, labels: dict[datetime.date, str]) -> str:
        return texts.rule_line(
            rule.id, rule.field, rule.value, rule.every, self._label(labels, rule.anchor)
        )

    async def _reply_after_change(
        self, inter: disnake.ApplicationCommandInteraction, guild_id: int, text: str
    ) -> None:
        pending = await self.ctx.schedule_service.pending_changes(guild_id)
        if not pending:
            reply = f"{text}\n{texts.SCHEDULE_NO_CHANGES}".strip()
            await inter.followup.send(reply, ephemeral=True)
            return
        view = ApplyChangesView(self.ctx, guild_id, inter.author.id, pending)
        view.origin = inter
        await inter.followup.send(view.content(text), view=view, ephemeral=True)

    async def _active_admin(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        """Autocomplete runs no cog check: admins only, nothing while idle."""
        if not self.ctx.is_admin(inter.author.id):
            return False
        return (
            self.ctx.db is not None
            and self.ctx.schedule_service is not None
            and inter.guild_id is not None
            and await activation.is_active(self.ctx.db, inter.guild_id)
        )

    # -- commands ---------------------------------------------------------------------

    @commands.slash_command(
        name="schedule", description="Genre and location rules (admin only)", contexts=SERVER_ONLY
    )
    async def schedule_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @schedule_base.sub_command(name="show", description="Show defaults, rules and next events")
    async def schedule_show(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        guild_id = inter.guild_id
        service = self.ctx.schedule_service
        labels = await self._anchor_labels(guild_id)
        defaults = []
        for field in FIELDS:
            value = await db_rules.get_default(self.ctx.db, guild_id, field)
            if value:
                defaults.append(f'{texts.field_name(field)}: "{value}"')
        rules = [self._line(r, labels) for r in await db_rules.list_rules(self.ctx.db, guild_id)]
        events = []
        for d in await service.upcoming_dates(guild_id, 10):
            if d.event is not None:
                e = d.event
                events.append(
                    texts.schedule_event_line(
                        d.label, e.genre, e.genre_by_hand, e.location, e.location_by_hand
                    )
                )
            else:
                genre = await service.resolved(guild_id, "genre", d.date)
                location = await service.resolved(guild_id, "location", d.date)
                events.append(texts.schedule_event_line(d.label, genre, False, location, False))
        await inter.followup.send(texts.schedule_show(defaults, rules, events), ephemeral=True)

    @schedule_base.sub_command(name="default", description="Set or clear a default value")
    async def schedule_default(
        self,
        inter: disnake.ApplicationCommandInteraction,
        field: str = commands.Param(description="What to set", choices=FIELDS),
        value: str | None = commands.Param(None, description="The value (empty clears it)"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        try:
            await self.ctx.schedule_service.set_default(inter.guild_id, field, value)  # type: ignore[arg-type]
        except ValueError as exc:
            await inter.followup.send(str(exc), ephemeral=True)
            return
        stored = await db_rules.get_default(self.ctx.db, inter.guild_id, field)  # type: ignore[arg-type]
        await self._reply_after_change(inter, inter.guild_id, texts.default_set(field, stored))

    @schedule_base.sub_command(name="apply", description="Apply rules to upcoming events")
    async def schedule_apply(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        await self._reply_after_change(inter, inter.guild_id, "")

    @schedule_base.sub_command_group(name="rule", description="Repeating genre or location")
    async def rule_group(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @rule_group.sub_command(name="add", description="Add a rule")
    async def rule_add(
        self,
        inter: disnake.ApplicationCommandInteraction,
        field: str = commands.Param(description="What the rule sets", choices=FIELDS),
        value: str = commands.Param(description="The value"),
        every: int = commands.Param(description="Every how many events", ge=1, le=52),
        start: str = commands.Param(description="The first event of the rule"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        guild_id = inter.guild_id
        try:
            anchor = datetime.date.fromisoformat(start)
        except ValueError:
            await inter.followup.send(texts.RULE_ANCHOR_NOT_UPCOMING, ephemeral=True)
            return
        rule = Rule(None, field, value, every, anchor)  # type: ignore[arg-type]
        try:
            added = await self.ctx.schedule_service.add_rule(guild_id, rule)
        except RuleConflict as exc:
            await self._send_conflict(inter, guild_id, exc)
            return
        except ValueError as exc:
            await inter.followup.send(str(exc), ephemeral=True)
            return
        labels = await self._anchor_labels(guild_id)
        await self._reply_after_change(
            inter, guild_id, texts.rule_added(self._line(added, labels))
        )

    @rule_add.autocomplete("start")
    async def rule_start_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> dict[str, str]:
        if not await self._active_admin(inter):
            return {}
        assert inter.guild_id is not None
        try:
            dates = await self.ctx.schedule_service.upcoming_dates(
                inter.guild_id, ANCHOR_CHOICES
            )
        except Exception:
            logger.warning("start autocomplete failed", exc_info=True)
            return {}
        needle = user_input.strip().casefold()
        return {
            d.label: d.date.isoformat() for d in dates if needle in d.label.casefold()
        }

    async def _send_conflict(
        self, inter: disnake.ApplicationCommandInteraction, guild_id: int, exc: RuleConflict
    ) -> None:
        labels = await self._anchor_labels(guild_id)
        lines = [
            (
                r.id,
                texts.rule_summary(
                    r.field, r.value, r.every, self._label(labels, r.anchor)
                ),
            )
            for r in exc.rules
        ]
        await inter.followup.send(texts.rule_conflict(lines), ephemeral=True)

    @rule_group.sub_command(name="edit", description="Change a rule")
    async def rule_edit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        rule: str = commands.Param(description="The rule (pick one from the list)"),
        value: str | None = commands.Param(None, description="The new value"),
        every: int | None = commands.Param(
            None, description="Every how many events", ge=1, le=52
        ),
        start: str | None = commands.Param(None, description="The first event of the rule"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        guild_id = inter.guild_id
        if value is None and every is None and start is None:
            await inter.response.send_message(texts.RULE_EDIT_NEEDS_OPTION, ephemeral=True)
            return
        await inter.response.defer(ephemeral=True)
        stored = await self._stored_rule(guild_id, rule)
        if stored is None:
            await inter.followup.send(texts.RULE_NOT_FOUND, ephemeral=True)
            return
        anchor = stored.anchor
        if start is not None:
            try:
                anchor = datetime.date.fromisoformat(start)
            except ValueError:
                await inter.followup.send(texts.RULE_ANCHOR_NOT_UPCOMING, ephemeral=True)
                return
        new = Rule(
            stored.id,
            stored.field,
            stored.value if value is None else value,
            stored.every if every is None else every,
            anchor,
        )
        try:
            saved = await self.ctx.schedule_service.edit_rule(guild_id, new)
        except RuleConflict as exc:
            await self._send_conflict(inter, guild_id, exc)
            return
        except LookupError:
            await inter.followup.send(texts.RULE_NOT_FOUND, ephemeral=True)
            return
        except ValueError as exc:
            await inter.followup.send(str(exc), ephemeral=True)
            return
        labels = await self._anchor_labels(guild_id)
        await self._reply_after_change(
            inter, guild_id, texts.rule_edited(self._line(saved, labels))
        )

    @rule_edit.autocomplete("start")
    async def rule_edit_start_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> dict[str, str]:
        return await self.rule_start_autocomplete(inter, user_input)

    @rule_group.sub_command(name="remove", description="Remove a rule")
    async def rule_remove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        rule: str = commands.Param(description="The rule (pick one from the list)"),
    ) -> None:
        if not await self._ready(inter):
            return
        assert inter.guild_id is not None
        await inter.response.defer(ephemeral=True)
        stored = await self._stored_rule(inter.guild_id, rule)
        if stored is None or stored.id is None:
            await inter.followup.send(texts.RULE_NOT_FOUND, ephemeral=True)
            return
        if not await self.ctx.schedule_service.remove_rule(inter.guild_id, stored.id):
            await inter.followup.send(texts.RULE_NOT_FOUND, ephemeral=True)
            return
        await self._reply_after_change(inter, inter.guild_id, texts.rule_removed(stored.id))

    async def _stored_rule(self, guild_id: int, raw: str) -> Rule | None:
        try:
            rule_id = int(raw)
        except ValueError:
            return None
        return await db_rules.get_rule(self.ctx.db, guild_id, rule_id)

    @rule_edit.autocomplete("rule")
    async def rule_edit_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> dict[str, str]:
        return await self._rule_choices(inter, user_input)

    @rule_remove.autocomplete("rule")
    async def rule_remove_autocomplete(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> dict[str, str]:
        return await self._rule_choices(inter, user_input)

    async def _rule_choices(
        self, inter: disnake.ApplicationCommandInteraction, user_input: str
    ) -> dict[str, str]:
        if not await self._active_admin(inter):
            return {}
        assert inter.guild_id is not None
        try:
            rules = await db_rules.list_rules(self.ctx.db, inter.guild_id)
            labels = await self._anchor_labels(inter.guild_id)
        except Exception:
            logger.warning("rule autocomplete failed", exc_info=True)
            return {}
        needle = user_input.strip().casefold()
        out = {}
        for r in rules:
            name = texts.option_label(self._line(r, labels))
            if needle in name.casefold():
                out[name] = str(r.id)
        return dict(list(out.items())[:25])
