"""Admins cog: /admins add, remove and list (bot owners only).

The gate is the shared admin gate of AdminCog (server only, admin, idle rule) plus an owner
check that runs for every sub command before any callback.
"""
import logging

import disnake
from disnake.ext import commands

from bot.cogs import SERVER_ONLY
from bot.cogs.admin import AdminCog
from bot.context import BotContext
from bot.services.admin_management import AdminChange, AdminManagementService, clean_name
from bot.ui.embeds import _safe_dj_name

logger = logging.getLogger(__name__)

MAX_MESSAGE = 1900
DENIED = "You don't have permission to use this command."
OWNER_ONLY = "Only the bot owners can manage the admin list."
GENERIC_ERROR = "Something went wrong. Please try again in a moment."
UNKNOWN_USER = "unknown user"


def _actor(inter: disnake.ApplicationCommandInteraction) -> str:
    return clean_name(inter.author.display_name) or str(inter.author.id)


def _answer(change: AdminChange, verb: str) -> str:
    """The reply text; a success names the person with the name escaped for Discord."""
    if change.ok and change.name:
        return f"{verb} {_safe_dj_name(change.name, 80)}."
    return change.message


class NotOwner(commands.CheckFailure):
    """An admin who is not an owner tried to manage the admin list."""


def chunk_lines(lines: list[str], limit: int = MAX_MESSAGE) -> list[str]:
    """Join lines into messages of at most `limit` characters, never splitting a line."""
    messages: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


class AdminsCog(commands.Cog):
    def __init__(self, ctx: BotContext, service: AdminManagementService) -> None:
        self.ctx = ctx
        self.service = service

    async def cog_slash_command_check(self, inter: disnake.ApplicationCommandInteraction) -> bool:
        if not await AdminCog.cog_slash_command_check(self, inter):
            return False
        admins = getattr(self.ctx, "admins", None)
        if admins is None or not admins.is_owner(inter.author.id):
            raise NotOwner("owner only")
        return True

    async def cog_slash_command_error(
        self, inter: disnake.ApplicationCommandInteraction, error: Exception
    ) -> None:
        if isinstance(error, NotOwner):
            if not inter.response.is_done():
                await inter.response.send_message(OWNER_ONLY, ephemeral=True)
        elif isinstance(error, commands.CheckFailure):
            if not inter.response.is_done():
                await inter.response.send_message(DENIED, ephemeral=True)
        else:
            logger.error("Unexpected error in /admins command: %s", error, exc_info=True)
            if not inter.response.is_done():
                await inter.response.send_message(GENERIC_ERROR, ephemeral=True)
            else:
                await inter.followup.send(GENERIC_ERROR, ephemeral=True)

    @commands.slash_command(
        name="admins", description="Manage the admin list (owners only)", contexts=SERVER_ONLY
    )
    async def admins_base(self, inter: disnake.ApplicationCommandInteraction) -> None:
        pass

    @admins_base.sub_command(name="add", description="Make a person an admin")
    async def admins_add(
        self, inter: disnake.ApplicationCommandInteraction, user: disnake.User
    ) -> None:
        await inter.response.defer(ephemeral=True)
        change = await self.service.add(user.id, actor=_actor(inter))
        await inter.followup.send(_answer(change, "Added"), ephemeral=True)

    @admins_base.sub_command(name="remove", description="Remove an added admin")
    async def admins_remove(
        self, inter: disnake.ApplicationCommandInteraction, user: disnake.User
    ) -> None:
        await inter.response.defer(ephemeral=True)
        change = await self.service.remove(user.id, actor=_actor(inter))
        await inter.followup.send(_answer(change, "Removed"), ephemeral=True)

    @admins_base.sub_command(name="list", description="Show the owners and added admins")
    async def admins_list(self, inter: disnake.ApplicationCommandInteraction) -> None:
        await inter.response.defer(ephemeral=True)
        rows = await self.service.list_admins()
        lines = [
            f"{'Owner' if is_owner else 'Admin'}: "
            f"{_safe_dj_name(name, 80) if name else UNKNOWN_USER} (ID {uid})"
            for uid, name, is_owner in rows
        ]
        for message in chunk_lines(lines) or ["No admins."]:
            await inter.followup.send(message, ephemeral=True)
