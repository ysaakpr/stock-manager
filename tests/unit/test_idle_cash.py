"""Idle cash by cause, buys at the per-order ceiling and the empty-tier flag (X2). Offline."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.cap_tier_campaign import decision_sessions
from backtest.idle_cash import (
    EMPTY_TIER_DECISIONS,
    IdleCashError,
    ceiling_buys,
    idle_cash,
    longest_buy_free_span,
    xirr_from_first_buy,
)
from backtest.rails import ratified_backtest_rail_policy
from backtest.sweep import CAP_TIER_ARMS
from backtest.tax import DividendCredit, RunLedger, TaxTrade
from backtest.xirr import Cashflow
from execution.broker import Side

D = Decimal
RAILS = ratified_backtest_rail_policy().rails  # ₹1.2 L or 15 % of the case, whichever is less
DAY = [date(2024, 1, 1) + timedelta(days=i) for i in range(8)]


def _trade(when: date, side: Side, quantity: int, net: str, isin: str = "INE000A01011") -> TaxTrade:
    return TaxTrade(
        isin=isin,
        trade_date=when,
        side=side,
        quantity=quantity,
        net_amount=D(net),
        stt=D(0),
        stt_known=True,
    )


def _ledger(
    trades: list[TaxTrade], *, deposit: str = "1000000", end_nav: str = "1000000", **kw: object
) -> RunLedger:
    return RunLedger(
        source="test",
        trades=tuple(trades),
        external_flows=(Cashflow(DAY[0], -D(deposit)),),
        terminal_date=DAY[-1],
        terminal_nav=D(end_nav),
        terminal_prices={},
        **kw,  # type: ignore[arg-type]
    )


def _flat(value: str, days: int = 5) -> list[tuple[date, Decimal]]:
    return [(DAY[i], D(value)) for i in range(days)]


# ── waiting proceeds ────────────────────────────────────────────────────────────────────────────


def test_proceeds_that_wait_one_cycle_are_waiting_cash_exactly() -> None:
    # Buy on day 1, a stop-out on day 2, idle on day 3, the next buy on day 4.
    ledger = _ledger(
        [
            _trade(DAY[1], Side.BUY, 500, "50000"),
            _trade(DAY[2], Side.SELL, 500, "50000"),
            _trade(DAY[4], Side.BUY, 400, "40000", isin="INE000B01011"),
        ]
    )
    idle = idle_cash(ledger, _flat("1000000"), rails=RAILS)
    # cash / NAV by day: 1, 0.95, 1, 1, 0.96; waiting: 0, 0, 0.05, 0.05, 0.
    assert idle.mean_cash_share == D("4.91") / 5
    assert idle.mean_waiting_share == D("0.10") / 5
    assert idle.mean_ceiling_share == 0
    assert idle.mean_other_share == D("4.81") / 5
    assert idle.waiting_share_of_cash == (D("0.10") / 5) / (D("4.91") / 5)
    assert idle.buys == 2 and idle.ceiling_buys == 0


def test_a_buy_sessions_own_sale_proceeds_wait_for_the_next_buy() -> None:
    # Day 3 rotates: sells the day-1 name and buys another. The buy was sized from free cash, so
    # the sale's 30,000 is waiting until day 4's buy, not leftover.
    ledger = _ledger(
        [
            _trade(DAY[1], Side.BUY, 300, "30000"),
            _trade(DAY[3], Side.SELL, 300, "30000"),
            _trade(DAY[3], Side.BUY, 200, "20000", isin="INE000B01011"),
            _trade(DAY[4], Side.BUY, 300, "30000", isin="INE000C01011"),
        ]
    )
    idle = idle_cash(ledger, _flat("1000000"), rails=RAILS)
    # cash: 1.00, 0.97, 0.97, 0.98, 0.95; waiting on day 3 only: 0.03.
    assert idle.mean_waiting_share == D("0.03") / 5
    assert idle.mean_cash_share == D("4.87") / 5


def test_waiting_never_exceeds_the_cash_on_hand_and_dividends_are_not_proceeds() -> None:
    ledger = _ledger(
        [
            _trade(DAY[1], Side.BUY, 100, "990000"),
            _trade(DAY[2], Side.SELL, 10, "5000"),
        ],
        dividends=(DividendCredit("INE000A01011", DAY[3], D("1000")),),
    )
    idle = idle_cash(ledger, _flat("1000000"), rails=RAILS)
    # cash: 1.000, 0.010, 0.015, 0.016, 0.016; waiting from day 2: 0.005 each day.
    assert idle.mean_waiting_share == D("0.015") / 5
    assert idle.mean_cash_share == D("1.057") / 5


def test_a_ledger_that_is_not_the_navs_run_is_refused() -> None:
    with pytest.raises(IdleCashError, match="negative"):
        idle_cash(_ledger([_trade(DAY[1], Side.BUY, 10, "2000000")]), _flat("1000000"), rails=RAILS)


# ── buys at the ceiling ─────────────────────────────────────────────────────────────────────────


def test_ceiling_buys_are_those_one_share_short_of_the_ceiling() -> None:
    nav = [(DAY[0], D("1000000")), (DAY[1], D("1000000")), (DAY[2], D("500000"))]
    nav.append((DAY[3], D("1000000")))  # the fill day's own NAV must not set its ceiling
    trades = [
        _trade(DAY[1], Side.BUY, 100, "119950"),  # ₹1.2 L cap: one more share would breach it
        _trade(DAY[1], Side.BUY, 100, "100000", isin="INE000B01011"),  # well inside: not bound
        # Day 3 is sized off day 2's NAV of ₹5 L: the 15 % ceiling is ₹75,000, not ₹1.2 L.
        _trade(DAY[3], Side.BUY, 50, "74990", isin="INE000C01011"),
        _trade(DAY[3], Side.SELL, 100, "120000"),  # sells are never ceiling-bound buys
    ]
    assert ceiling_buys(_ledger(trades), nav, rails=RAILS) == frozenset({0, 2})


def test_leftover_after_a_ceiling_buy_is_ceiling_bound() -> None:
    ledger = _ledger(
        [
            _trade(DAY[1], Side.BUY, 100, "119950"),
            _trade(DAY[3], Side.BUY, 100, "50000", isin="INE000B01011"),
        ]
    )
    idle = idle_cash(ledger, _flat("1000000"), rails=RAILS)
    assert idle.ceiling_buys == 1
    # Days 1-2 follow the ceiling buy (cash 0.88005 each); day 3's buy was not bound.
    assert idle.mean_ceiling_share == D("1.76010") / 5
    assert idle.mean_other_share == D("1") / 5 + D("0.83005") * 2 / 5


# ── empty tiers ─────────────────────────────────────────────────────────────────────────────────

DECISIONS = [date(2020, 1, 1) + timedelta(days=14 * k) for k in range(EMPTY_TIER_DECISIONS + 10)]


def _first_buy_after(decision: int) -> list[date]:
    return [DECISIONS[decision] + timedelta(days=1), DECISIONS[-1] + timedelta(days=1)]


def test_the_empty_tier_flag_does_not_fire_at_n() -> None:
    span = longest_buy_free_span(DECISIONS, _first_buy_after(EMPTY_TIER_DECISIONS))
    assert span is not None
    assert span.decisions == EMPTY_TIER_DECISIONS
    assert not span.flagged


def test_the_empty_tier_flag_fires_at_n_plus_one_and_reports_the_span() -> None:
    span = longest_buy_free_span(DECISIONS, _first_buy_after(EMPTY_TIER_DECISIONS + 1))
    assert span is not None
    assert span.decisions == EMPTY_TIER_DECISIONS + 1
    assert span.flagged
    assert span.start == DECISIONS[0]
    assert span.next_buy == DECISIONS[EMPTY_TIER_DECISIONS + 1] + timedelta(days=1)


def test_buying_every_decision_has_no_span_and_never_buying_runs_to_the_end() -> None:
    every = [d + timedelta(days=1) for d in DECISIONS]
    assert longest_buy_free_span(DECISIONS, every) is None
    never = longest_buy_free_span(DECISIONS, [])
    assert never is not None and never.decisions == len(DECISIONS) and never.next_buy is None


def test_swing_decisions_follow_the_runners_cadence() -> None:
    arm = CAP_TIER_ARMS[0]
    assert arm.swing is not None
    sessions = [date(2024, 1, 1) + timedelta(days=i) for i in range(25)]
    step = arm.swing.rebalance_interval_sessions
    assert decision_sessions(arm, sessions) == tuple(sessions[::step])


# ── XIRR from the first buy ─────────────────────────────────────────────────────────────────────


def test_xirr_from_first_buy_starts_at_the_decision_before_it() -> None:
    start, first, end = date(2020, 1, 1), date(2021, 1, 1), date(2022, 1, 1)
    nav = [(start, D("1000000")), (first, D("1000000")), (end, D("2000000"))]
    ledger = RunLedger(
        source="test",
        trades=(_trade(first + timedelta(days=1), Side.BUY, 10, "500000"),),
        external_flows=(Cashflow(start, D("-1000000")),),
        terminal_date=end,
        terminal_nav=D("2000000"),
        terminal_prices={},
    )
    measured = xirr_from_first_buy(ledger, nav)
    assert measured is not None
    assert measured[0] == first
    assert measured[1] == D("1.00000000")  # doubled over 365 days
