"""Integration-suite configuration: keep real-model (`live`) tests opt-in.

M6.8's live fire drill calls a real model — `ClaudeCliLLM`, `claude -p` on this machine's
subscription (HUMAN_DECISIONS D16). That consumes rate limit and is not deterministic, so it must
never ride along with a bare ``uv run pytest`` or ``make check``. This hook **deselects every
`live`-marked test unless the marker expression positively selects it** (``-m live``). Deselected
rather than skipped, so a bare run does not even report them, and ``--collect-only`` shows exactly
what would run — which is what `test_fire_drill.py` pins.

It also refuses, at collection, any test that reaches the real model through the `live_llm`
fixture (directly or via another fixture) without carrying the `live` mark: such a test would
escape the deselection above and spend the budget in `make check`.

**Scope: `tests/integration/` only.** A conftest applies to the directory it sits in, so this gate
does not cover `tests/unit/`, `tests/golden/` or anything else. A live test placed outside
`tests/integration/` would run unguarded, which is why the real-model fixture lives in
`test_fire_drill.py` and nowhere else.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

#: The fixture that hands a test the real (budgeted) model. Using it is what makes a test live.
LIVE_FIXTURE = "live_llm"


def live_selected(markexpr: str) -> bool:
    """True only when the `-m` expression positively names `live` (``live``, ``live and x``).

    A bare run (``""``), ``not live``, or an expression that does not mention `live` at all leaves
    the live tests out. Any ``not`` in an expression that names `live` is read as exclusion — a
    false negative costs an operator one re-run; a false positive spends the model budget.
    """
    tokens = markexpr.replace("(", " ").replace(")", " ").split()
    return "live" in tokens and "not" not in tokens


def unmarked_live_fixture_users(items: Sequence[pytest.Item]) -> list[str]:
    """Node ids of tests whose fixture closure includes `live_llm` but which lack the `live` mark.

    `fixturenames` is the full closure, so a test that reaches the model through an intermediate
    fixture is caught as well as one that names `live_llm` directly.
    """
    return [
        item.nodeid
        for item in items
        if LIVE_FIXTURE in getattr(item, "fixturenames", ())
        and item.get_closest_marker("live") is None
    ]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse unmarked real-model tests, then deselect ``live`` tests unless ``-m live``."""
    unmarked = unmarked_live_fixture_users(items)
    if unmarked:
        raise pytest.UsageError(
            f"tests use the real-model fixture {LIVE_FIXTURE!r} without @pytest.mark.live, so "
            f"they would run in a bare pytest / make check: {', '.join(unmarked)}"
        )
    if live_selected(str(config.getoption("markexpr", default="") or "")):
        return
    live = [item for item in items if item.get_closest_marker("live") is not None]
    if not live:
        return
    config.hook.pytest_deselected(items=live)
    items[:] = [item for item in items if item.get_closest_marker("live") is None]
