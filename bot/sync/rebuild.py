"""Rebuild Postgres from Notion (disaster recovery) and the startup recovery cases.

``rebuild_from_notion`` replaces events, slots, DJs and the Bot data tables with what
Notion holds: safety export, maintenance on, ``import_from_notion(mode="replace")``
(one transaction, which also restores the Bot data rows), then the DJ logos are
downloaded from Notion (its file URLs are signed and expire), maintenance off and a
summary in the admin log. ``web_admins`` is never touched. Logos normally flow from
Postgres to Notion only; this is the one exception, and the restored logos are marked
synced so the mirror does not upload them again.

``run_startup_recovery`` runs after the upgrade import:
- Postgres empty (no DJs, no events) and Notion holds events: rebuild.
- Postgres filled and Notion empty (no events, no DJs, e.g. a fresh set of databases):
  forget the stale mirror rows and queue everything for Notion (``resync_all``).
Neither runs without Notion. A failure is logged; startup goes on.
"""
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from bot.backup.export import write_safety_export
from bot.db.config import get_value, set_value
from bot.results import ActionResult
from bot.services.logo import LogoError, download_logo_file
from bot.sync.notion_import import (
    CONFIG_GUILD_ID,
    PG_TRUTH_IMPORTED_KEY,
    ImportResult,
    import_from_notion,
)
from bot.ui import texts

__all__ = [
    "PG_TRUTH_IMPORTED_KEY",
    "RebuildReport",
    "post_rebuild_result",
    "rebuild_from_notion",
    "run_startup_recovery",
]

logger = logging.getLogger(__name__)

SAFETY_EXPORT_REASON = "before-rebuild"

_LOGO_REASONS = {
    "too_big": "the file is too large",
    "not_image": "not a PNG or JPEG image",
    "bad_source": "not a web link",
    "download_failed": "the download failed",
}


@dataclass
class RebuildReport:
    result: ImportResult
    logo_failures: list[str] = field(default_factory=list)


async def rebuild_from_notion(ctx: Any, *, post_result: bool = True) -> ActionResult:
    """Replace the database content with Notion's; see the module docstring.

    Refusals (no Notion, maintenance already on, failed safety export) change nothing.
    With ``post_result`` False the summary waits in ``ctx.rebuild_report`` for
    ``post_rebuild_result`` (startup: Discord is not connected yet).
    """
    pool, client = ctx.db, ctx.notion_client
    if pool is None or client is None:
        return ActionResult(False, texts.REBUILD_NO_NOTION)
    if ctx.maintenance:
        return ActionResult(False, texts.REBUILD_BUSY)
    # Claim maintenance before the first await, so a second rebuild or import is refused.
    ctx.maintenance = texts.MAINTENANCE
    try:
        try:
            path = await write_safety_export(pool, SAFETY_EXPORT_REASON)
        except Exception:
            logger.exception("The safety export before the rebuild failed")
            return ActionResult(False, texts.REBUILD_SAFETY_FAILED)
        logger.info("Safety export written to %s", path)
        async with contextlib.AsyncExitStack() as stack:
            if ctx.sync_cycle is not None:
                # A running Notion cycle must not compare its snapshot with the new data.
                await stack.enter_async_context(ctx.sync_cycle.exclusive())
            report = await _import_and_restore_logos(ctx)
    finally:
        ctx.maintenance = None
    ctx.rebuild_report = report
    if post_result:
        await post_rebuild_result(ctx)
    result = report.result
    if not result.ok:
        return ActionResult(False, texts.REBUILD_FAILED)
    return ActionResult(
        True,
        texts.rebuild_done_message(
            result.events, result.slots, result.djs, len(report.logo_failures)
        ),
    )


async def _import_and_restore_logos(ctx: Any) -> RebuildReport:
    pool = ctx.db
    try:
        result = await import_from_notion(pool, ctx.notion_client, mode="replace")
    except Exception as exc:
        logger.exception("The rebuild from Notion failed")
        result = ImportResult(False, 0, 0, 0, [f"The rebuild failed: {exc}"])
    if not result.ok:
        logger.error("The rebuild from Notion failed: %s", "; ".join(result.problems[:10]))
        return RebuildReport(result)
    logger.info(
        "Rebuilt %d events, %d slots and %d DJs from Notion",
        result.events, result.slots, result.djs,
    )
    try:
        await set_value(pool, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY, "1")
    except Exception:
        logger.exception("Recording the finished rebuild failed")
    failures = await _restore_logos(pool, result.logos)
    if ctx.mirror is not None:
        ctx.mirror.notify()  # rows the import queued (new guest ids, missing slot rows)
    return RebuildReport(result, failures)


