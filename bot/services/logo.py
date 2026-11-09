"""Download and sanitise DJ logo images uploaded to Discord."""

import asyncio
import io
from typing import Literal
from urllib.parse import urlsplit

import disnake
import httpx
from PIL import Image

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_PIXELS_PER_SIDE = 4096
MAX_LOGO_TOTAL_PIXELS = 16_000_000
# The stored logo is sent as a Discord DM attachment and uploaded to Notion, so it
# is scaled down to this longest side and must stay well below both size limits.
LOGO_MAX_SIDE = 1024
MAX_LOGO_OUTPUT_BYTES = 4 * 1024 * 1024
ALLOWED_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

# MPO is the multi-picture JPEG of phone cameras; only its first frame is used.
_ALLOWED_FORMATS = ("PNG", "JPEG", "MPO")
_DOWNLOAD_TIMEOUT_SECONDS = 10.0
# Decoding a large image needs a lot of memory: process one logo at a time.
_PROCESS_LIMIT = asyncio.Semaphore(1)

LogoReason = Literal["too_big", "not_image", "bad_source", "download_failed", "many", "needs_file"]


class LogoError(Exception):
    def __init__(self, reason: LogoReason) -> None:
        super().__init__(reason)
        self.reason: LogoReason = reason


def process_logo(data: bytes) -> bytes:
    """Validate PNG/JPEG bytes and return a freshly encoded PNG without metadata.

    The result is at most LOGO_MAX_SIDE px on its longest side (aspect ratio kept,
    never scaled up) and at most MAX_LOGO_OUTPUT_BYTES, else LogoError("not_image").
    """
    if len(data) > MAX_LOGO_BYTES:
        raise LogoError("too_big")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in _ALLOWED_FORMATS:
                raise LogoError("not_image")
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_LOGO_PIXELS_PER_SIDE
                or height > MAX_LOGO_PIXELS_PER_SIDE
                or width * height > MAX_LOGO_TOTAL_PIXELS
            ):
                raise LogoError("not_image")
            if image.format == "MPO":
                image.seek(0)
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            converted = image.convert("RGBA" if has_alpha else "RGB")
            longest = max(width, height)
            if longest > LOGO_MAX_SIDE:
                scale = LOGO_MAX_SIDE / longest
                target = (max(1, round(width * scale)), max(1, round(height * scale)))
                converted = converted.resize(target, Image.Resampling.LANCZOS)
            clean = Image.frombytes(converted.mode, converted.size, converted.tobytes())
            out = io.BytesIO()
            clean.save(out, format="PNG", optimize=True)
            result = out.getvalue()
            if len(result) > MAX_LOGO_OUTPUT_BYTES:
                raise LogoError("not_image")
            return result
    except LogoError:
        raise
    except Exception as exc:
        raise LogoError("not_image") from exc


def _is_allowed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in ALLOWED_HOSTS
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


async def _fetch(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        async with client.stream(
            "GET", url, follow_redirects=False, timeout=_DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            if response.status_code != 200:
                raise LogoError("download_failed")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_LOGO_BYTES:
                    raise LogoError("too_big")
                chunks.append(chunk)
            return b"".join(chunks)
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
        raise LogoError("download_failed") from exc


async def download_attachment(
    attachment: disnake.Attachment, *, client: httpx.AsyncClient | None = None
) -> bytes:
    if not _is_allowed_url(attachment.url):
        raise LogoError("bad_source")
    if attachment.size > MAX_LOGO_BYTES:
        raise LogoError("too_big")
    try:
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT_SECONDS):
            if client is not None:
                return await _fetch(client, attachment.url)
            async with httpx.AsyncClient() as own_client:
                return await _fetch(own_client, attachment.url)
    except TimeoutError as exc:
        raise LogoError("download_failed") from exc


async def logo_from_message(message: disnake.Message) -> bytes:
    if len(message.attachments) > 1:
        raise LogoError("many")
    if not message.attachments:
        raise LogoError("needs_file")
    data = await download_attachment(message.attachments[0])
    return await process_logo_async(data)


async def process_logo_async(data: bytes) -> bytes:
    """process_logo in a worker thread, one image at a time (keeps the event loop free)."""
    async with _PROCESS_LIMIT:
        return await asyncio.to_thread(process_logo, data)
