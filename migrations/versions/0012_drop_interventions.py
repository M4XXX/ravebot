"""Drop the interventions table (the /intervene flow is removed).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("interventions")


def downgrade() -> None:
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
