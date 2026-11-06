"""Add fully_booked_events table: events already announced as fully booked.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06

event_id is the Notion page id of the Events row (same key as slot_locks).
A row exists while the event is full and its "Event fully booked" log entry
was posted; it is deleted again when a slot opens, so a refill announces anew.
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fully_booked_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column(
            "announced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("fully_booked_events")
