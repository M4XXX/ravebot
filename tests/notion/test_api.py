"""Tests for bot/notion/api.py — NotionApi retry, backoff and pagination."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.notion.api import NotionApi, NotionWriteError
from bot.ui.embeds import ErrorCode

API_KEY = "secret_super_key_123"


def make_log() -> MagicMock:
    log = MagicMock()
    log.post_error = AsyncMock(return_value=None)
    return log


def make_api(handler, log: MagicMock | None = None) -> NotionApi:
    return NotionApi(
        API_KEY, log or make_log(), transport=httpx.MockTransport(handler)
    )


async def test_sends_notion_version_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    api = make_api(handler)
    assert await api.request("GET", "/users/me", None, "ctx") == {"ok": True}
    assert seen["notion-version"] == "2026-03-11"
    assert seen["authorization"] == f"Bearer {API_KEY}"
    await api.close()


async def test_succeeds_on_third_attempt_after_two_500s() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"id": "p"})

    log = make_log()
    api = make_api(handler, log)
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await api.request("POST", "/pages", {}, "ctx")
    assert result == {"id": "p"}
    assert [c.args[0] for c in sleep.call_args_list] == [1.0, 2.0]
    log.post_error.assert_not_called()


async def test_three_failures_raise_and_post_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    log = make_log()
    api = make_api(handler, log)
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(NotionWriteError):
            await api.request("POST", "/pages", {}, "ctx")
    log.post_error.assert_awaited_once()
    assert log.post_error.call_args.kwargs["error_code"] == ErrorCode.ERR_NOTION_WRITE


async def test_4xx_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={})

    log = make_log()
    api = make_api(handler, log)
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(NotionWriteError):
            await api.request("POST", "/pages", {}, "ctx")
    assert calls["n"] == 1
    sleep.assert_not_called()
    log.post_error.assert_awaited_once()


async def test_429_honours_retry_after() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"ok": 1})

    api = make_api(handler)
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()) as sleep:
        await api.request("GET", "/x", None, "ctx")
    assert sleep.call_args_list[0].args[0] >= 2


async def test_query_all_follows_cursor() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content) if request.content else {}
        bodies.append(body)
        assert request.url.path.endswith("/data_sources/ds1/query")
        start = len(bodies) - 1
        size = 100 if start < 2 else 50
        results = [{"id": f"{start}-{i}"} for i in range(size)]
        more = start < 2
        return httpx.Response(
            200,
            json={
                "results": results,
                "has_more": more,
                "next_cursor": f"c{start + 1}" if more else None,
            },
        )

    api = make_api(handler)
    pages = await api.query_all("ds1", {"page_size": 100})
    assert len(pages) == 250
    assert len(bodies) == 3
    assert "start_cursor" not in bodies[0]
    assert bodies[1]["start_cursor"] == "c1"


async def test_api_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    log = make_log()
    api = make_api(handler, log)
    with caplog.at_level(logging.DEBUG):
        with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(NotionWriteError) as exc:
                await api.request("POST", "/pages", {}, "ctx")
    assert API_KEY not in caplog.text
    assert API_KEY not in str(exc.value)
    assert API_KEY not in repr(log.post_error.call_args)


async def test_tolerated_status_raises_immediately_without_log_or_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"object": "error"})

    log = make_log()
    api = make_api(handler, log)
    with pytest.raises(NotionWriteError) as exc:
        await api.request("GET", "/x", None, "ctx", tolerate=(404,))
    assert exc.value.status_code == 404
    assert calls == 1
    log.post_error.assert_not_awaited()
    await api.close()


async def test_final_failure_carries_status_code() -> None:
    api = make_api(lambda r: httpx.Response(400, json={}))
    with pytest.raises(NotionWriteError) as exc:
        await api.request("GET", "/x", None, "ctx")
    assert exc.value.status_code == 400
    await api.close()


async def test_network_failure_has_no_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    api = make_api(handler)
    with patch("bot.notion.api.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(NotionWriteError) as exc:
            await api.request("GET", "/x", None, "ctx")
    assert exc.value.status_code is None
    await api.close()
