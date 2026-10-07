"""run_walk_forward screens one universe on both halves — the choice and its check never differ."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import backtest.verdict as verdict
from backtest.sweep import SweepResult


def test_both_sweeps_receive_the_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_sweep(**kwargs: Any) -> SweepResult:
        calls.append(kwargs)
        return SweepResult()

    monkeypatch.setattr(verdict, "run_sweep", fake_sweep)
    verdict.run_walk_forward(
        selection=(date(2016, 9, 1), date(2021, 8, 31)),
        verification=(date(2021, 9, 1), date(2026, 8, 31)),
        arms=(),
        universe_name="turnover_floor",
    )
    # Selection first, then verification; dropping the universe from either call fails here.
    assert [c["start"] for c in calls] == [date(2016, 9, 1), date(2021, 9, 1)]
    assert [c.get("universe_name") for c in calls] == ["turnover_floor", "turnover_floor"]
