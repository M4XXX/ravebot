"""Tests for bot/main.py — bot factory and startup configuration."""
from bot.main import create_bot


class TestCreateBot:
    def test_members_intent_enabled(self) -> None:
        """members intent must be True — required for on_member_update (FR1 DJ role detection)."""
        bot = create_bot()
        assert bot.intents.members is True

    def test_default_intents_enabled(self) -> None:
        """Default intents are present alongside the members intent."""
        bot = create_bot()
        assert bot.intents.guilds is True
