import asyncio
import io
import random
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

from bot.services import logo
from bot.services.logo import (
    MAX_LOGO_BYTES,
    MAX_LOGO_OUTPUT_BYTES,
    LogoError,
    download_attachment,
    logo_from_message,
    process_logo,
)

GOOD_URL = "https://cdn.discordapp.com/attachments/1/2/logo.png"


def make_image(fmt: str, size=(32, 32), mode="RGB", **save_kwargs) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(
        buf, format=fmt, **save_kwargs
    )
    return buf.getvalue()


def reason_of(exc_info) -> str:
    return exc_info.value.reason


def test_png_roundtrip():
    out = process_logo(make_image("PNG", (40, 20)))
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    assert img.size == (40, 20)


def test_jpeg_returns_png():
    out = process_logo(make_image("JPEG", (30, 50)))
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    assert img.size == (30, 50)


def test_alpha_preserved():
    out = process_logo(make_image("PNG", mode="RGBA"))
    assert Image.open(io.BytesIO(out)).mode == "RGBA"


def test_metadata_stripped():
    exif = Image.Exif()
    exif[0x010E] = "secret-description"
    data = make_image("JPEG", exif=exif.tobytes())
    assert b"secret-description" in data
    out = process_logo(data)
    assert b"secret-description" not in out
    assert not Image.open(io.BytesIO(out)).info.get("exif")


def test_png_text_chunk_stripped():
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "hidden-comment")
    data = make_image("PNG", pnginfo=info)
    assert b"hidden-comment" in data
    assert b"hidden-comment" not in process_logo(data)


@pytest.mark.parametrize("fmt", ["GIF", "WEBP", "BMP"])
def test_other_formats_rejected(fmt):
    with pytest.raises(LogoError) as e:
        process_logo(make_image(fmt))
    assert reason_of(e) == "not_image"


def test_svg_rejected():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    with pytest.raises(LogoError) as e:
        process_logo(svg)
    assert reason_of(e) == "not_image"


def test_random_bytes_rejected():
    with pytest.raises(LogoError) as e:
        process_logo(b"\x00\x01random bytes named logo.png" * 10)
    assert reason_of(e) == "not_image"


def test_truncated_png_rejected():
    data = make_image("PNG", (200, 200))
    with pytest.raises(LogoError) as e:
        process_logo(data[: len(data) // 2])
    assert reason_of(e) == "not_image"


def test_side_limit():
    with pytest.raises(LogoError) as e:
        process_logo(make_image("PNG", (5000, 10)))
    assert reason_of(e) == "not_image"


def test_total_pixel_limit():
    with pytest.raises(LogoError) as e:
        process_logo(make_image("PNG", (4096, 4096)))
    assert reason_of(e) == "not_image"


def test_large_but_allowed_is_downsized():
    out = process_logo(make_image("PNG", (4000, 4000)))
    assert Image.open(io.BytesIO(out)).size == (1024, 1024)


def noisy_photo_jpeg(size=(4000, 3000)) -> bytes:
    """A photo-like JPEG: noise scaled up, so it compresses like a real photo."""
    small = (size[0] // 4, size[1] // 4)
    noise = random.Random(1).randbytes(small[0] * small[1] * 3)
    img = Image.frombytes("RGB", small, noise).resize(size, Image.Resampling.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def test_big_photo_output_is_small_and_at_most_1024_px():
    data = noisy_photo_jpeg()
    assert len(data) <= MAX_LOGO_BYTES
    out = process_logo(data)
    assert len(out) <= MAX_LOGO_OUTPUT_BYTES
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    assert max(img.size) <= 1024
    assert img.size == (1024, 768)


def test_downsize_keeps_aspect_ratio_portrait():
    out = process_logo(make_image("JPEG", (600, 3000)))
    assert Image.open(io.BytesIO(out)).size == (205, 1024)


def test_small_image_is_not_upscaled():
    out = process_logo(make_image("PNG", (100, 50)))
    assert Image.open(io.BytesIO(out)).size == (100, 50)


def test_output_above_cap_is_refused(monkeypatch):
    monkeypatch.setattr(logo, "MAX_LOGO_OUTPUT_BYTES", 10)
    with pytest.raises(LogoError) as e:
        process_logo(make_image("PNG", (64, 64)))
    assert reason_of(e) == "not_image"


def test_mpo_is_accepted_as_jpeg_first_frame():
    first = Image.new("RGB", (40, 30), (250, 0, 0))
    second = Image.new("RGB", (40, 30), (0, 0, 250))
    buf = io.BytesIO()
    first.save(buf, format="MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    out = Image.open(io.BytesIO(process_logo(buf.getvalue())))
    assert out.format == "PNG"
    assert out.size == (40, 30)
    r, g, b = out.convert("RGB").getpixel((20, 15))
    assert r > 200 and b < 50


def test_too_big_bytes():
    with pytest.raises(LogoError) as e:
        process_logo(b"\x00" * (MAX_LOGO_BYTES + 1))
    assert reason_of(e) == "too_big"


def att(url=GOOD_URL, size=100) -> Any:
    return SimpleNamespace(url=url, size=size)


def client_with(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/logo.png",
        "https://cdn.discordapp.com.evil.example/logo.png",
        "http://cdn.discordapp.com/attachments/1/2/logo.png",
        "https://evil.example/https://cdn.discordapp.com/x.png",
        "https://cdn.discordapp.com@evil.example/x.png",
    ],
)
async def test_bad_source_no_request(url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x")

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(url), client=c)
    assert reason_of(e) == "bad_source"
    assert calls == []


async def test_download_ok_media_host():
    data = make_image("PNG")

    def handler(request):
        return httpx.Response(200, content=data)

    async with client_with(handler) as c:
        out = await download_attachment(
            att("https://media.discordapp.net/attachments/1/2/a.png"), client=c
        )
    assert out == data


async def test_redirect_is_download_failed():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(), client=c)
    assert reason_of(e) == "download_failed"


async def test_http_error_is_download_failed():
    async with client_with(lambda r: httpx.Response(404)) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(), client=c)
    assert reason_of(e) == "download_failed"


async def test_declared_size_too_big_no_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x")

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(size=MAX_LOGO_BYTES + 1), client=c)
    assert reason_of(e) == "too_big"
    assert calls == []


async def test_streamed_body_too_big():
    def handler(request):
        return httpx.Response(200, content=b"\x00" * (MAX_LOGO_BYTES + 10))

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(size=100), client=c)
    assert reason_of(e) == "too_big"


