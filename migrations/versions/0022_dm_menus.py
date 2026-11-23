"""DM menus table.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-23

dm_menus: the last menu message the bot sent to each user's DM, so the next menu
can replace it.
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dm_menus (
            user_id BIGINT PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE dm_menus")
