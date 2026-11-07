"""Add the web_admins table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-07

Admins added at runtime (through the admin page). The owners from ADMIN_USER_IDS
stay in the environment and are never stored here.
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_admins",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )


def downgrade() -> None:
    op.drop_table("web_admins")
