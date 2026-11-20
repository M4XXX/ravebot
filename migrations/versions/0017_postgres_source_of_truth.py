"""Events, Notion mirror, outbox and sync issues.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-20

- events holds the bot's own events (Postgres is the source of truth).
- notion_mirror keeps, per entity, the Notion page id and the last synced state.
- notion_outbox queues changes to push to Notion.
- sync_issues records conflicts and warnings found by the Notion poll.
- Existing DJ Notion page ids are copied into notion_mirror.
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE events (
            uid TEXT PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            date DATE NOT NULL,
            start TEXT NOT NULL,
            slot_minutes INT NOT NULL,
            slot_count INT NOT NULL CHECK (slot_count BETWEEN 1 AND 100),
            status TEXT NULL,
            genre TEXT NULL,
            notes TEXT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE notion_mirror (
            entity TEXT NOT NULL CHECK (entity IN ('event', 'slot', 'dj', 'botdata')),
            entity_key TEXT NOT NULL,
            notion_page_id TEXT NULL,
            synced_hash TEXT NULL,
            synced_payload JSONB NULL,
            synced_at TIMESTAMPTZ NULL,
            PRIMARY KEY (entity, entity_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE notion_outbox (
            entity TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            op TEXT NOT NULL DEFAULT 'upsert' CHECK (op IN ('upsert', 'delete')),
            notion_page_id TEXT NULL,
            enqueued_at TIMESTAMPTZ DEFAULT now(),
            attempts INT DEFAULT 0,
            next_attempt_at TIMESTAMPTZ DEFAULT now(),
            last_error TEXT NULL,
            PRIMARY KEY (entity, entity_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sync_issues (
            id BIGSERIAL PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('conflict', 'warning')),
            entity TEXT NULL,
            entity_key TEXT NULL,
            summary TEXT NOT NULL,
            pg_payload JSONB NULL,
            notion_payload JSONB NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            resolved_at TIMESTAMPTZ NULL,
            resolution TEXT NULL
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_sync_issues_open_conflict
        ON sync_issues (entity, entity_key)
        WHERE resolved_at IS NULL AND kind = 'conflict'
        """
    )
    op.execute(
        """
        INSERT INTO notion_mirror (entity, entity_key, notion_page_id)
        SELECT 'dj', discord_user_id::text, notion_dj_page_id
        FROM djs WHERE notion_dj_page_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE sync_issues")
    op.execute("DROP TABLE notion_outbox")
    op.execute("DROP TABLE notion_mirror")
    op.execute("DROP TABLE events")
