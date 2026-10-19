"""Holding-period study: cell boundaries, outcome arithmetic, minimum horizon, timing tests.

Offline: synthetic calendars and ledgers only. Each rule that decides a headline has a test that
fails if the rule is inverted (a hurdle read backwards, a cell that leaks into the next month, a
minimum horizon that accepts a later relapse, a truncation that keeps a later fill).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.holding_period import (
    DENSE_HORIZONS,
    HORIZONS,
    HURDLES,
    NAMED_INTERVALS,
    SPAN_END,
    SPAN_START,
    Cell,
    Outcome,
    _dense_consistent,
    grid_starts,
    min_horizon_table,
    shares_by_horizon,
    truncate_ledger,
)
from backtest.holding_period_charts import horizon_box_svg, probability_svg
from backtest.holding_period_stats import (
    anchor_sessions,
    chain_growth,
    growth,
    horizon_end,
    independent_windows,
    longest_under_water,
    max_drawdown,
    min_horizon,
    month_add,
    parked_share,
    permutation_spread_pvalue,
    share_meeting,
    sip_xirr,
    wilson_interval,
)
from backtest.tax import DividendCredit, InterestIncome, RunLedger, SplitEvent, TaxTrade
from backtest.xirr import Cashflow
from execution.broker import Side


def _weekdays(start: date, end: date) -> list[date]:
    out = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS = _weekdays(date(2012, 6, 1), date(2026, 10, 30))


# ── calendar and cells ───────────────────────────────────────────────────────────────────────


def test_month_add_wraps_years_both_ways() -> None:
    assert month_add(2020, 11, 3) == (2021, 2)
    assert month_add(2020, 1, -1) == (2019, 12)
    assert month_add(2020, 12, 12) == (2021, 12)


def test_anchor_sessions_first_on_or_after_the_day() -> None:
    anchors = anchor_sessions(SESSIONS, 15)
    # 2020-03-15 is a Sunday: the anchor is Monday the 16th, never the Friday before.
    assert anchors[(2020, 3)] == date(2020, 3, 16)
    assert anchor_sessions(SESSIONS, 1)[(2020, 2)] == date(2020, 2, 3)
    with pytest.raises(ValueError):
        anchor_sessions(SESSIONS, 31)


def test_horizon_end_is_the_session_before_the_next_anchor() -> None:
    anchors = anchor_sessions(SESSIONS, 1)
    start = anchors[(2020, 3)]
    end = horizon_end(SESSIONS, anchors, start, 12, SPAN_END)
    assert end == date(2021, 2, 26)  # 2021-03-01 is the next start; the cell stops before it
    assert end < anchors[(2021, 3)]


def test_horizon_end_refuses_a_cell_ending_in_an_incomplete_month() -> None:
    anchors = anchor_sessions(SESSIONS, 1)
    start = anchors[(2026, 9)]
    # One month from Sept-2026 ends 2026-09-30 (Oct's anchor 10-01 is inside the span) ...
    assert horizon_end(SESSIONS, anchors, start, 1, SPAN_END) == date(2026, 9, 30)
    # ... but two months would end inside October, which the span does not complete.
    assert horizon_end(SESSIONS, anchors, start, 2, SPAN_END) is None


def test_grid_starts_open_on_the_first_full_month_and_stop_with_the_span() -> None:
    starts = grid_starts(SESSIONS, 1)
    assert starts[0] == date(2012, 8, 1)
    assert starts[-1] == date(2026, 9, 1)
    assert all(s <= SPAN_END for s in starts)
    mid = grid_starts(SESSIONS, 15)
    assert mid[0] == date(2012, 8, 15)
    assert mid[-1] == date(2026, 9, 15)


def test_named_intervals_are_stated_inside_the_span() -> None:
    keys = [n.key for n in NAMED_INTERVALS]
    assert len(keys) == len(set(keys)) >= 24
    for n in NAMED_INTERVALS:
        assert SPAN_START <= n.start < n.end <= SPAN_END, n.key


# ── outcome arithmetic ───────────────────────────────────────────────────────────────────────


def test_max_drawdown_and_under_water() -> None:
    values = [Decimal(v) for v in ("100", "120", "90", "110", "125", "100")]
    assert max_drawdown(values) == Decimal("0.25")
    days = [date(2020, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    points = list(zip(days, values, strict=True))
    # Under water from the 120 peak (day 10) until 125 regains it (day 40) = 30 days; the final
    # spell (day 40 -> 50, unrecovered) is 10 days.
    assert longest_under_water(points) == 30
    rising = [(d, Decimal(i + 1)) for i, d in enumerate(days)]
    assert longest_under_water(rising) == 0


def test_under_water_counts_an_unrecovered_spell_to_the_end() -> None:
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(4)]
    points = list(zip(days, [Decimal(x) for x in ("100", "90", "95", "99")], strict=True))
    assert longest_under_water(points) == 3


def test_parked_share_follows_the_latest_rebalance_reading() -> None:
    sessions = [date(2020, 1, d) for d in range(1, 11)]
    rebalances = [date(2020, 1, 1), date(2020, 1, 6)]
    on = {date(2020, 1, 1): False, date(2020, 1, 6): True}
    assert parked_share(sessions, rebalances, on) == Decimal("0.5")
    flipped = {k: not v for k, v in on.items()}
    assert parked_share(sessions, rebalances, flipped) == Decimal("0.5")
    all_on = dict.fromkeys(rebalances, True)
    assert parked_share(sessions, rebalances, all_on) == Decimal("0")


def test_growth_round_trips_an_annual_rate() -> None:
    start, end = date(2020, 1, 1), date(2021, 12, 31)  # 730 days
    factor = growth(Decimal("0.10"), start, end)
    assert abs(factor - Decimal("1.21")) < Decimal("0.0001")
    assert chain_growth([(Decimal("0.10"), start, end), (Decimal("0"), end, end)]) == factor
    with pytest.raises(ValueError):
        growth(Decimal("-1"), start, end)


def test_sip_on_a_flat_path_earns_nothing_and_on_a_rising_one_less_than_lump() -> None:
    dates = [date(2020, m, 1) for m in range(1, 13)]
    flat = dict.fromkeys(dates, Decimal("100"))
    instalments = [(d, Decimal("1000")) for d in dates]
    rate = sip_xirr(instalments, flat, (date(2020, 12, 31), Decimal("100")))
    assert rate is not None and abs(rate) < Decimal("0.0001")
    rising = {d: Decimal(100 + 2 * i) for i, d in enumerate(dates)}
    sip = sip_xirr(instalments, rising, (date(2020, 12, 31), Decimal("124")))
    assert sip is not None and sip > Decimal("0")


# ── shares, minimum horizon, intervals ───────────────────────────────────────────────────────


def test_share_meeting_counts_value_at_or_above_the_hurdle_and_skips_unscored() -> None:
    pairs = [
        (Decimal("0.12"), Decimal("0.12")),
        (Decimal("0.10"), Decimal("0.12")),
        (None, Decimal("0.12")),
        (Decimal("0.20"), None),
    ]
    assert share_meeting(pairs) == (1, 2)
    # Inverted reading (hurdle >= value) would count the 0.10 window too.
    assert share_meeting([(Decimal("0.10"), Decimal("0.12"))]) == (0, 1)


def test_min_horizon_requires_every_longer_horizon_to_hold() -> None:
    shares = {1: (5, 10), 3: (9, 10), 6: (8, 10), 12: (10, 10), 24: (5, 5)}
    # 3 m clears 90 % but 6 m falls back: the minimum is 12 m, not 3 m.
    assert min_horizon(shares, Decimal("0.9")) == 12
    assert min_horizon(shares, Decimal("0.75")) == 3
    assert min_horizon(shares, Decimal("1")) == 12
    assert min_horizon({1: (1, 2), 3: (1, 2)}, Decimal("0.9")) is None
    assert min_horizon({1: (0, 0), 3: (2, 2)}, Decimal("1")) == 3


def test_independent_windows_and_wilson_width() -> None:
    assert independent_windows(170, 12) == 14
    assert independent_windows(170, 84) == 2
    lo, hi = wilson_interval(10, 10, 2)
    assert hi == 1.0 and lo < 0.6  # two independent windows prove little
    lo14, _ = wilson_interval(10, 10, 14)
    assert lo14 > lo
    assert wilson_interval(0, 0, 5) == (0.0, 1.0)


def test_permutation_pvalue_separates_signal_from_noise() -> None:
    flat = {m: [0.1, 0.2, 0.15, 0.12] for m in range(12)}
    spread, p = permutation_spread_pvalue(flat, iterations=200)
    assert spread == 0.0 and p == 1.0
    split = {0: [1.0 + 0.01 * i for i in range(10)], 1: [0.01 * i for i in range(10)]}
    _, p_split = permutation_spread_pvalue(split, iterations=500)
    assert p_split < 0.05


def test_dense_consistency_rule() -> None:
    assert _dense_consistent(12, 12)
    assert _dense_consistent(12, 10)  # between the coarse 9 m and 12 m
    assert not _dense_consistent(12, 9)  # the coarse grid would have found 9 m
    assert not _dense_consistent(12, 15)
    assert _dense_consistent(None, None)
    assert not _dense_consistent(None, 60)
    assert set(HORIZONS) <= set(DENSE_HORIZONS)


# ── cells against hurdles ────────────────────────────────────────────────────────────────────


def _outcome(start: date, walk: str, tri: str = "0.10", cash: str = "0.04") -> Outcome:
    return Outcome(
        start=start,
        end=start + timedelta(days=365),
        pre_tax_xirr=Decimal(walk) + Decimal("0.02"),
        walkaway_xirr=Decimal(walk),
        realised_xirr=Decimal(walk),
        tax_error=None,
        max_drawdown=Decimal("0.1"),
        under_water_days=30,
        parked=Decimal("0"),
        trades=10,
        exit_charges=Decimal("100"),
        tri_pre_tax=Decimal(tri) + Decimal("0.01"),
        tri_walkaway=Decimal(tri),
        cash_pre_tax=Decimal(cash) + Decimal("0.02"),
        cash_after_tax=Decimal(cash),
    )


def test_hurdles_read_the_walkaway_figure_against_the_same_window() -> None:
    start = date(2020, 1, 1)
    cells = [
        Cell("month-start", 12, _outcome(start, "0.15", tri="0.20"), Decimal("0.30")),
        Cell("month-start", 12, _outcome(start, "0.25", tri="0.20"), Decimal("0.01")),
    ]
    expected = Decimal("0.18")
    by_name = {h.key: h for h in HURDLES}
    assert shares_by_horizon(cells, by_name["tri"], expected, [12]) == {12: (1, 2)}
    assert shares_by_horizon(cells, by_name["expected"], expected, [12]) == {12: (1, 2)}
    assert shares_by_horizon(cells, by_name["0.12"], expected, [12]) == {12: (2, 2)}
    assert shares_by_horizon(cells, by_name["cash"], expected, [12]) == {12: (2, 2)}
    # The continuous-run slice is scored on its own figure, not the exact one.
    approx = shares_by_horizon(cells, by_name["0.12"], expected, [12], approx=True)
    assert approx == {12: (1, 2)}


def test_min_horizon_table_flags_disagreeing_grids_as_unstable() -> None:
    starts = [date(2015, 1, 1) + timedelta(days=31 * i) for i in range(20)]
    good = [
        Cell("month-start", h, _outcome(s, "0.30"), Decimal("0.30"))
        for s in starts
        for h in HORIZONS
    ]
    mid_bad = [Cell("mid-month", h, _outcome(s, "0.01"), None) for s in starts for h in HORIZONS]
    _, stable = min_horizon_table(
        {"month-start": good, "mid-month": good}, Decimal("0.2"), Decimal("0.2")
    )
    assert stable["12 % a year"]
    _, unstable = min_horizon_table(
        {"month-start": good, "mid-month": mid_bad}, Decimal("0.2"), Decimal("0.2")
    )
    assert not unstable["12 % a year"]


# ── ledger truncation ────────────────────────────────────────────────────────────────────────


def test_truncate_ledger_keeps_only_what_happened_by_the_end() -> None:
    isin = "INE002A01018"

    def trade(day: int, side: Side) -> TaxTrade:
        return TaxTrade(isin, date(2020, 1, day), side, 10, Decimal("1000"), Decimal("1"), True)

    ledger = RunLedger(
        source="synthetic",
        trades=(trade(2, Side.BUY), trade(10, Side.SELL), trade(20, Side.BUY)),
        external_flows=(Cashflow(date(2020, 1, 1), Decimal("-100000")),),
        terminal_date=date(2020, 1, 31),
        terminal_nav=Decimal("101000"),
        terminal_prices={isin: Decimal("100")},
        dividends=(
            DividendCredit(isin, date(2020, 1, 5), Decimal("5")),
            DividendCredit(isin, date(2020, 1, 25), Decimal("5")),
        ),
        corporate_events=(SplitEvent(isin, date(2020, 1, 15), 2, 1),),
        interest=(
            InterestIncome(date(2020, 1, 10), Decimal("3")),
            InterestIncome(date(2020, 1, 11), Decimal("3")),
        ),
    )
    cut = truncate_ledger(ledger, date(2020, 1, 10))
    assert [t.trade_date.day for t in cut.trades] == [2, 10]  # the fill on the end day stays
    assert len(cut.dividends) == 1 and cut.corporate_events == ()
    assert [i.received.day for i in cut.interest] == [10]
    assert cut.terminal_date == date(2020, 1, 10)


# ── charts ───────────────────────────────────────────────────────────────────────────────────


def test_charts_are_well_formed_svg() -> None:
    box = horizon_box_svg(
        {1: [0.1, -0.2, 0.3], 12: [0.2, 0.15, 0.25]}, {12: 0.12}, 0.2, title="D13"
    )
    assert box.startswith("<svg") and box.rstrip().endswith("</svg>")
    prob = probability_svg({"a": [(1, 0.5), (12, 0.9)], "b": [(1, 0.2), (12, 1.0)]}, title="D13")
    assert prob.count("<path") >= 2
