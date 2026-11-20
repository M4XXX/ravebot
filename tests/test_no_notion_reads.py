"""Event reads come from Postgres: no runtime caller of the Notion list_events."""

import ast
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent / "bot"
# The Notion client and the mirror/poll code
ALLOWED = ("notion/", "sync/")


def test_no_list_events_outside_sync() -> None:
    offenders = []
    for path in sorted(BOT.rglob("*.py")):
        rel = path.relative_to(BOT).as_posix()
        if rel.startswith(ALLOWED):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "list_events"
                and "notion" in ast.unparse(node.func.value).lower()
            ):
                offenders.append(f"bot/{rel}:{node.lineno}")
    assert not offenders, offenders
