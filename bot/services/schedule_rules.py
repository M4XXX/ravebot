"""Pure schedule rule logic: which rule value applies to which schedule date."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from math import gcd
from typing import Literal

log = logging.getLogger(__name__)

Field = Literal["genre", "location"]


@dataclass(frozen=True)
class Rule:
    id: int | None
    field: Field
    value: str
    every: int
    anchor: datetime.date


def rule_hits(rule: Rule, day: datetime.date, interval_days: int) -> bool:
    delta = (day - rule.anchor).days
    return (
        delta >= 0
        and delta % interval_days == 0
        and (delta // interval_days) % rule.every == 0
    )


def conflicts(new: Rule, existing: Sequence[Rule], interval_days: int) -> list[Rule]:
    """Existing rules for the same field that can hit the same date as `new`."""
    found: list[Rule] = []
    for other in existing:
        if other.field != new.field:
            continue
        if new.id is not None and other.id == new.id:
            continue
        d = (other.anchor - new.anchor).days
        if d % interval_days != 0:
            continue
        k = d // interval_days
        if k % gcd(new.every, other.every) == 0:
            found.append(other)
    return found


def resolve(
    field: Field,
    day: datetime.date,
    rules: Sequence[Rule],
    default: str | None,
    interval_days: int,
) -> str | None:
    hits = [r for r in rules if r.field == field and rule_hits(r, day, interval_days)]
    if not hits:
        return default
    if len(hits) > 1:
        log.warning("Multiple %s rules hit %s: %s", field, day, [r.id for r in hits])
        hits.sort(key=lambda r: (r.id is None, r.id or 0))
    return hits[0].value


def reanchor(
    anchor: datetime.date, grid_origin: datetime.date, interval_days: int
) -> datetime.date:
    """Smallest grid_origin + n * interval_days that is >= anchor."""
    delta = (anchor - grid_origin).days
    n = -(-delta // interval_days)
    return grid_origin + datetime.timedelta(days=n * interval_days)
