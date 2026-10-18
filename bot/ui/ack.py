"""Answer Discord in time even when Notion or the database is slow.

Discord drops an interaction that is not acknowledged within 3 seconds; the user then sees
"This interaction failed" although the booking went through. Handlers that may be slow run
inside AutoAck: if they have not answered after ACK_AFTER_SECONDS, the interaction is deferred
("thinking…") and reply() sends the answer as a follow-up. Fast handlers are unaffected.
"""
import asyncio
import logging
from typing import Any

import disnake

logger = logging.getLogger(__name__)

ACK_AFTER_SECONDS = 2.0

# Interactions use __slots__, so the per-interaction lock lives here, keyed by identity.
_locks: dict[int, asyncio.Lock] = {}


class AutoAck:
    """async with AutoAck(inter): defer automatically if the body is still running at 2 s."""

    def __init__(self, inter: disnake.Interaction, ephemeral: bool = False) -> None:
        self._inter = inter
        self._ephemeral = ephemeral
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "AutoAck":
        _locks[id(self._inter)] = asyncio.Lock()
        self._task = asyncio.create_task(self._defer_later())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
        _locks.pop(id(self._inter), None)

    async def _defer_later(self) -> None:
        await asyncio.sleep(ACK_AFTER_SECONDS)
        lock = _locks.get(id(self._inter))
        if lock is None:
            return
        async with lock:
            if self._inter.response.is_done() is not True:
                try:
                    await self._inter.response.defer(with_message=True, ephemeral=self._ephemeral)
                except disnake.HTTPException as exc:
                    logger.warning("Could not defer a slow interaction: %s", exc)


async def reply(inter: disnake.Interaction, *args: Any, **kwargs: Any) -> Any:
    """inter.response.send_message, or a follow-up once the interaction was answered or deferred.

    Also outside AutoAck: an error handler runs after the handler's AutoAck has ended, and
    may find the interaction already deferred.
    """
    lock = _locks.get(id(inter))
    if lock is None:
        if inter.response.is_done() is True:
            return await inter.followup.send(*args, **kwargs)
        return await inter.response.send_message(*args, **kwargs)
    async with lock:
        if inter.response.is_done() is True:
            return await inter.followup.send(*args, **kwargs)
        return await inter.response.send_message(*args, **kwargs)


def auto_ack(ephemeral: bool = False) -> Any:
    """Decorator for slow interaction handlers: runs the body inside AutoAck.

    The interaction is found among the arguments; without one (unit tests calling the
    handler with mocks) the handler runs unchanged.
    """
    import functools

    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        async def handler(*args: Any, **kwargs: Any) -> Any:
            inter = next(
                (a for a in (*args, *kwargs.values()) if isinstance(a, disnake.Interaction)), None
            )
            if inter is None:
                return await fn(*args, **kwargs)
            async with AutoAck(inter, ephemeral=ephemeral):
                return await fn(*args, **kwargs)

        return handler

    return wrap
