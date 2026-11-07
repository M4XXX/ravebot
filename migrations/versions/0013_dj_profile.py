"""Add DJ logo columns and the dj_conversations table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-07

djs.logo_image holds the processed logo; logo_synced tells whether Notion has
it. dj_conversations keeps the open onboarding/edit DM conversation per user.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("djs", sa.Column("logo_image", sa.LargeBinary(), nullable=True))
    op.add_column(
        "djs",
        sa.Column(
            "logo_synced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_table(
        "dj_conversations",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("logo_image", sa.LargeBinary(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("discord_user_id"),
    )


def downgrade() -> None:
    op.drop_table("dj_conversations")
    op.drop_column("djs", "logo_synced")
    op.drop_column("djs", "logo_image")
