"""Three-way planner: Postgres vs Notion vs the last synced state. Pure, no I/O.

For one entity type it compares the Postgres payloads, the Notion payloads and the
mirror's synced hash and lists what the caller should do. Entities with an open
conflict (``open_keys``) are left alone.

``unmatched_notion`` holds payload dicts of Notion pages with no known key, each
with a ``_page_id`` field. For events a ``_bot_id`` field (the page's "Bot ID") may
be present; it lets the planner adopt a page the worker created but could not record.
"""
from dataclasses import dataclass
from typing import Any, Literal

from bot.db.mirror import MirrorRow
from bot.sync.payloads import payload_hash

__all__ = ["Change", "Side", "plan"]

Kind = Literal[
    "push", "apply", "conflict", "new_in_notion", "deleted_in_notion",
    "key_changed", "mass_change", "agree",
]


@dataclass(frozen=True)
class Side:
    payloads: dict[str, dict]
    page_ids: dict[str, str]


@dataclass(frozen=True)
class Change:
    kind: Kind
    entity: str
    key: str | None
    pg: dict | None
    notion: dict | None
    page_id: str | None


def _natural_key(entity: str, payload: dict[str, Any]) -> Any:
    if entity == "event":
        return payload.get("event_id")
    if entity == "dj":
        return payload.get("discord_id")
    if entity == "slot":
        return (payload.get("event_uid"), payload.get("number"))
    return None


def strip_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def plan(
    entity: str,
    pg: dict[str, dict],
    notion: Side,
    mirror: dict[str, MirrorRow],
    open_keys: set[str],
    *,
    unmatched_notion: list[dict],
) -> list[Change]:
    if (
        pg
        and not notion.payloads
        and not unmatched_notion
        and any(r.notion_page_id for r in mirror.values())
    ):
        return [Change("mass_change", entity, None, None, None, None)]

    changes: list[Change] = []
    deleted = 0

    def _adopts(u: dict) -> bool:
        bot_id = u.get("_bot_id")
        return (
            entity == "event"
            and bot_id in pg
            and bot_id not in open_keys
            and bot_id not in notion.payloads
            and not (mirror.get(bot_id) and mirror[bot_id].notion_page_id)
        )

    adopted = {u["_bot_id"] for u in unmatched_notion if _adopts(u)}

    def add(kind: Kind, key: str | None, p: dict | None, n: dict | None, page: str | None) -> None:
        changes.append(Change(kind, entity, key, p, n, page))

    for key in sorted(set(pg) | set(notion.payloads) | set(mirror)):
        if key in open_keys or key in adopted:
            continue
        p = pg.get(key)
        n = notion.payloads.get(key)
        row = mirror.get(key)
        page_id = notion.page_ids.get(key) or (row.notion_page_id if row else None)
        synced = row.synced_hash if row else None

        if n is None:
            if p is None:
                continue
            if row is not None and row.notion_page_id:
                deleted += 1
                add("deleted_in_notion", key, p, None, row.notion_page_id)
            else:
                add("push", key, p, None, None)
            continue
        if p is None:
            if row is not None and row.notion_page_id:
                add("push", key, None, n, page_id)  # Postgres deletion not mirrored yet
            else:
                add("new_in_notion", key, None, n, page_id)
            continue
        if row is None or synced is None:
            add("push", key, p, n, page_id)
            continue
        if row.synced_payload is not None and _natural_key(
            entity, n
        ) != _natural_key(entity, row.synced_payload):
            add("key_changed", key, p, n, page_id)
            continue
        hp, hn = payload_hash(p), payload_hash(n)
        if hp == synced and hn == synced:
            continue
        if hn == synced:
            add("push", key, p, n, page_id)
        elif hp == synced:
            add("apply", key, p, n, page_id)
        elif hp == hn:
            add("agree", key, p, n, page_id)
        else:
            add("conflict", key, p, n, page_id)

    if deleted > max(3, 0.2 * len(mirror)):
        return [Change("mass_change", entity, None, None, None, None)]

    existing = {_natural_key(entity, v) for v in pg.values()}
    counts: dict[Any, int] = {}
    for u in unmatched_notion:
        nk = _natural_key(entity, u)
        counts[nk] = counts.get(nk, 0) + 1
    for u in unmatched_notion:
        page_id = u.get("_page_id")
        payload = strip_meta(u)
        if _adopts(u):
            bot_id = u["_bot_id"]
            add("agree", bot_id, pg[bot_id], payload, page_id)
            continue
        nk = _natural_key(entity, payload)
        if nk in existing or counts[nk] > 1:
            add("key_changed", None, None, payload, page_id)
        else:
            add("new_in_notion", None, None, payload, page_id)
    return changes
