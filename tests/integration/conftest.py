"""Integration-suite configuration: keep real-model (`live`) tests opt-in.

M6.8's live fire drill calls a real model — `ClaudeCliLLM`, `claude -p` on this machine's
subscription (HUMAN_DECISIONS D16). That consumes rate limit and is not deterministic, so it must
never ride along with a bare ``uv run pytest`` or ``make check``. This hook **deselects every
`live`-marked test unless the marker expression positively selects it** (``-m live``). Deselected
rather than skipped, so a bare run does not even report them, and ``--collect-only`` shows exactly
what would run — which is what `test_fire_drill.py` pins.

Scoped to `tests/integration/` on purpose: it must not change collection for any other suite.
"""

from __future__ import annotations

import pytest


def live_selected(markexpr: str) -> bool:
    """True only when the `-m` expression positively names `live` (``live``, ``live and x``).

    A bare run (``""``), ``not live``, or an expression that does not mention `live` at all leaves
    the live tests out. Any ``not`` in an expression that names `live` is read as exclusion — a
    false negative costs an operator one re-run; a false positive spends the model budget.
    """
    tokens = markexpr.replace("(", " ").replace(")", " ").split()
    return "live" in tokens and "not" not in tokens


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect ``live`` tests unless the run opted in with ``-m live``."""
    if live_selected(str(config.getoption("markexpr", default="") or "")):
        return
    live = [item for item in items if item.get_closest_marker("live") is not None]
    if not live:
        return
    config.hook.pytest_deselected(items=live)
    items[:] = [item for item in items if item.get_closest_marker("live") is None]
