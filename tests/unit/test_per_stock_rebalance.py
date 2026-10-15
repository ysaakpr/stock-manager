"""The per-stock rebalance study driver: its plan, its baseline, and what it accepts as trials."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.cash_interest import load_repo_rate_schedule
from backtest.folds import FOLDS_PATH, load_folds
from backtest.per_stock_rebalance import (
    BASELINE,
    DECIDING_FLOOR,
    FLOORS,
    StudyError,
    load_supplied_sharpes,
    study_plan,
)
from backtest.policies.momentum_v2 import PAPER_RATIFIED_2026_09_06
from backtest.sweep import HIGH_FLOOR, LOW_FLOOR, PER_STOCK_REBALANCE_ARMS


def test_the_baseline_is_d13_and_the_arms_are_d13_plus_one_switch() -> None:
    assert BASELINE.v2 == PAPER_RATIFIED_2026_09_06
    band, trim = PER_STOCK_REBALANCE_ARMS
    assert band.v2 is not None and band.v2.band_exit_daily and band.v2.drift_trim_band is None
    assert trim.v2 is not None and not trim.v2.band_exit_daily
    assert trim.v2.drift_trim_band == Decimal("0.25")
    assert {band.reference, trim.reference} == {BASELINE.label}


def test_the_rule_decides_at_10_crore_and_1_crore_is_run_beside_it() -> None:
    assert DECIDING_FLOOR == HIGH_FLOOR == Decimal("100000000")
    assert FLOORS == (HIGH_FLOOR, LOW_FLOOR)


def test_the_plan_is_the_three_fold_test_windows_and_their_continuous_union(
    tmp_path: Path,
) -> None:
    folds = load_folds()
    plan = study_plan(tmp_path, folds, (BASELINE,), data_root=None, signal_panel=None)
    assert [(w.fold, w.role) for w in plan.windows] == [
        ("F1", "test"),
        ("F2", "test"),
        ("F3", "test"),
        ("F1-F3", "continuous"),
    ]
    assert [w.window for w in plan.windows[:3]] == [f.test for f in folds.folds]
    assert plan.windows[3].window.start == folds.folds[0].test.start
    assert plan.windows[3].window.end == folds.folds[-1].test.end
    # Every window at both floors, the longest (continuous) first.
    assert len(plan.units) == 8
    assert plan.units[0][0] == 3 and plan.units[1][0] == 3


def _trial_file(tmp_path: Path, **overrides: object) -> Path:
    document = {
        "commit": "a" * 40,
        "lake_last_session": "2026-10-01",
        "folds_sha256": hashlib.sha256(FOLDS_PATH.read_bytes()).hexdigest(),
        "floor": str(HIGH_FLOOR),
        "universe": "turnover_floor",
        "book_corporate_actions": True,
        "cash_interest": load_repo_rate_schedule().identity(),
        "trials": [{"arm": "x", "sharpe": 0.05}, {"arm": "y", "sharpe": 0.07}],
        **overrides,
    }
    path = tmp_path / "trial-sharpes.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_supplied_trial_sharpes_are_read_with_their_provenance(tmp_path: Path) -> None:
    sharpes, source = load_supplied_sharpes(_trial_file(tmp_path))
    assert [(t.label, t.sharpe) for t in sharpes] == [("x", 0.05), ("y", 0.07)]
    assert "aaaaaaaaaaaa" in source and "2026-10-01" in source


@pytest.mark.parametrize(
    "override",
    [
        {"floor": str(LOW_FLOOR)},
        {"universe": "nifty500"},
        {"folds_sha256": "0" * 64},
        {"book_corporate_actions": False},
        {"cash_interest": "none"},
    ],
)
def test_supplied_trial_sharpes_from_other_conditions_are_refused(
    tmp_path: Path, override: dict[str, object]
) -> None:
    with pytest.raises(StudyError, match="different conditions"):
        load_supplied_sharpes(_trial_file(tmp_path, **override))
