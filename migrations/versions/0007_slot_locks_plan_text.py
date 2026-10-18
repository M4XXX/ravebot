"""Remember the text the bot wrote into a plan's slot column (NOTION_LAYOUT=tds).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18

Cancelling empties the column only while it still holds exactly this text, and /admin book
overwrites only this text of a displaced DJ. Comparing with the DJ's current profile name
instead left a stale name behind after a rename, and could empty a name an organizer had
typed in that happened to match.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("slot_locks", sa.Column("plan_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("slot_locks", "plan_text")
