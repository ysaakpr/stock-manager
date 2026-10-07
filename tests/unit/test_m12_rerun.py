"""M12.R: the re-run driver runs the existing arms, the D13 paper config and the owner's windows."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from backtest.campaign import CampaignError
from backtest.m12_rerun import LONG, RERUN_ARMS, WINDOWS, RerunPlan, rerun_arms
from backtest.paper_session import ratified_paper_book
from backtest.sweep import ARMS, D13_PAPER_BASELINE, DURATION_ARMS


def test_every_existing_arm_runs_once() -> None:
    labels = [arm.label for arm in RERUN_ARMS]
    assert len(labels) == len(set(labels))
    assert {a.label for a in (*ARMS, *DURATION_ARMS)} <= set(labels)
    assert rerun_arms() == RERUN_ARMS


def test_d13_baseline_is_the_paper_books_configuration() -> None:
    # The baseline must be what paper trading runs, not the all-on research arm beside it.
    assert D13_PAPER_BASELINE in RERUN_ARMS
    assert D13_PAPER_BASELINE.v2 == ratified_paper_book().parameters
    assert D13_PAPER_BASELINE.v2.vol_target_annual is None
    assert D13_PAPER_BASELINE not in ARMS


def test_windows_are_the_owner_mandate() -> None:
    assert WINDOWS["decade"] == (date(2016, 9, 1), date(2026, 8, 31))
    assert WINDOWS["six-year"] == (date(2019, 7, 1), date(2026, 8, 31))
    assert WINDOWS["wf-selection"] == (date(2016, 9, 1), date(2021, 8, 31))
    assert WINDOWS["wf-verification"] == (date(2021, 9, 1), date(2026, 8, 31))
    # Selection closes before verification opens — the choice cannot see the answer.
    assert WINDOWS["wf-selection"][1] < WINDOWS["wf-verification"][0]


def test_long_window_is_opt_in() -> None:
    assert LONG not in RerunPlan(out_dir=Path("/x"), data_root=None).units


def test_plan_refuses_unknown_units() -> None:
    with pytest.raises(CampaignError):
        RerunPlan(out_dir=Path("/x"), data_root=None, units=("nonsense",))


def test_universe_is_the_floor_only_screen_the_old_reports_ran() -> None:
    # NIFTY 500 PIT membership opens 2016-10-24, after the decade and selection windows open.
    assert RerunPlan(out_dir=Path("/x"), data_root=None).universe == "turnover_floor"
