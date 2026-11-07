"""Tests for start_admin_page / stop_admin_page."""
import logging
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

from bot.web import start_admin_page, stop_admin_page

PASSWORD = "s3cret-pw-value"


def _ctx(password: str = PASSWORD, port: str = "8080") -> SimpleNamespace:
    config = SimpleNamespace(admin_page_password=password, admin_page_port=port)
    return SimpleNamespace(config=config, admins=MagicMock())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_no_password_returns_none_and_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        runner = await start_admin_page(_ctx(password=""), object())
    assert runner is None
    assert "Admin page is off (ADMIN_PAGE_PASSWORD is not set)." in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("port", ["abc", "0", "70000", "-5", "80.5"])
async def test_bad_port_returns_none_without_leaking_password(
    port: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        runner = await start_admin_page(_ctx(port=port), object())
    assert runner is None
    assert "ADMIN_PAGE_PORT" in caplog.text
    assert port in caplog.text
    assert PASSWORD not in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_port_in_use_returns_none_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        with caplog.at_level(logging.INFO):
            runner = await start_admin_page(_ctx(port=str(port)), object())
    finally:
        blocker.close()
    assert runner is None
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert "could not listen on port" in caplog.text
    assert PASSWORD not in caplog.text


@pytest.mark.asyncio
async def test_normal_start_serves_login_and_stop_frees_port(
    caplog: pytest.LogCaptureFixture,
) -> None:
    port = _free_port()
    with caplog.at_level(logging.INFO):
        runner = await start_admin_page(_ctx(port=str(port)), object())
    assert runner is not None
    try:
        assert f"Admin page listening on port {port}." in caplog.text
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/login") as resp:
                assert resp.status == 200
            async with http.get(
                f"http://127.0.0.1:{port}/", allow_redirects=False
            ) as resp:
                assert resp.status in (302, 303)
    finally:
        await stop_admin_page(runner)
    with socket.socket() as s:
        s.bind(("0.0.0.0", port))


@pytest.mark.asyncio
async def test_stop_accepts_none() -> None:
    await stop_admin_page(None)


@pytest.mark.asyncio
async def test_missing_registry_returns_none_with_generic_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _ctx(port=str(_free_port()))
    ctx.admins = None
    with caplog.at_level(logging.INFO):
        runner = await start_admin_page(ctx, object())
    assert runner is None
    assert "Admin page could not start" in caplog.text
    assert "could not listen" not in caplog.text
    assert PASSWORD not in caplog.text
