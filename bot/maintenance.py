"""One data-replacing operation at a time (Ruling R20).

The upgrade import, the rebuild from Notion and the backup import each replace data
in Postgres. ``try_claim`` never waits: it returns None while another of them runs
(the caller refuses with ``texts.OPERATION_BUSY``), else a context manager that holds
the claim until it exits. Check and set happen without an await in between, so a
plain module flag is enough for the single event loop (and process) the bot runs.
"""
import contextlib
from collections.abc import Iterator

__all__ = ["running", "try_claim"]

_running: str | None = None


def running() -> str | None:
    """Name of the operation that holds the claim, or None."""
    return _running


def try_claim(name: str) -> "contextlib.AbstractContextManager[None] | None":
    """Claim at once for ``name``; None when another operation already runs."""
    global _running
    if _running is not None:
        return None
    _running = name
    return _release()


@contextlib.contextmanager
def _release() -> Iterator[None]:
    global _running
    try:
        yield
    finally:
        _running = None