async def test_timeout_is_download_failed():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(), client=c)
    assert reason_of(e) == "download_failed"


def message(attachments, content="") -> Any:
    return SimpleNamespace(attachments=attachments, content=content)


async def test_two_attachments_many():
    with pytest.raises(LogoError) as e:
        await logo_from_message(message([att(), att()]))
    assert reason_of(e) == "many"


async def test_text_link_needs_file():
    with pytest.raises(LogoError) as e:
        await logo_from_message(message([], "https://example.com/logo.png"))
    assert reason_of(e) == "needs_file"


async def test_no_attachment_needs_file():
    with pytest.raises(LogoError) as e:
        await logo_from_message(message([]))
    assert reason_of(e) == "needs_file"


async def test_one_valid_attachment(monkeypatch):
    data = make_image("JPEG", (20, 20))

    async def fake_download(attachment, *, client=None):
        return data

    monkeypatch.setattr(logo, "download_attachment", fake_download)
    out = await logo_from_message(message([att()]))
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    assert img.size == (20, 20)


def test_icc_profile_and_exif_stripped():
    exif = Image.Exif()
    exif[0x010E] = "desc"
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(
        buf, format="JPEG", icc_profile=b"fake-icc-profile" * 4, exif=exif.tobytes()
    )
    out = Image.open(io.BytesIO(process_logo(buf.getvalue())))
    assert "icc_profile" not in out.info
    assert not out.info.get("exif")
    assert not out.getexif()


async def test_port_not_443_rejected():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att("https://cdn.discordapp.com:8443/x.png"), client=c)
    assert reason_of(e) == "bad_source"
    assert calls == []


async def test_explicit_443_allowed():
    async with client_with(lambda r: httpx.Response(200, content=b"abc")) as c:
        out = await download_attachment(att("https://cdn.discordapp.com:443/x.png"), client=c)
    assert out == b"abc"


async def test_overall_deadline(monkeypatch):
    monkeypatch.setattr(logo, "_DOWNLOAD_TIMEOUT_SECONDS", 0.05)

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.02)
                yield b"x"

    def handler(request):
        return httpx.Response(200, stream=Drip())

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(), client=c)
    assert reason_of(e) == "download_failed"


@pytest.mark.parametrize("exc", [httpx.InvalidURL("bad"), httpx.StreamError("broken")])
async def test_invalid_url_and_stream_error(exc):
    def handler(request):
        raise exc

    async with client_with(handler) as c:
        with pytest.raises(LogoError) as e:
            await download_attachment(att(), client=c)
    assert reason_of(e) == "download_failed"


async def test_logo_processing_runs_one_at_a_time(monkeypatch):
    assert isinstance(logo._PROCESS_LIMIT, asyncio.Semaphore)
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def slow_process(data):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return b"png"

    async def fake_download(attachment, *, client=None):
        return b"data"

    monkeypatch.setattr(logo, "download_attachment", fake_download)
    monkeypatch.setattr(logo, "process_logo", slow_process)
    outs = await asyncio.gather(*(logo_from_message(message([att()])) for _ in range(3)))
    assert outs == [b"png"] * 3
    assert state["max"] == 1
