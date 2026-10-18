"""Startup self-check: tell the organizer what is missing before DJs notice.

Run once when the bot is ready. It looks at the served guild only and never changes
anything; problems are posted to the admin log (or the console when no log channel exists).
"""
import logging
from typing import Any

import disnake

logger = logging.getLogger(__name__)

# Organizers give the DJ role; the bot recognises it in any letter case (views.is_registered_dj)
# and never assigns it, so its position does not matter. The bot assigns Active Booking and
# finds it by its exact name (disnake.utils.get), so that one must match exactly and sit
# below the bot's role.
DJ_ROLE = "DJ"
BOOKING_ROLE = "Active Booking"
CHANNEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "read_message_history")


def _role(guild: Any, name: str) -> Any:
    return next((r for r in guild.roles if r.name.lower() == name.lower()), None)


def check_guild(guild: Any, dj_channel_id: str | None, log_channel_id: str | None) -> list[str]:
    """Problems with roles, role order and channel permissions in the served guild."""
    problems: list[str] = []
    me = guild.me
    if not me.guild_permissions.manage_roles:
        problems.append("The bot lacks Manage Roles (needed to give DJs the Active Booking role).")
    if _role(guild, DJ_ROLE) is None:
        problems.append(f"Role '{DJ_ROLE}' does not exist.")
    booking = next((r for r in guild.roles if r.name == BOOKING_ROLE), None)
    if booking is None:
        near = _role(guild, BOOKING_ROLE)
        hint = f" ('{near.name}' exists, but the name must match exactly)" if near else ""
        problems.append(f"Role '{BOOKING_ROLE}' does not exist{hint}.")
    elif booking >= me.top_role:
        problems.append(f"Move the bot's role above '{BOOKING_ROLE}' so it can assign it.")
    for label, raw in (("DJ channel", dj_channel_id), ("log channel", log_channel_id)):
        if raw is None:
            problems.append(f"No {label} set (/config set {label.split()[0].lower()}-channel).")
            continue
        channel = guild.get_channel(int(raw))
        if channel is None:
            problems.append(f"The {label} ({raw}) no longer exists or is not visible to the bot.")
            continue
        perms = channel.permissions_for(me)
        missing = [p.replace("_", " ") for p in CHANNEL_PERMISSIONS if not getattr(perms, p)]
        if missing:
            problems.append(f"In #{channel.name} the bot is missing: {', '.join(missing)}.")
    return problems


async def run(bot: Any, ctx: Any, guild_id: int) -> list[str]:
    """Check the served guild once and report the result."""
    from bot.db.config import get_value

    guild = bot.get_guild(guild_id)
    if guild is None or ctx.db is None:
        return []
    problems = check_guild(
        guild,
        await get_value(ctx.db, guild_id, "dj_channel"),
        await get_value(ctx.db, guild_id, "log_channel"),
    )
    if not problems:
        logger.info("Self-check: roles, role order and channel permissions are fine")
        return problems
    logger.warning("Self-check found %d problem(s): %s", len(problems), problems)
    if ctx.log_service is not None:
        try:
            await ctx.log_service.post_notice(
                "**RaveBot setup check**\n" + "\n".join(f"• {p}" for p in problems)
            )
        except disnake.HTTPException:
            logger.exception("Could not post the self-check result")
    return problems
