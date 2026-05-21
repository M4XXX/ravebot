"""Add outreach_contacts table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-20

Tracks per-DJ contact history within each outreach sequence.
One row per (event_id, discord_user_id) pair; composite PK prevents
a DJ from being contacted twice in the same sequence.

Status values: sent, accepted, declined, timeout, unreachable.

[Source: epics.md#Story 5.2 AC1-AC5]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outreach_contacts",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="sent"),
        sa.Column(
            "contacted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("event_id", "discord_user_id"),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["outreach_sequences.event_id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("outreach_contacts")
