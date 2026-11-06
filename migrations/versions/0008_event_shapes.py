"""Add event_shapes table: last accepted Start / Slot length / Slots per event.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06

event_id is the Notion page id of the Events row (same key as slot_locks).
The sync service compares each event's current Start and Slot length with the
stored row and restores them in Notion while a slot of the event holds a DJ.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_shapes",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("start", sa.Text(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), nullable=False),
        sa.Column("slot_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("event_shapes")
