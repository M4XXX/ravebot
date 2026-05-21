"""Add outreach_sequences table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20

Tracks proactive outreach sequences per Notion event.
One row per event_id; status transitions: active → complete (all slots filled)
or active → exhausted (all eligible DJs contacted, slots remain open, Story 5.3).

[Source: epics.md#Story 5.1 AC2]
[Source: architecture.md#Data Architecture — Config Storage]
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outreach_sequences",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("outreach_sequences")
