"""Golden-suite test configuration: keep the live-network cross-check opt-in.

Reference A (`test_yfinance_reference.py`) has one test that pulls from Yahoo Finance. It is marked
``network``, and this hook **skips every network-marked test unless it was positively selected**
with ``-m network``. That is what keeps a bare ``uv run pytest`` — and therefore ``make check`` —
fully offline (AGENTIC_CONTEXT B8), while still letting an operator refresh the cached fixtures on
demand. ``-m 'not network'`` (the task's verification command) also skips them, belt and braces.

Scoped to `tests/golden/` on purpose: it must not change collection for any other suite.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``network`` tests unless the marker expression positively selects them (``-m network``).

    Only three marker expressions are used against this suite — ``""`` (bare run), ``"network"``
    (the opt-in), and ``"not network"`` (the verify command). A ``network`` token that is not
    negated means the operator asked for the live pull; anything else leaves it skipped.
    """
    markexpr = str(config.getoption("markexpr", default="") or "")
    tokens = markexpr.replace("(", " ").replace(")", " ").split()
    opted_in = "network" in tokens and "not" not in tokens
    if opted_in:
        return

    skip_network = pytest.mark.skip(reason="live-network test; opt in with `-m network`")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)
