"""The cap-tier campaign's plan and report arithmetic (X2, 2026-10-05). Offline; replays nothing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.cap_tier_campaign import (
    COMPARISON_LABELS,
    cap_tier_plan,
    longest_drawdown,
    max_drawdown,
    name_contributions,
    worst_calendar_year,
)
from backtest.sweep import CAP_TIER_ARMS, HIGH_FLOOR, LOW_FLOOR
from backtest.tax import ReissueEvent, RunLedger, TaxTrade
from execution.broker import Side

D = Decimal


def _path(*values: tuple[str, int]) -> list[tuple[date, Decimal]]:
    return [(date.fromisoformat(when), D(v)) for when, v in values]


def test_the_plan_is_the_full_window_and_the_three_fold_tests_at_both_floors() -> None:
    plan = cap_tier_plan(Path("/tmp/x"), data_root=None)
    assert [(w.name, w.start, w.end) for w in plan.windows] == [
        ("full", date(2012, 7, 4), date(2026, 8, 31)),
        ("F1-test", date(2016, 9, 1), date(2019, 8, 30)),
        ("F2-test", date(2019, 9, 2), date(2022, 8, 31)),
        ("F3-test", date(2022, 9, 1), date(2026, 8, 31)),
    ]
    assert [a.label for a in plan.arms] == [
        *(a.label for a in CAP_TIER_ARMS),
        *COMPARISON_LABELS,
    ]
    assert plan.units[0] == (0, LOW_FLOOR) and plan.units[1] == (0, HIGH_FLOOR)
    assert len(plan.units) == 8


def test_max_drawdown_is_peak_to_trough() -> None:
    assert max_drawdown(_path(("2020-01-01", 100), ("2020-01-02", 120), ("2020-01-03", 90))) == D(
        "0.25"
    )


def test_longest_drawdown_counts_peak_to_recovery_not_a_run_of_new_highs() -> None:
    rising = _path(("2020-01-01", 1), ("2020-02-01", 2), ("2020-03-01", 3))
    assert longest_drawdown(rising) == (0, True)
    dip = _path(
        ("2020-01-01", 100),
        ("2020-01-11", 90),
        ("2020-01-31", 100),  # 30 days peak to recovery
        ("2020-02-05", 99),
        ("2020-02-10", 101),  # 5 days
    )
    assert longest_drawdown(dip) == (30, True)


def test_an_open_drawdown_counts_to_the_end_and_says_so() -> None:
    path = _path(("2020-01-01", 100), ("2020-01-02", 101), ("2020-12-31", 50))
    assert longest_drawdown(path) == (364, False)


def test_worst_calendar_year_chains_from_the_previous_year_end() -> None:
    path = _path(
        ("2018-06-01", 100),
        ("2018-12-31", 110),  # 2018: +10 %
        ("2019-12-31", 88),  # 2019: -20 %
        ("2020-12-31", 132),  # 2020: +50 %
    )
    assert worst_calendar_year(path) == (2019, D("-0.2"))


def _trade(isin: str, side: Side, qty: int, net: str, when: str) -> TaxTrade:
    return TaxTrade(
        isin=isin,
        trade_date=date.fromisoformat(when),
        side=side,
        quantity=qty,
        net_amount=D(net),
        stt=D(0),
        stt_known=True,
    )


def test_contributions_fold_a_reissue_into_one_name_and_mark_the_end() -> None:
    """Bought as OLD, carried to NEW by a reissue, held at the end: one name's profit."""
    ledger = RunLedger(
        source="t",
        trades=(
            _trade("INE000000OLD", Side.BUY, 10, "1000", "2020-01-02"),
            _trade("INE000000BBB", Side.BUY, 5, "500", "2020-01-02"),
            _trade("INE000000BBB", Side.SELL, 5, "400", "2020-06-01"),
        ),
        external_flows=(),
        terminal_date=date(2020, 12, 31),
        terminal_nav=D(1900),
        terminal_prices={"INE000000NEW": D(150)},
        corporate_events=(
            ReissueEvent(isin="INE000000NEW", ex_date=date(2020, 3, 2), from_isin="INE000000OLD"),
        ),
    )
    assert name_contributions(ledger) == {"INE000000NEW": D(500), "INE000000BBB": D(-100)}
