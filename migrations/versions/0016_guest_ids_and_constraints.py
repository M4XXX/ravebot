"""Guest DJ id sequence, unique DJ names, status checks, drop a redundant index.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-09

- guest_dj_id_seq hands out negative ids for guest DJs (no Discord account). A
  sequence never repeats a number, so a new guest never inherits the bookings or
  history of a deleted one. It starts below the lowest id already in use.
- ux_djs_dj_name_norm makes DJ names unique the same way the bot compares them
  (trimmed, inner whitespace collapsed, lower case). It is only created when the
  existing rows have no duplicate names; otherwise a NOTICE is raised and the bot
  keeps its own check.
- Status columns get CHECK constraints. They are NOT VALID: old rows are not
  checked, every new or changed row is.
- ix_djs_discord_user_id duplicated the primary key index.
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_NAME_KEY = r"lower(regexp_replace(btrim(dj_name), '\s+', ' ', 'g'))"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE start_at bigint;
        BEGIN
            SELECT LEAST(-1, COALESCE(MIN(discord_user_id), 0) - 1) INTO start_at FROM djs;
            EXECUTE format(
                'CREATE SEQUENCE guest_dj_id_seq INCREMENT BY -1 MINVALUE %s MAXVALUE -1 '
                'START WITH %s NO CYCLE',
                -9223372036854775807, start_at
            );
        END $$;
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM djs GROUP BY {_NAME_KEY} HAVING count(*) > 1
            ) THEN
                RAISE NOTICE 'Duplicate DJ names exist: ux_djs_dj_name_norm not created';
            ELSE
                CREATE UNIQUE INDEX ux_djs_dj_name_norm ON djs ({_NAME_KEY});
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE outreach_sequences ADD CONSTRAINT ck_outreach_sequences_status "
        "CHECK (status IN ('active', 'complete', 'exhausted')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE outreach_contacts ADD CONSTRAINT ck_outreach_contacts_status "
        "CHECK (status IN ('sent', 'accepted', 'declined', 'timeout', 'unreachable')) NOT VALID"
    )
    op.execute("DROP INDEX IF EXISTS ix_djs_discord_user_id")


def downgrade() -> None:
    op.create_index("ix_djs_discord_user_id", "djs", ["discord_user_id"])
    op.execute(
        "ALTER TABLE outreach_contacts DROP CONSTRAINT IF EXISTS ck_outreach_contacts_status"
    )
    op.execute(
        "ALTER TABLE outreach_sequences DROP CONSTRAINT IF EXISTS ck_outreach_sequences_status"
    )
    op.execute("DROP INDEX IF EXISTS ux_djs_dj_name_norm")
    op.execute("DROP SEQUENCE IF EXISTS guest_dj_id_seq")
