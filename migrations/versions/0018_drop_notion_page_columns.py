"""Drop djs.notion_dj_page_id and the event_shapes table.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-20

- Notion page ids now live in notion_mirror (copied there by 0017).
- event_shapes guarded Notion hand edits of Start / Slot length / Slots per event;
  Postgres is the source of truth now, so nothing reads it any more.
- downgrade re-creates both empty and copies the DJ page ids back from notion_mirror.
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE djs DROP COLUMN notion_dj_page_id")
    op.execute("DROP TABLE event_shapes")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE event_shapes (
            event_id TEXT PRIMARY KEY,
            start TEXT NOT NULL,
            slot_minutes INTEGER NOT NULL,
            slot_count INTEGER NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE djs ADD COLUMN notion_dj_page_id TEXT NULL")
    op.execute(
        """
        UPDATE djs SET notion_dj_page_id = m.notion_page_id
        FROM notion_mirror m
        WHERE m.entity = 'dj' AND m.entity_key = djs.discord_user_id::text
          AND m.notion_page_id IS NOT NULL
        """
    )
