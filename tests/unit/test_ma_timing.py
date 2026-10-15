"""Research MA timing (backtest.ma_timing): the lag is real, nothing future leaks, a switch
costs what the shared cost model says, and the regime-length arms leave D13 unchanged.

Offline: series are built in memory, and the one lake test writes into ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.cash_interest import load_repo_rate_schedule
from backtest.m12_rerun import ARM_SETS, REGIME_DAILY_SET, REGIME_LENGTH_SET
from backtest.ma_timing import (
    ETF_ISIN,
    ETF_UNIT_DIVISOR,
    SLIPPAGE,
    Cadence,
    CostMode,
    IndexPoint,
    IndexSeries,
    MaTimingError,
    SimResult,
    TimingRule,
    after_tax,
    load_index,
    metrics,
    simulate,
    states,
)
from backtest.policies.momentum_v2 import PAPER_RATIFIED_2026_09_06, MomentumV2Parameters
from backtest.sweep import D13_PAPER_BASELINE, REGIME_LENGTH_ARMS
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, TriPoint, TriSeries, write_tri_l1
from execution.costs import CostModel, Side, Trade, load_rate_card

COST = CostModel(load_rate_card(), account_state="MH")
REPO = load_repo_rate_schedule()


def _weekdays(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _series(levels: list[str], start: date = date(2021, 1, 4)) -> IndexSeries:
    """Levels are quoted as in the tests and stored x100, so an ETF unit is a realistic rupee."""
    sessions = _weekdays(start, len(levels))
    pts = tuple(IndexPoint(s, s, Decimal(v) * 100) for s, v in zip(sessions, levels, strict=True))
    return IndexSeries("test", "Test", pts, {"Test": tuple(sessions)})


#: Flat at 100 for 5 sessions, a jump to 110 on session 5, then flat: a 3-SMA cross on session 5.
CROSS = ["100"] * 5 + ["110"] * 10


def _run(
    series: IndexSeries,
    rule: TimingRule | None,
    costs: CostMode = CostMode.CARD,
    lag: int = 1,
) -> SimResult:
    return simulate(
        series,
        start=series.points[0].session,
        end=series.points[-1].session,
        rule=rule,
        cost_model=COST,
        repo=REPO,
        costs=costs,
        lag=lag,
    )


# ── signal and lag ──────────────────────────────────────────────────────────────────────────────


def test_decision_uses_the_close_it_is_struck_on() -> None:
    s = _series(CROSS)
    decided = states(s.points, TimingRule(3))
    sessions = s.sessions
    assert decided[sessions[1]] == (None, None)  # fewer than 3 points
    assert decided[sessions[4]][0] is True  # 100 >= SMA 100 (at-or-above is invested)
    falling = _series(["100"] * 5 + ["90"] * 5)
    d2 = states(falling.points, TimingRule(3))
    assert d2[falling.sessions[4]][0] is True
    assert d2[falling.sessions[5]][0] is False  # 90 < SMA(100,100,90)


def test_switch_trades_at_the_next_close_not_the_signal_close() -> None:
    s = _series(["100"] * 5 + ["90"] * 3 + ["120"] * 5)
    run = _run(s, TimingRule(3), costs=CostMode.FRICTIONLESS)
    trades = run.trades
    # Invested from the first decision (session 2) -> buy at session 3's close.
    assert trades[0].side is Side.BUY and trades[0].session == s.sessions[3]
    # Risk-off decided at session 5's close -> sold at session 6's close, never session 5's.
    assert trades[1].side is Side.SELL and trades[1].session == s.sessions[6]
    # Session 7's close (90) sits at its SMA (90, 90, 90): at-or-above is invested -> bought at 8.
    assert trades[2].side is Side.BUY and trades[2].session == s.sessions[8]


def test_the_lag_is_real_shifting_the_signal_a_day_changes_the_result() -> None:
    zigzag = ["100", "104", "98", "106", "97", "108", "96", "110", "95", "112"] * 3
    s = _series(zigzag)
    lagged = _run(s, TimingRule(3), costs=CostMode.FRICTIONLESS, lag=1)
    same_close = _run(s, TimingRule(3), costs=CostMode.FRICTIONLESS, lag=0)
    assert lagged.nav != same_close.nav
    assert lagged.trades[0].session > same_close.trades[0].session


def test_no_future_point_changes_a_past_decision_or_nav() -> None:
    base = ["100", "101", "99", "103", "102", "98", "97", "105", "106", "100", "99", "110"]
    s1 = _series(base)
    altered = [*base[:7], "50", "200", "50", "200", "50"]
    s2 = _series(altered)
    rule = TimingRule(3)
    d1, d2 = states(s1.points, rule), states(s2.points, rule)
    for session in s1.sessions[:7]:
        assert d1[session] == d2[session]
    r1 = _run(s1, rule)
    r2 = _run(s2, rule)
    # NAV through session 7 depends only on decisions through session 6 and closes through 7.
    assert r1.nav[:7] == r2.nav[:7]


def test_a_point_not_yet_knowable_is_not_in_the_decision() -> None:
    sessions = _weekdays(date(2021, 1, 4), 6)
    levels = ["100", "100", "100", "100", "100", "50"]
    pts = [IndexPoint(s, s, Decimal(v)) for s, v in zip(sessions, levels, strict=True)]
    # The crash close on the last session becomes knowable only a session later.
    pts[-1] = IndexPoint(sessions[-1], sessions[-1] + timedelta(days=1), Decimal("50"))
    decided = states(pts, TimingRule(3))
    assert decided[sessions[-1]][0] is True


def test_band_holds_the_state_inside_the_band() -> None:
    s = _series(["100"] * 5 + ["99.5"] * 3 + ["97"] * 3)
    banded = states(s.points, TimingRule(3, Decimal("0.01")))
    plain = states(s.points, TimingRule(3))
    assert plain[s.sessions[5]][0] is False  # 99.5 < SMA
    assert banded[s.sessions[5]][0] is True  # inside the 1% band: unchanged
    assert banded[s.sessions[8]][0] is False  # 97 < SMA x 0.99


def test_weekly_cadence_decides_only_on_the_weeks_last_session() -> None:
    s = _series(["100"] * 5 + [str(90 - i) for i in range(10)])  # 2021-01-04 is a Monday
    d = states(s.points, TimingRule(3, cadence=Cadence.WEEKLY))
    # Session 5 (Mon 2021-01-11) is risk-off daily, but the weekly read waits for Friday.
    assert d[s.sessions[5]][0] is True
    assert d[s.sessions[9]][0] is False  # Fri 2021-01-15


# ── costs ───────────────────────────────────────────────────────────────────────────────────────


def test_a_switch_costs_what_the_shared_cost_model_charges() -> None:
    s = _series(["100"] * 5 + ["90"] * 3 + ["120"] * 5)
    run = _run(s, TimingRule(3))
    first = run.trades[0]
    mid = (Decimal("10000") / ETF_UNIT_DIVISOR).quantize(Decimal("0.01"))
    assert first.price == (mid * (1 + SLIPPAGE)).quantize(Decimal("0.01"))
    expected = COST.charge(Trade(ETF_ISIN, first.session, Side.BUY, first.units, first.price))
    assert first.charges == expected.total > 0
    assert first.net_amount == expected.net_amount
    sell = run.trades[1]
    assert sell.price < Decimal("9000") / ETF_UNIT_DIVISOR  # slippage works against the seller
    ex = _run(s, TimingRule(3), costs=CostMode.CARD_EX_STT)
    stt = expected.securities_transaction_tax
    assert ex.trades[0].charges == expected.total - stt
    free = _run(s, TimingRule(3), costs=CostMode.FRICTIONLESS)
    assert all(t.charges == 0 for t in free.trades)
    assert run.nav[-1][1] < ex.nav[-1][1] < free.nav[-1][1]


def test_buy_and_hold_trades_once_and_is_always_invested() -> None:
    s = _series(CROSS)
    run = _run(s, None)
    assert len(run.trades) == 1 and run.switches == 0
    m = metrics(run)
    assert m.invested_share == 1
    assert m.max_drawdown >= 0


def test_short_round_trips_are_taxed_short_term() -> None:
    zigzag = ["100", "104", "98", "106", "97", "108", "96", "110", "95", "112"] * 3
    s = _series(zigzag)
    run = _run(s, TimingRule(3))
    taxed = after_tax(run)
    assert taxed.realisations and all(r.term.value == "STCG" for r in taxed.realisations)
    assert taxed.after_tax_xirr_liquidated is not None


# ── the lake reader ─────────────────────────────────────────────────────────────────────────────


def _write(root: Path, names: list[str]) -> None:
    days = _weekdays(date(2021, 1, 4), len(names))
    points = tuple(
        TriPoint(
            index_slug="nifty50",
            index_name=n,
            as_of=d,
            tri_value=Decimal("100") + i,
            method=TRI_METHOD_PUBLISHED,
        )
        for i, (d, n) in enumerate(zip(days, names, strict=True))
    )
    series = TriSeries(
        index_slug="nifty50", index_name=names[0], method=TRI_METHOD_PUBLISHED, points=points
    )
    write_tri_l1(series, data_root=root)


def test_load_index_normalises_a_case_variant_label(tmp_path: Path) -> None:
    _write(tmp_path, ["Nifty 50", "NIFTY 50", "Nifty 50", "Nifty 50"])
    s = load_index("nifty50", through=date(2021, 12, 31), data_root=tmp_path)
    assert s.name == "Nifty 50" and len(s.points) == 4
    assert set(s.spellings) == {"Nifty 50", "NIFTY 50"}


def test_load_index_refuses_a_label_that_differs_beyond_case(tmp_path: Path) -> None:
    _write(tmp_path, ["Nifty 50", "Nifty Next 50", "Nifty 50"])
    with pytest.raises(MaTimingError, match="beyond case"):
        load_index("nifty50", through=date(2021, 12, 31), data_root=tmp_path)


# ── D13 is unchanged ────────────────────────────────────────────────────────────────────────────

#: D13's run identity as persisted by every campaign since 2026-09-06.
D13_REPR = (
    "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
    "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
    "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
    "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
    "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
)


def test_d13_regime_length_default_is_200_and_its_identity_is_unchanged() -> None:
    assert MomentumV2Parameters().regime_ma_days == 200
    assert PAPER_RATIFIED_2026_09_06.regime_ma_days == 200
    assert repr(PAPER_RATIFIED_2026_09_06) == D13_REPR
    assert D13_PAPER_BASELINE.v2 is PAPER_RATIFIED_2026_09_06


def test_regime_length_arms_change_only_the_regime_length() -> None:
    lengths = []
    for arm in REGIME_LENGTH_ARMS:
        assert arm.v2 is not None
        lengths.append(arm.v2.regime_ma_days)
        assert replace(arm.v2, regime_ma_days=200) == PAPER_RATIFIED_2026_09_06
        assert repr(arm.v2) == D13_REPR.replace(
            "regime_ma_days=200", f"regime_ma_days={lengths[-1]}"
        )
    assert lengths == [30, 50]
    assert REGIME_LENGTH_SET[0] is D13_PAPER_BASELINE
    assert ARM_SETS["regime-length"] == REGIME_LENGTH_SET
    # The M14.5 set is untouched.
    assert REGIME_DAILY_SET[0].label == D13_PAPER_BASELINE.label
    assert all(a.v2 is not None and a.v2.regime_ma_days == 200 for a in REGIME_DAILY_SET)
