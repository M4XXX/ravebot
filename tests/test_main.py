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


def test_setup_logging_installs_redacting_formatter(capsys):
    import logging

    from bot.main import _setup_logging
    from bot.redact import RedactingFormatter

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        _setup_logging("INFO", ["tok-supersecret"])
        assert all(isinstance(h.formatter, RedactingFormatter) for h in root.handlers)
        logging.getLogger("t").error("leak tok-supersecret")
        err = capsys.readouterr().err
        assert "tok-supersecret" not in err
        assert "leak ***" in err
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
