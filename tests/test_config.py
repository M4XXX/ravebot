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
            "NOTION_PARENT_PAGE_ID",
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
            "NOTION_PARENT_PAGE_ID",
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
            "NOTION_PARENT_PAGE_ID",
            "ADMIN_USER_IDS",
        ):
            assert var in message, f"Expected {var} in exit message"

    def test_exits_when_single_var_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sys.exit() with message even when only one var is missing."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/test")
        monkeypatch.setenv("NOTION_API_KEY", "secret_key")
        monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "test-parent-id")
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
        monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "test-notion-parent-id")
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
        assert config.notion_parent_page_id == "test-notion-parent-id"

    def test_exits_when_admin_ids_only_commas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADMIN_USER_IDS containing only commas must not silently produce empty frozenset."""
        self._set_required(monkeypatch)
        monkeypatch.setenv("ADMIN_USER_IDS", ",,,")
        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()
        assert "ADMIN_USER_IDS" in str(exc.value)


class TestNotionParentPage:
    def _set_others(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
        monkeypatch.setenv("POSTGRES_URL", "postgresql://localhost/test")
        monkeypatch.setenv("NOTION_API_KEY", "secret_key")
        monkeypatch.setenv("ADMIN_USER_IDS", "1")

    def test_missing_notion_parent_page_id_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_others(monkeypatch)
        monkeypatch.delenv("NOTION_PARENT_PAGE_ID", raising=False)
        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()
        assert "NOTION_PARENT_PAGE_ID" in str(exc.value)

    def test_notion_database_id_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_others(monkeypatch)
        monkeypatch.delenv("NOTION_PARENT_PAGE_ID", raising=False)
        monkeypatch.setenv("NOTION_DATABASE_ID", "old-db-id")
        Config = _fresh_config(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            Config.from_env()
        assert "NOTION_PARENT_PAGE_ID" in str(exc.value)

    def test_notion_parent_page_id_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._set_others(monkeypatch)
        monkeypatch.setenv("NOTION_PARENT_PAGE_ID", "page-123")
        Config = _fresh_config(monkeypatch)
        assert Config.from_env().notion_parent_page_id == "page-123"


class TestAdminPageConfig:
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k, v in {
            "DISCORD_BOT_TOKEN": "t",
            "POSTGRES_URL": "postgresql://x",
            "NOTION_API_KEY": "k",
            "NOTION_PARENT_PAGE_ID": "p",
            "ADMIN_USER_IDS": "1",
        }.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("ADMIN_PAGE_PASSWORD", raising=False)
        monkeypatch.delenv("ADMIN_PAGE_PORT", raising=False)

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        cfg = _fresh_config(monkeypatch).from_env()
        assert cfg.admin_page_password == ""
        assert cfg.admin_page_port == "8080"

    def test_reads_and_strips_both_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        monkeypatch.setenv("ADMIN_PAGE_PASSWORD", "  pw  ")
        monkeypatch.setenv("ADMIN_PAGE_PORT", " 9090 ")
        cfg = _fresh_config(monkeypatch).from_env()
        assert cfg.admin_page_password == "pw"
        assert cfg.admin_page_port == "9090"

    def test_empty_port_means_8080(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        monkeypatch.setenv("ADMIN_PAGE_PORT", "   ")
        assert _fresh_config(monkeypatch).from_env().admin_page_port == "8080"


def test_repr_does_not_contain_the_admin_page_password() -> None:
    from bot.config import Config

    config = Config(
        discord_bot_token="t", postgres_url="p", notion_api_key="n",
        notion_parent_page_id="x", admin_user_ids=frozenset({1}), log_level="INFO",
        db_pool_min=1, db_pool_max=2, admin_page_password="hunter2-secret",
    )
    assert "hunter2-secret" not in repr(config)
