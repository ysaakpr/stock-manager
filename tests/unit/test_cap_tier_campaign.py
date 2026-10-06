"""The cap-tier campaign's plan and report arithmetic (X2, 2026-10-05). Offline; replays nothing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

import backtest.cap_tier_campaign as campaign
from backtest.cap_tier_campaign import (
    COMPARISON_LABELS,
    CapTierCampaignError,
    annualised_growth,
    cap_tier_plan,
    longest_drawdown,
    max_drawdown,
    name_contributions,
    path_figures,
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


def test_annualised_growth_is_exact_decimal_not_a_float_round_trip() -> None:
    # 1.21 over two years is exactly 10 % a year; through a float it is 0.10000000000000009.
    rate = annualised_growth(D("1.21"), 730)
    assert type(rate) is Decimal
    assert rate == D("0.1")
    # Doubling over two years: sqrt(2) - 1, to the helper's own 28 digits.
    with localcontext() as ctx:
        ctx.prec = 28
        root_two = D(2).sqrt() - 1
    assert annualised_growth(D(2), 730) == root_two
    assert annualised_growth(D(2), 730).quantize(D("0.0001")) == D("0.4142")


def test_annualised_growth_ignores_the_callers_decimal_context() -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        rate = annualised_growth(D(2), 730)
    assert rate == annualised_growth(D(2), 730)
    assert len(rate.as_tuple().digits) > 6


def test_annualised_growth_refuses_a_span_with_no_rate() -> None:
    with pytest.raises(CapTierCampaignError):
        annualised_growth(D(2), 0)
    with pytest.raises(CapTierCampaignError):
        annualised_growth(D(-1), 365)


def test_path_cagr_is_decimal_and_never_calls_float(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_float(*_: object) -> float:
        raise AssertionError("a return was computed through float")

    monkeypatch.setattr(campaign, "float", no_float, raising=False)
    figures = path_figures(_path(("2020-01-01", 100), ("2020-06-01", 90), ("2021-12-31", 150)))
    assert type(figures.cagr) is Decimal
    # 1.5 over 730 days: 1.5 ** 0.5 - 1.
    assert figures.cagr.quantize(D("0.0001")) == D("0.2247")


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
