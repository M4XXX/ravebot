"""Management lineup messages table.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-22

management_lineup_messages: the Discord messages posted per event in the DJ
Management channel. Empty message_ids means suppressed (event was fully booked
when "Only new" was chosen).
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE management_lineup_messages (
            event_id TEXT PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            message_ids BIGINT[] NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE management_lineup_messages")
