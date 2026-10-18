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


def test_dj_database_warning_only_in_classic_without_it(caplog):
    """Story 8.5: startup warning when DJ pages share the events database."""
    from dataclasses import replace

    from bot.config import Config
    from bot.main import _log_dj_database_setting

    base = Config(discord_bot_token="t", postgres_url="postgresql://h/db", notion_api_key="k",
                  notion_database_id="events", admin_user_ids=frozenset({1}), log_level="INFO",
                  db_pool_min=1, db_pool_max=2)
    caplog.set_level("INFO")
    _log_dj_database_setting(base)
    assert "NOTION_DJ_DATABASE_ID is not set" in caplog.text
    caplog.clear()
    _log_dj_database_setting(replace(base, notion_dj_database_id="djs"))
    assert caplog.text == ""
    _log_dj_database_setting(replace(base, notion_layout="tds"))
    assert "not set" not in caplog.text
