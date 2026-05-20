"""Add slot_locks table and notion_dj_page_id column on djs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-20

Tables/columns added:
  slot_locks — pessimistic row-level locking for concurrent slot bookings (FR17, FR19)
  djs.notion_dj_page_id — stores the Notion page ID for DJ records, used when writing
                           bookings to the Notion event's DJs relation (Story 4.2)

[Source: epics.md#Story 4.2 AC2]
[Source: architecture.md#Slot Booking Atomicity: SELECT...FOR UPDATE in Transaction]
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # slot_locks: one row per (event_id, slot_index) combination.
    # discord_user_id = NULL  → slot available
    # discord_user_id = value → slot claimed by that DJ
    # SELECT...FOR UPDATE on this row provides the pessimistic lock.
    op.create_table(
        "slot_locks",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id", "slot_index"),
    )

    # Nullable: DJs onboarded before Story 4.2 will not have this value until
    # they re-onboard or it is backfilled. Booking flow handles None gracefully.
    op.add_column(
        "djs",
        sa.Column("notion_dj_page_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("djs", "notion_dj_page_id")
    op.drop_table("slot_locks")
