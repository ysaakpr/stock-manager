"""Static enforcement of invariant #13's real rule: `get_secret_value()` never reaches a log call
or an exception constructor, anywhere in product code.

This is the allowlist direction a review of this branch asked for, applied where it is tractable
without a rewrite of the logging pipeline: `dataplatform.logging`'s `_redaction_processor` is a
regex denylist — it has zero coverage for SMTP, Anthropic or Kite credentials (nothing calls
`get_secret_value()` on them yet, so there is nothing to redact today, but nothing would catch it
the day M6.8 or M8.3 adds that call either) and cannot reach into Rich's rendered console output
at all. A regex can only clean up after a secret has already reached the boundary; this scans
every call site that would let one arrive there in the first place, mechanically, on every run —
not just the shapes the audit happened to think to name a pattern for.

Deliberately NOT a runtime guard (a custom logger wrapper, a monkeypatched `get_secret_value`):
this repo's actual entrypoints (`dataplatform.logging.get_logger`, plain `raise`) are the ones
every call site already uses, and a static check catches the mistake at review time rather than
waiting for the one test run that happens to exercise the failing branch. `SecretStr` intact all
the way to the point of use, never `.get_secret_value()` bound to a name that outlives one
expression: this is that rule, enforced rather than merely documented.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dataplatform.config import REPO_ROOT

#: The product-code packages `pyproject.toml`'s `[tool.mypy] files` also scopes to — deliberately
#: not `tests/`, which legitimately calls `get_secret_value()` to construct and assert on fixture
#: secrets, never to log or raise with one.
PRODUCT_PACKAGES = ("dataplatform", "analyst", "execution", "backtest", "accounting")

#: Bound-logger and stdlib-logger method names that emit a record. `warn` is stdlib's deprecated
#: alias for `warning`; included because deprecated is not the same as unused.
_LOG_METHOD_NAMES = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _calls_get_secret_value(node: ast.AST) -> bool:
    """Whether `.get_secret_value()` is called anywhere inside this subtree."""
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "get_secret_value"
        for sub in ast.walk(node)
    )


def _offending_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `Call` in `tree` that is either a log-shaped method call or an exception
    construction, with `get_secret_value()` nested somewhere in its arguments."""
    raised_exception_calls = {
        id(node.exc)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
    }
    offenders: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_log_call = isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHOD_NAMES
        is_raised = id(node) in raised_exception_calls
        if not (is_log_call or is_raised):
            continue
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        if any(_calls_get_secret_value(arg) for arg in arguments):
            offenders.append(node)
    return offenders


def _product_source_files() -> list[Path]:
    return sorted(
        path
        for package in PRODUCT_PACKAGES
        for path in (REPO_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize(
    "path", _product_source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_product_file_lets_get_secret_value_reach_a_log_call_or_a_raise(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders = _offending_calls(tree)

    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)}: get_secret_value() reaches a log call or a raised "
        f"exception at line(s) {sorted(o.lineno for o in offenders)} — keep the SecretStr "
        "wrapped until the single call (psycopg.connect, smtp.login, httpx's URL builder, ...) "
        "that actually needs the plaintext, and never pass the unwrapped value onward from there."
    )
