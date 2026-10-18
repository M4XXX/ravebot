"""Config — loads all environment variables at startup.

Fails fast with a descriptive message if any required variable is missing.
All secrets (token, API key, DB URL) come exclusively from env vars — never from files.
[Source: architecture.md#Secrets: Environment Variables Only]
"""
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from environment variables.

    All required fields are validated at construction time via from_env().
    Optional fields use safe defaults.
    """

    discord_bot_token: str
    postgres_url: str
    notion_api_key: str
    notion_database_id: str
    admin_user_ids: frozenset[int]
    log_level: str
    db_pool_min: int
    db_pool_max: int
    guild_id: int | None = None
    # "classic": the bot's own database (Name, Date, Slots, DJs, Discord ID).
    # "tds": an organizers' plan with one text column per hour slot (docs/user-guide.md 2.6).
    notion_layout: str = "classic"
    # Status names DJs may book in, in the order given (empty: every event is bookable).
    # The first one is set on events the bot creates in the tds layout.
    notion_bookable_statuses: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        """Load config from environment variables.

        Exits immediately with a descriptive message if any required variable is absent.
        All missing variables are reported together so the user can fix all at once.
        """
        missing: list[str] = []

        def require(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                missing.append(key)
                return ""
            return val

        discord_bot_token = require("DISCORD_BOT_TOKEN")
        postgres_url = require("POSTGRES_URL")
        notion_api_key = require("NOTION_API_KEY")
        notion_database_id = require("NOTION_DATABASE_ID")
        raw_admin_ids = require("ADMIN_USER_IDS")

        if missing:
            sys.exit(f"Missing required environment variables: {', '.join(missing)}")

        admin_ids = frozenset(
            int(uid.strip()) for uid in raw_admin_ids.split(",") if uid.strip()
        )
        if not admin_ids:
            sys.exit("ADMIN_USER_IDS must contain at least one valid integer Discord user ID")

        raw_guild_id = os.getenv("GUILD_ID", "").strip()
        if raw_guild_id and not raw_guild_id.isdigit():
            sys.exit("GUILD_ID must be a numeric Discord server ID")

        notion_layout = os.getenv("NOTION_LAYOUT", "classic").strip().lower() or "classic"
        if notion_layout not in ("classic", "tds"):
            sys.exit("NOTION_LAYOUT must be 'classic' or 'tds'")
        bookable = tuple(
            s.strip() for s in os.getenv("NOTION_BOOKABLE_STATUSES", "").split(",") if s.strip()
        )

        return cls(
            discord_bot_token=discord_bot_token,
            postgres_url=postgres_url,
            notion_api_key=notion_api_key,
            notion_database_id=notion_database_id,
            admin_user_ids=admin_ids,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            db_pool_min=int(os.getenv("DB_POOL_MIN", "5")),
            db_pool_max=int(os.getenv("DB_POOL_MAX", "20")),
            guild_id=int(raw_guild_id) if raw_guild_id else None,
            notion_layout=notion_layout,
            notion_bookable_statuses=bookable,
        )
