"""Resolve the DJ and invites roles from stored settings.

Stored ids (config keys ``dj_role_id`` and ``invites_role_id``) win. Only when
the key is missing do we fall back to the old role names. A stored id whose
role no longer exists returns None without falling back.
"""
import disnake

from bot.db.config import get_value

DJ_ROLE_KEY = "dj_role_id"
INVITES_ROLE_KEY = "invites_role_id"


async def _stored_role(db, guild: disnake.Guild, key: str) -> tuple[bool, disnake.Role | None]:
    raw = await get_value(db, guild.id, key)
    if raw is None:
        return False, None
    try:
        return True, guild.get_role(int(raw))
    except ValueError:
        return True, None


async def get_dj_role(db, guild: disnake.Guild) -> disnake.Role | None:
    found, role = await _stored_role(db, guild, DJ_ROLE_KEY)
    if found:
        return role
    return next((r for r in guild.roles if r.name.lower() == "dj"), None)


async def get_invites_role(db, guild: disnake.Guild) -> disnake.Role | None:
    found, role = await _stored_role(db, guild, INVITES_ROLE_KEY)
    if found:
        return role
    return disnake.utils.get(guild.roles, name="Active Booking")
