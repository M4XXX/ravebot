"""Initial schema: djs table, config table, ix_djs_discord_user_id index.

Revision ID: 0001
Revises:
Create Date: 2026-05-18

Tables created:
  djs    — DJ profile storage (FR2, FR6, FR7, FR42)
  config — Runtime configuration key/value per guild (FR34–FR37)

[Source: epics.md#Story 1.2 Acceptance Criteria]
[Source: architecture.md#Data Architecture — Config Storage: PostgreSQL Config Table]
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "djs",
        # Primary key — Discord snowflake user ID (exceeds 32-bit int range → BigInteger)
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        # Profile fields set during onboarding modal (Story 2.2)
        sa.Column("dj_name", sa.Text(), nullable=False),
        sa.Column("vrchat_name", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("stream_url_primary", sa.Text(), nullable=True),
        sa.Column("stream_url_backup", sa.Text(), nullable=True),
        # Booking pool participation flag (FR7, Story 2.3)
        sa.Column(
            "active_booking",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        # Set to true once onboarding modal is submitted (Story 2.2)
        sa.Column(
            "onboarding_complete",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        # Tracks progress through multi-step onboarding flow (Story 2.2)
        sa.Column(
            "onboarding_step",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # Timestamp of last proactive outreach contact (Story 5.2)
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
        # Row creation timestamp — timezone-aware for consistent scheduler comparisons
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )
    # Index on primary key column — required by AC2; also accelerates per-user lookups (FR42)
    op.create_index("ix_djs_discord_user_id", "djs", ["discord_user_id"])

    op.create_table(
        "config",
        # guild_id + key form the composite primary key (one row per config value per guild)
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        # Updated on every /config set command (Story 1.4, Stories 7.2–7.3)
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("guild_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("config")
    op.drop_index("ix_djs_discord_user_id", table_name="djs")
    op.drop_table("djs")
