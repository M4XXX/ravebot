"""Regression: EventsCog and OutreachCog must keep the bot they are given.

disnake does not set a .bot attribute on cogs, so the background loops crashed
with AttributeError on a real gateway connection while mocked tests passed.
"""
from unittest.mock import MagicMock

from bot.cogs.events import EventsCog
from bot.cogs.outreach_cog import OutreachCog


def _bot_with_guild(guild_id: int) -> MagicMock:
    bot = MagicMock()
    guild = MagicMock()
    guild.id = guild_id
    bot.guilds = [guild]
    return bot


def test_events_cog_reads_guild_from_constructor_bot() -> None:
    cog = EventsCog(MagicMock(), _bot_with_guild(111))
    assert cog._get_guild_id() == 111


def test_outreach_cog_reads_guild_from_constructor_bot() -> None:
    cog = OutreachCog(MagicMock(), _bot_with_guild(222))
    assert cog._get_guild_id() == 222
