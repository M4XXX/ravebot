"""Add reminders_sent table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-21

Tracks per-DJ, per-slot reminder delivery to prevent duplicate reminders.
One row per (event_id, discord_user_id, slot_index) — composite PK prevents
a DJ from receiving more than one reminder for the same slot.

Intentionally has no FK constraint on event_id: events are managed in Notion
(not PostgreSQL) so there is no local events table to reference. The composite
PK alone is sufficient for deduplication.

[Source: epics.md#Story 5.4 AC2, AC4]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders_sent",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("event_id", "discord_user_id", "slot_index"),
    )


def downgrade() -> None:
    op.drop_table("reminders_sent")
