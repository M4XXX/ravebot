"""Tests for bot/config.py — Config.from_env() validation and parsing."""
import sys

import pytest


def _fresh_config(monkeypatch: pytest.MonkeyPatch) -> type:
    """Import Config freshly, bypassing any cached module state."""
    monkeypatch.delitem(sys.modules, "bot.config", raising=False)
    from bot.config import Config  # noqa: PLC0415
    return Config


class TestConfigMissingVars:
    def test_exits_when_all_required_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sys.exit() called when all required env vars are absent."""
        for key in (
            "DISCORD_BOT_TOKEN",
            "POSTGRES_URL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "ADMIN_USER_IDS",
        ):
            monkeypatch.delenv(key, raising=False)

        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()

        message = str(exc.value)
        assert "DISCORD_BOT_TOKEN" in message

    def test_exits_naming_each_missing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit message names every missing variable."""
        for key in (
            "DISCORD_BOT_TOKEN",
            "POSTGRES_URL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "ADMIN_USER_IDS",
        ):
            monkeypatch.delenv(key, raising=False)

        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()

        message = str(exc.value)
        for var in (
            "DISCORD_BOT_TOKEN",
            "POSTGRES_URL",
            "NOTION_API_KEY",
            "NOTION_DATABASE_ID",
            "ADMIN_USER_IDS",
        ):
            assert var in message, f"Expected {var} in exit message"

    def test_exits_when_single_var_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sys.exit() with message even when only one var is missing."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/test")
        monkeypatch.setenv("NOTION_API_KEY", "secret_key")
        monkeypatch.setenv("NOTION_DATABASE_ID", "test-db-id")
        monkeypatch.delenv("ADMIN_USER_IDS", raising=False)

        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()

        assert "ADMIN_USER_IDS" in str(exc.value)


class TestConfigParsing:
    def _set_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "test_token_abc")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://user:pw@localhost:5432/ravebot")
        monkeypatch.setenv("NOTION_API_KEY", "secret_notion_key_xyz")
        monkeypatch.setenv("NOTION_DATABASE_ID", "test-notion-db-id")
        monkeypatch.setenv("ADMIN_USER_IDS", "111,222,333")

    def test_parses_admin_user_ids_as_frozenset_of_ints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_required(monkeypatch)
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.admin_user_ids == frozenset({111, 222, 333})

    def test_parses_admin_user_ids_with_spaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_required(monkeypatch)
        monkeypatch.setenv("ADMIN_USER_IDS", " 111 , 222 , 333 ")
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.admin_user_ids == frozenset({111, 222, 333})

    def test_default_log_level_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_required(monkeypatch)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.log_level == "INFO"

    def test_default_pool_min_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_required(monkeypatch)
        monkeypatch.delenv("DB_POOL_MIN", raising=False)
        monkeypatch.delenv("DB_POOL_MAX", raising=False)
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.db_pool_min == 5
        assert config.db_pool_max == 20

    def test_custom_pool_sizes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_required(monkeypatch)
        monkeypatch.setenv("DB_POOL_MIN", "3")
        monkeypatch.setenv("DB_POOL_MAX", "10")
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.db_pool_min == 3
        assert config.db_pool_max == 10

    def test_stores_required_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_required(monkeypatch)
        Config = _fresh_config(monkeypatch)
        config = Config.from_env()
        assert config.discord_bot_token == "test_token_abc"
        assert config.postgres_url == "postgresql://user:pw@localhost:5432/ravebot"
        assert config.notion_api_key == "secret_notion_key_xyz"
        assert config.notion_database_id == "test-notion-db-id"

    def test_exits_when_admin_ids_only_commas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADMIN_USER_IDS containing only commas must not silently produce empty frozenset."""
        self._set_required(monkeypatch)
        monkeypatch.setenv("ADMIN_USER_IDS", ",,,")
        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()
        assert "ADMIN_USER_IDS" in str(exc.value)


def test_repr_hides_secrets():
    from bot.config import Config

    cfg = Config(
        discord_bot_token="tok-discord-1",
        postgres_url="postgresql://u:pw-secret-2@h/db",
        notion_api_key="secret_notion_3",
        notion_database_id="db-id",
        admin_user_ids=frozenset({1}),
        log_level="INFO",
        db_pool_min=1,
        db_pool_max=2,
    )
    text = repr(cfg)
    for secret in ("tok-discord-1", "pw-secret-2", "secret_notion_3"):
        assert secret not in text


@pytest.mark.parametrize(("raw", "expected"), [(None, None), ("", None), ("  ", None),
                                               (" dj-db-id ", "dj-db-id")])
def test_notion_dj_database_id(monkeypatch: pytest.MonkeyPatch, raw, expected) -> None:
    """Story 8.5: optional NOTION_DJ_DATABASE_ID; empty means the events database."""
    for key, value in (("DISCORD_BOT_TOKEN", "tok"), ("POSTGRES_URL", "postgresql://h/db"),
                       ("NOTION_API_KEY", "secret_key"), ("NOTION_DATABASE_ID", "events-db"),
                       ("ADMIN_USER_IDS", "1")):
        monkeypatch.setenv(key, value)
    if raw is None:
        monkeypatch.delenv("NOTION_DJ_DATABASE_ID", raising=False)
    else:
        monkeypatch.setenv("NOTION_DJ_DATABASE_ID", raw)
    assert _fresh_config(monkeypatch).from_env().notion_dj_database_id == expected
