"""Event location, hand markers and the schedule_rules table.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21

- events.location: where the event takes place.
- events.genre_by_hand / location_by_hand: the value was set by hand, so rules
  and defaults never overwrite it. Existing genres count as hand values.
- schedule_rules: recurring genre / location rules per guild.
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN location TEXT NULL")
    op.execute("ALTER TABLE events ADD COLUMN genre_by_hand BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE events ADD COLUMN location_by_hand BOOLEAN NOT NULL DEFAULT false")
    op.execute("UPDATE events SET genre_by_hand = true WHERE genre IS NOT NULL")
    op.execute(
        """
        CREATE TABLE schedule_rules (
            id BIGSERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            field TEXT NOT NULL CHECK (field IN ('genre', 'location')),
            value TEXT NOT NULL,
            every INTEGER NOT NULL CHECK (every BETWEEN 1 AND 52),
            anchor DATE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX schedule_rules_guild_id_idx ON schedule_rules (guild_id)")


def downgrade() -> None:
    op.execute("DROP TABLE schedule_rules")
    op.execute("ALTER TABLE events DROP COLUMN location_by_hand")
    op.execute("ALTER TABLE events DROP COLUMN genre_by_hand")
    op.execute("ALTER TABLE events DROP COLUMN location")
