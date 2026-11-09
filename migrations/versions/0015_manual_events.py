"""Add the manual_events table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-09

Events created by hand with /create event. The event scheduler ignores them: they do
not count toward the kept-ready number and do not move the schedule. Every other event
counts as scheduled.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_events",
        sa.Column("page_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("page_id"),
    )


def downgrade() -> None:
    op.drop_table("manual_events")
