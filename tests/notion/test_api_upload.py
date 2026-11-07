"""Tests for NotionApi.upload_file."""
import httpx
import pytest

from bot.notion.api import NotionWriteError
from tests.notion.test_api import make_api


async def test_upload_creates_then_sends_multipart() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/send"):
            return httpx.Response(200, json={"id": "up1", "status": "uploaded"})
        return httpx.Response(200, json={"id": "up1", "status": "pending"})

    api = make_api(handler)
    assert await api.upload_file("logo.png", "image/png", b"PNGDATA", "ctx") == "up1"
    assert len(seen) == 2
    assert seen[0].url.path.endswith("/file_uploads")
    assert seen[0].headers["content-type"] == "application/json"
    assert b'"filename"' in seen[0].content and b"image/png" in seen[0].content
    assert seen[1].url.path.endswith("/file_uploads/up1/send")
    assert seen[1].headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in seen[1].content and b"PNGDATA" in seen[1].content
    assert seen[1].headers["notion-version"] == "2026-03-11"
    assert seen[1].headers["authorization"].startswith("Bearer ")
    await api.close()


async def test_upload_send_4xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/send"):
            return httpx.Response(400, json={})
        return httpx.Response(200, json={"id": "up1"})

    api = make_api(handler)
    with pytest.raises(NotionWriteError):
        await api.upload_file("logo.png", "image/png", b"x", "ctx")
    await api.close()
