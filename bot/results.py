"""Outcome of an admin action (admin page, sync decisions): ok flag and the message to show."""
from dataclasses import dataclass

__all__ = ["ActionResult"]


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
