"""DJ channel lineup posts table.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-22

dj_channel_lineup_posts: the last lineup post the bot made in each DJ channel,
so the next post for the same event can replace it.
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dj_channel_lineup_posts (
            channel_id BIGINT PRIMARY KEY,
            event_id TEXT NOT NULL,
            message_id BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE dj_channel_lineup_posts")
