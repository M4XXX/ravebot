"""Tests for bot/db/djs.py — DJ repository functions."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_get_dj_by_discord_id_returns_none_when_not_found() -> None:
    """Returns None when no DJ row exists for the given user ID."""
    from bot.db.djs import get_dj_by_discord_id

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    result = await get_dj_by_discord_id(pool, 99999)

    assert result is None
    pool.fetchrow.assert_awaited_once_with(
        "SELECT * FROM djs WHERE discord_user_id = $1", 99999
    )


@pytest.mark.asyncio
async def test_get_dj_by_discord_id_returns_record_when_found() -> None:
    """Returns the asyncpg Record when a DJ row exists."""
    from bot.db.djs import get_dj_by_discord_id

    mock_record = MagicMock()
    mock_record["discord_user_id"] = 12345
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=mock_record)

    result = await get_dj_by_discord_id(pool, 12345)

    assert result is mock_record


@pytest.mark.asyncio
async def test_insert_dj_stores_profile() -> None:
    """insert_dj executes INSERT with correct positional values."""
    from bot.db.djs import insert_dj

    pool = MagicMock()
    pool.execute = AsyncMock()

    await insert_dj(
        pool,
        discord_user_id=99999,
        dj_name="TestDJ",
        vrchat_name="VRTestDJ",
        logo_url="https://i.imgur.com/test.png",
        stream_url_primary="https://twitch.tv/testdj",
        stream_url_backup="https://twitch.tv/testdj_backup",
    )

    pool.execute.assert_awaited_once()
    call_args = pool.execute.call_args[0]
    sql: str = call_args[0]
    params = call_args[1:]
    assert "INSERT INTO djs" in sql
    assert params[0] == 99999      # discord_user_id
    assert params[1] == "TestDJ"   # dj_name
    assert params[2] == "VRTestDJ" # vrchat_name
    assert params[3] == "https://i.imgur.com/test.png"  # logo_url
    assert params[4] == "https://twitch.tv/testdj"      # stream_url_primary
    assert params[5] == "https://twitch.tv/testdj_backup"  # stream_url_backup


@pytest.mark.asyncio
async def test_insert_dj_with_optional_fields_none() -> None:
    """insert_dj accepts None for all optional fields."""
    from bot.db.djs import insert_dj

    pool = MagicMock()
    pool.execute = AsyncMock()

    await insert_dj(
        pool,
        discord_user_id=11111,
        dj_name="MinimalDJ",
        vrchat_name=None,
        logo_url=None,
        stream_url_primary="https://twitch.tv/minimal",
        stream_url_backup=None,
    )

    pool.execute.assert_awaited_once()
    call_args = pool.execute.call_args[0]
    params = call_args[1:]
    assert params[2] is None   # vrchat_name
    assert params[3] is None   # logo_url
    assert params[5] is None   # stream_url_backup


@pytest.mark.asyncio
async def test_update_active_booking_sets_true() -> None:
    """update_active_booking returns True when DJ row is found and updated."""
    from bot.db.djs import update_active_booking

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")

    result = await update_active_booking(pool, discord_user_id=12345, active_booking=True)

    assert result is True
    pool.execute.assert_awaited_once()
    call_args = pool.execute.call_args[0]
    assert "UPDATE djs" in call_args[0]
    assert call_args[1] is True   # active_booking value
    assert call_args[2] == 12345  # discord_user_id


@pytest.mark.asyncio
async def test_update_active_booking_not_found_returns_false() -> None:
    """update_active_booking returns False when no DJ row exists."""
    from bot.db.djs import update_active_booking

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")

    result = await update_active_booking(pool, discord_user_id=99999, active_booking=True)

    assert result is False


# ---------------------------------------------------------------------------
# Story 5.2: get_active_booking_djs, update_last_contacted_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_booking_djs_returns_list():
    from bot.db.djs import get_active_booking_djs

    pool = MagicMock()
    fake_rows = [{"discord_user_id": 111, "dj_name": "DJ One", "last_contacted_at": None}]
    pool.fetch = AsyncMock(return_value=fake_rows)
    result = await get_active_booking_djs(pool)
    assert len(result) == 1
    assert result[0]["discord_user_id"] == 111
    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args[0][0]
    assert "active_booking" in sql
    assert "last_contacted_at" in sql


@pytest.mark.asyncio
async def test_get_active_booking_djs_returns_empty():
    from bot.db.djs import get_active_booking_djs

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    result = await get_active_booking_djs(pool)
    assert result == []


@pytest.mark.asyncio
async def test_update_last_contacted_at_calls_execute():
    from bot.db.djs import update_last_contacted_at

    pool = MagicMock()
    pool.execute = AsyncMock()
    await update_last_contacted_at(pool, 999)
    pool.execute.assert_awaited_once()
    sql = pool.execute.call_args[0][0]
    assert "last_contacted_at" in sql
    assert pool.execute.call_args[0][1] == 999


# ---------------------------------------------------------------------------
# Story 6.5: get_all_djs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_djs_returns_all_rows():
    """get_all_djs returns a list of all DJ rows as dicts."""
    from bot.db.djs import get_all_djs

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[
        {"discord_user_id": 1, "dj_name": "Alpha DJ", "active_booking": True},
        {"discord_user_id": 2, "dj_name": "Beta DJ", "active_booking": False},
    ])

    result = await get_all_djs(pool)

    assert len(result) == 2
    assert result[0]["dj_name"] == "Alpha DJ"
    assert result[0]["active_booking"] is True
    assert result[1]["dj_name"] == "Beta DJ"
    assert result[1]["active_booking"] is False
    pool.fetch.assert_awaited_once()
    sql = pool.fetch.call_args[0][0]
    assert "FROM djs" in sql
    assert "ORDER BY dj_name ASC" in sql


@pytest.mark.asyncio
async def test_get_all_djs_returns_empty_when_no_djs():
    """get_all_djs returns empty list when no DJs are registered."""
    from bot.db.djs import get_all_djs

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])

    result = await get_all_djs(pool)

    assert result == []


@pytest.mark.asyncio
async def test_clear_all_notion_dj_page_ids_clears_every_row() -> None:
    from bot.db.djs import clear_all_notion_dj_page_ids

    pool = MagicMock()
    pool.execute = AsyncMock()
    await clear_all_notion_dj_page_ids(pool)
    pool.execute.assert_awaited_once_with("UPDATE djs SET notion_dj_page_id = NULL")


@pytest.mark.asyncio
async def test_insert_dj_logo_image_sets_unsynced() -> None:
    from bot.db.djs import insert_dj

    pool = MagicMock()
    pool.execute = AsyncMock()
    kwargs = dict(
        discord_user_id=1, dj_name="A", vrchat_name=None, logo_url=None,
        stream_url_primary="https://twitch.tv/a", stream_url_backup=None,
    )
    await insert_dj(pool, logo_image=b"img", **kwargs)
    sql, *params = pool.execute.await_args.args
    assert "logo_image" in sql and "logo_synced" in sql
    assert b"img" in params and False in params
    await insert_dj(pool, **kwargs)
    _, *params = pool.execute.await_args.args
    assert None in params and True in params


@pytest.mark.asyncio
async def test_update_dj_fields_rejects_unknown_key() -> None:
    from bot.db.djs import update_dj_fields

    pool = MagicMock()
    pool.execute = AsyncMock()
    with pytest.raises(ValueError):
        await update_dj_fields(pool, 1, dj_name="A", evil="x; DROP")
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_dj_fields_updates_and_reports() -> None:
    from bot.db.djs import update_dj_fields

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    assert await update_dj_fields(pool, 5, dj_name="New") is True
    sql, *params = pool.execute.await_args.args
    assert "UPDATE djs SET dj_name = $1" in sql and "discord_user_id = $2" in sql
    assert params == ["New", 5]
    pool.execute = AsyncMock(return_value="UPDATE 0")
    assert await update_dj_fields(pool, 5, dj_name="New") is False


@pytest.mark.asyncio
async def test_update_dj_fields_logo_image_resets_url_and_sync() -> None:
    from bot.db.djs import update_dj_fields

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    await update_dj_fields(pool, 5, logo_image=b"img")
    sql, *params = pool.execute.await_args.args
    assert "logo_image = $1" in sql
    assert "logo_url = NULL" in sql
    assert "logo_synced = false" in sql
    assert params == [b"img", 5]


@pytest.mark.asyncio
async def test_update_dj_fields_logo_image_respects_explicit_synced() -> None:
    from bot.db.djs import update_dj_fields

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    await update_dj_fields(pool, 5, logo_image=b"img", logo_synced=True)
    sql, *params = pool.execute.await_args.args
    assert "logo_synced = $2" in sql and "logo_synced = false" not in sql
    assert params == [b"img", True, 5]


@pytest.mark.asyncio
async def test_dj_name_exists_passes_cleaned_name_and_exclusion() -> None:
    from bot.db.djs import dj_name_exists

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=True)
    assert await dj_name_exists(pool, "  DJ   Foo ", exclude_user_id=9) is True
    sql, *params = pool.fetchval.await_args.args
    assert "lower(regexp_replace(btrim(dj_name)" in sql
    assert params == ["dj foo", 9]
    pool.fetchval = AsyncMock(return_value=None)
    assert await dj_name_exists(pool, "x") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected"),
    [("DJ Stra\u00dfe", "dj stra\u00dfe"), ("\ufb01re DJ", "\ufb01re dj")],
)
async def test_dj_name_exists_lowercases_like_sql_lower(name, expected) -> None:
    """Python side uses str.lower() like SQL lower(): sharp s and the fi ligature
    stay as they are (casefold would turn them into "ss" and "fi" and never match)."""
    from bot.db.djs import dj_name_exists

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    await dj_name_exists(pool, name)
    assert pool.fetchval.await_args.args[1] == expected


@pytest.mark.asyncio
async def test_logo_helpers() -> None:
    from bot.db.djs import get_logo_image, list_unsynced_logo_user_ids, set_logo_synced

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=b"img")
    assert await get_logo_image(pool, 1) == b"img"
    pool.execute = AsyncMock()
    await set_logo_synced(pool, 1, True)
    sql, *params = pool.execute.await_args.args
    assert "logo_synced = $1" in sql and params == [True, 1]
    pool.fetch = AsyncMock(return_value=[{"discord_user_id": 3}, {"discord_user_id": 4}])
    assert await list_unsynced_logo_user_ids(pool) == [3, 4]
    assert "logo_synced = false" in pool.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_unsynced_logo_query_includes_djs_without_image() -> None:
    from bot.db.djs import list_unsynced_logo_user_ids

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    await list_unsynced_logo_user_ids(pool)
    assert "logo_image" not in pool.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_next_guest_id_counts_down_below_zero() -> None:
    from bot.db.djs import next_guest_id

    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    assert await next_guest_id(pool) == -1
    pool.fetchval = AsyncMock(return_value=123456789012345678)  # only real users
    assert await next_guest_id(pool) == -1
    pool.fetchval = AsyncMock(return_value=-4)
    assert await next_guest_id(pool) == -5


@pytest.mark.asyncio
async def test_delete_dj_reports_whether_a_row_went() -> None:
    from bot.db.djs import delete_dj

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 1")
    assert await delete_dj(pool, -1) is True
    pool.execute = AsyncMock(return_value="DELETE 0")
    assert await delete_dj(pool, -1) is False


@pytest.mark.asyncio
async def test_active_booking_pool_excludes_guests() -> None:
    from bot.db.djs import get_active_booking_djs

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    await get_active_booking_djs(pool)
    assert "discord_user_id > 0" in pool.fetch.call_args.args[0]
