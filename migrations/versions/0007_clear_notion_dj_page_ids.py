"""Clear stored Notion DJ page IDs (Notion rework).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06

The stored IDs point to pages in the old Notion database. The new DJs
database starts empty; ensure_dj_page recreates rows on demand.
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE djs SET notion_dj_page_id = NULL")


def downgrade() -> None:
    # No-op: the cleared IDs of the old Notion database cannot be restored.
    pass
