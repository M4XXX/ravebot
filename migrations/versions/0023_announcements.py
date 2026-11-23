"""Announcement rules and posts tables.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-23

announcement_rules: per-guild "post N days before at HH:MM" rules.
announcement_posts: one row per (event, kind, rule) announcement message. There is
no foreign key to the rules, so deleting a rule keeps its posts.
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE announcement_rules (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            days_before INTEGER NOT NULL CHECK (days_before BETWEEN 0 AND 365),
            post_time TIME NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE announcement_posts (
            id SERIAL PRIMARY KEY,
            event_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            rule_id INTEGER,
            channel_id BIGINT NOT NULL,
            message_id BIGINT,
            state TEXT NOT NULL DEFAULT 'claimed',
            digest TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX announcement_posts_unique
            ON announcement_posts (event_id, kind, COALESCE(rule_id, 0))
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE announcement_posts")
    op.execute("DROP TABLE announcement_rules")
