"""Add log_replies table: which user a log channel message entry belongs to.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06

message_id is the id of the log channel message ("Message from ..." entry);
the Reply button looks up the user to answer from it, also after a restart.
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "log_replies",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )


def downgrade() -> None:
    op.drop_table("log_replies")
