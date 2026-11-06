"""Guard: no em dash (U+2014) in user-facing strings under bot/."""
import ast
from pathlib import Path

EM_DASH = "\u2014"
LOG_FUNCS = {"debug", "info", "warning", "error", "exception", "critical"}
BOT_DIR = Path(__file__).resolve().parent.parent / "bot"


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def find_em_dashes(source: str) -> list[int]:
    """Return line numbers of string constants containing an em dash.

    Skips docstrings and anything inside logging calls (debug/info/...).
    """
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call) and _call_name(node) in LOG_FUNCS:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                for sub in ast.walk(arg):
                    skip.add(id(sub))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and EM_DASH in node.value
            and id(node) not in skip
        ):
            hits.append(node.lineno)
    return sorted(hits)


def test_scanner_flags_plain_string():
    assert find_em_dashes('x = "a \u2014 b"\n') == [1]


def test_scanner_flags_fstring_literal_part():
    assert find_em_dashes('x = f"a \u2014 {y}"\n') == [1]


def test_scanner_ignores_docstrings():
    src = (
        '"""mod \u2014 doc"""\n\n'
        'def f():\n    """a \u2014 b"""\n\n'
        'class C:\n    """a \u2014 b"""\n'
    )
    assert find_em_dashes(src) == []


def test_scanner_ignores_log_calls():
    src = 'logger.info("a \u2014 b")\nlog.error("x %s \u2014", y)\n'
    assert find_em_dashes(src) == []


def test_no_em_dash_in_bot_strings():
    offenders = []
    for path in sorted(BOT_DIR.rglob("*.py")):
        for line in find_em_dashes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(BOT_DIR.parent)}:{line}")
    assert not offenders, "Em dash in user-facing strings:\n" + "\n".join(offenders)