async def _restore_logos(pool: Any, logos: dict[int, tuple[str, str]]) -> list[str]:
    """Store each DJ's Notion logo; returns one line per logo that could not be stored."""
    if not logos:
        return []
    rows = await pool.fetch(
        "SELECT discord_user_id, dj_name FROM djs WHERE discord_user_id = ANY($1::bigint[])",
        list(logos),
    )
    names = {r["discord_user_id"]: r["dj_name"] for r in rows}
    failures: list[str] = []
    async with httpx.AsyncClient() as http:
        for dj_id, (kind, url) in logos.items():
            label = f"'{names.get(dj_id, dj_id)}'"
            try:
                if kind == "external":
                    if not url.lower().startswith(("https://", "http://")):
                        raise LogoError("bad_source")
                    # A link stays a link; logo_synced: Notion already holds it.
                    await pool.execute(
                        "UPDATE djs SET logo_url = $2, logo_image = NULL, logo_synced = true"
                        " WHERE discord_user_id = $1",
                        dj_id, url,
                    )
                    continue
                image = await download_logo_file(url, client=http)
                await pool.execute(
                    "UPDATE djs SET logo_image = $2, logo_url = NULL, logo_synced = true"
                    " WHERE discord_user_id = $1",
                    dj_id, image,
                )
            except LogoError as exc:
                logger.warning("Logo of DJ %s not restored: %s", dj_id, exc.reason)
                failures.append(f"{label}: {_LOGO_REASONS.get(exc.reason, exc.reason)}")
            except Exception:
                logger.exception("Storing the logo of DJ %s failed", dj_id)
                failures.append(f"{label}: it could not be stored")
    return failures


async def post_rebuild_result(ctx: Any) -> None:
    """Post a waiting rebuild summary to the admin log once. Never raises."""
    report, ctx.rebuild_report = ctx.rebuild_report, None
    log_service = ctx.log_service
    if report is None or log_service is None:
        return
    result = report.result
    try:
        if result.ok:
            await log_service.log_notice(
                texts.LOG_REBUILD_DONE_TITLE,
                texts.log_rebuild_done_text(
                    result.events, result.slots, result.djs,
                    [texts.escape_text(n) for n in result.notes],
                    [texts.escape_text(f) for f in report.logo_failures],
                ),
            )
        else:
            await log_service.log_attention(
                texts.LOG_REBUILD_FAILED_TITLE,
                texts.log_rebuild_failed_text([texts.escape_text(p) for p in result.problems]),
                texts.LOG_REBUILD_FAILED_TODO,
            )
    except Exception:
        logger.warning("Could not post the rebuild result", exc_info=True)


# --- startup cases ---


async def run_startup_recovery(ctx: Any) -> None:
    """The two startup cases of the module docstring. Never raises."""
    pool, client = ctx.db, ctx.notion_client
    if pool is None or client is None or ctx.maintenance:
        return
    try:
        if await get_value(pool, CONFIG_GUILD_ID, PG_TRUTH_IMPORTED_KEY) != "1":
            return  # the upgrade import has not finished
        pg_filled = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM djs) OR EXISTS (SELECT 1 FROM events)"
        )
        if not pg_filled:
            if await client.has_rows("events"):
                logger.warning("The database is empty and Notion holds events: rebuilding")
                await rebuild_from_notion(ctx, post_result=False)
            return
        if await client.has_rows("events") or await client.has_rows("djs"):
            return
        await _fill_empty_notion(ctx)
    except Exception:
        logger.exception("The startup check of Postgres against Notion failed")


async def _fill_empty_notion(ctx: Any) -> None:
    cycle = ctx.sync_cycle
    if cycle is None:  # the mirror is not running: nothing could write Notion
        return
    async with cycle.exclusive():
        # Mirror rows point at pages Notion no longer holds (or at an abandoned set of
        # databases): forget them, so the worker creates new pages.
        await ctx.db.execute("DELETE FROM notion_mirror")
        count = await cycle.resync_all()
    logger.warning("Notion is empty: %d records are queued to fill it from the database", count)
