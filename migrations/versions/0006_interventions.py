"""Add interventions table for admin DM takeover (Story 6.2).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-21

A row in this table means the DJ's DMs are under admin control.
All automated DMs (outreach, reminders, flow prompts) are suppressed
while a DJ has a row here.

INSERT ... ON CONFLICT DO NOTHING makes start_intervention idempotent.

[Source: epics.md#Story 6.2 -- FR32, FR33]
[Source: architecture.md#Stateless Services: All Per-User State in PostgreSQL]
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interventions",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )


def downgrade() -> None:
    op.drop_table("interventions")
