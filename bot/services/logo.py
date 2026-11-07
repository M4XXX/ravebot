"""Download and sanitise DJ logo images uploaded to Discord."""

import io
from typing import Literal
from urllib.parse import urlsplit

import disnake
import httpx
from PIL import Image

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_PIXELS_PER_SIDE = 4096
MAX_LOGO_TOTAL_PIXELS = 16_000_000
ALLOWED_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

_ALLOWED_FORMATS = ("PNG", "JPEG")
_DOWNLOAD_TIMEOUT_SECONDS = 10.0

LogoReason = Literal["too_big", "not_image", "bad_source", "download_failed", "many", "needs_file"]


class LogoError(Exception):
    def __init__(self, reason: LogoReason) -> None:
        super().__init__(reason)
        self.reason: LogoReason = reason


def process_logo(data: bytes) -> bytes:
    """Validate PNG/JPEG bytes and return a freshly encoded PNG without metadata."""
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
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            converted = image.convert("RGBA" if has_alpha else "RGB")
            out = io.BytesIO()
            converted.save(out, format="PNG")
            return out.getvalue()
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
    except httpx.HTTPError as exc:
        raise LogoError("download_failed") from exc


async def download_attachment(
    attachment: disnake.Attachment, *, client: httpx.AsyncClient | None = None
) -> bytes:
    if not _is_allowed_url(attachment.url):
        raise LogoError("bad_source")
    if attachment.size > MAX_LOGO_BYTES:
        raise LogoError("too_big")
    if client is not None:
        return await _fetch(client, attachment.url)
    async with httpx.AsyncClient() as own_client:
        return await _fetch(own_client, attachment.url)


async def logo_from_message(message: disnake.Message) -> bytes:
    if len(message.attachments) > 1:
        raise LogoError("many")
    if not message.attachments:
        raise LogoError("needs_file")
    data = await download_attachment(message.attachments[0])
    return process_logo(data)
