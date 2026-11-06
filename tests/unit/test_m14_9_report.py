"""M14.9 report: the switch replay uses the policy's own triggers, and reversals are counted."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from backtest.m14_9_report import (
    EntrySwitches,
    Readings,
    entry_switches,
    idle_cash_after_rebalances,
)
from backtest.policies.early_entry import BreadthReading, RegimeTrendReading
from backtest.policies.momentum_v2 import (
    D13_EARLY_BREADTH_TURN,
    D13_EARLY_CONFIRMED_CROSS,
    D13_EARLY_MOMENTUM_TURN,
    D13_EARLY_SCALE_IN,
    PAPER_RATIFIED_2026_09_06,
    RegimeReading,
)

SESSIONS = [d for d in (date(2024, 1, 1) + timedelta(days=n) for n in range(91)) if d.weekday() < 5]


def _on(session: date) -> bool:
    # Risk-off at the January rebalance, on from 10 January to 1 February (so the February
    # rebalance reads risk-on), off from 5 February (the March rebalance parks), on from 4 March.
    return date(2024, 1, 10) <= session < date(2024, 2, 5) or session >= date(2024, 3, 4)


def _regime(session: date) -> RegimeReading:
    level = Decimal("105") if _on(session) else Decimal("99")  # within 2% of the MA when off
    return RegimeReading(index_level=level, moving_average=Decimal("100"), knowable_date=session)


def _trend(session: date) -> RegimeTrendReading:
    streak = 0
    for s in reversed([s for s in SESSIONS if s <= session]):
        if not _on(s):
            break
        streak += 1
    r = _regime(session)
    return RegimeTrendReading(
        index_level=r.index_level,
        moving_average=r.moving_average,
        sessions_at_or_above=streak,
        short_average=Decimal("100") if session >= date(2024, 1, 8) else Decimal("98"),
        short_average_lagged=Decimal("99"),
        level_lagged=Decimal("90"),
        knowable_date=session,
    )


def _breadth(session: date) -> BreadthReading:
    share = Decimal("0.6") if session >= date(2024, 1, 12) else Decimal("0.4")
    return BreadthReading(
        share_above=share,
        share_above_lagged=Decimal("0.45"),
        names_counted=10,
        knowable_date=session,
    )


READINGS = Readings(regime=_regime, trend=_trend, breadth=_breadth)


def _count(params: object, bought: set[date] | None = None) -> EntrySwitches:
    return entry_switches(params, SESSIONS, READINGS, bought=bought or set())  # type: ignore[arg-type]


def test_d13_switches_only_on_rebalances() -> None:
    sw = _count(PAPER_RATIFIED_2026_09_06)
    # Parked at January's and March's rebalances, re-entered at February's; nothing early.
    assert (sw.parks, sw.monthly_reentries, sw.early, sw.park_sessions) == (2, 1, 0, 2)


def test_each_variant_reenters_on_its_own_session() -> None:
    assert _count(D13_EARLY_SCALE_IN).early_sessions == (date(2024, 1, 10), date(2024, 3, 4))
    assert _count(D13_EARLY_CONFIRMED_CROSS).early_sessions == (
        date(2024, 1, 16),
        date(2024, 3, 8),
    )
    assert _count(D13_EARLY_MOMENTUM_TURN).early_sessions[0] == date(2024, 1, 8)
    assert _count(D13_EARLY_BREADTH_TURN).early_sessions[0] == date(2024, 1, 12)


def test_an_early_reentry_unparks_so_the_next_rebalance_is_not_a_monthly_reentry() -> None:
    sw = _count(D13_EARLY_CONFIRMED_CROSS)
    assert sw.monthly_reentries == 0  # February's risk-on rebalance found the book invested
    assert sw.parks == 2


def test_a_reentry_the_index_gives_back_within_a_month_counts_as_reversed() -> None:
    # The momentum turn fires on 8 January, still below the MA; the index is below it again on
    # the 9th. The scale-in's 10 January entry sees the 5 February breakdown 18 sessions later.
    # Neither is re-parked: the February rebalance reads risk-on.
    cases = ((D13_EARLY_MOMENTUM_TURN, date(2024, 1, 8)), (D13_EARLY_SCALE_IN, date(2024, 1, 10)))
    for params, first in cases:
        sw = _count(params)
        assert sw.early_sessions == (first, date(2024, 3, 4))
        assert (sw.index_reversed, sw.reparked) == (1, 0)


def test_a_reentry_parked_again_at_the_next_rebalance_counts_as_reparked() -> None:
    def short_lived(session: date) -> RegimeReading:
        # On from 10 to 26 January only: the February rebalance reads risk-off.
        on = date(2024, 1, 10) <= session < date(2024, 1, 29)
        level = Decimal("105") if on else Decimal("95")
        return RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=session
        )

    readings = Readings(regime=short_lived, trend=_trend, breadth=_breadth)
    sw = entry_switches(D13_EARLY_SCALE_IN, SESSIONS, readings, bought=set())
    assert sw.early_sessions[0] == date(2024, 1, 10)
    assert sw.reparked == 1 and sw.index_reversed == 1
    # Inversion: D13 itself never re-enters early, so it has nothing to reverse.
    d13 = entry_switches(PAPER_RATIFIED_2026_09_06, SESSIONS, readings, bought=set())
    assert (d13.early, d13.reparked, d13.index_reversed) == (0, 0, 0)


def test_the_ledger_confirms_a_reentry_bought_on_the_next_session() -> None:
    sw = _count(D13_EARLY_SCALE_IN, bought={date(2024, 1, 11)})
    assert sw.early_confirmed == 1
    assert _count(D13_EARLY_SCALE_IN, bought={date(2024, 1, 10)}).early_confirmed == 0


def test_idle_cash_is_read_two_sessions_after_each_risk_on_rebalance() -> None:
    days = SESSIONS[:6]
    ledger = {
        "external_flows": [{"date": days[0].isoformat(), "amount": "-1000"}],
        "trades": [
            {"trade_date": days[1].isoformat(), "side": "BUY", "net_amount": "600"},
            {"trade_date": days[2].isoformat(), "side": "BUY", "net_amount": "300"},
            {"trade_date": days[4].isoformat(), "side": "SELL", "net_amount": "500"},
        ],
        "dividends": [{"received": days[2].isoformat(), "amount": "10"}],
        "interest": [],
    }
    nav = dict.fromkeys(days, Decimal("1000"))
    # 1000 - 600 - 300 + 10 at the close of the second session after the rebalance; the sale on
    # the fifth session is later and does not count there. Inversion: a later rebalance sees it.
    assert idle_cash_after_rebalances(ledger, nav, days, [days[0]]) == [Decimal("0.11")]
    assert idle_cash_after_rebalances(ledger, nav, days, [days[3]]) == [Decimal("0.61")]


def test_the_m14_10_report_decides_on_the_nifty500_cells() -> None:
    """M14.10 applies M14.9's rule to NIFTY 500; floor-only rows must not enter its verdict."""
    from backtest.m12_rerun import NIFTY500_WF_SET
    from backtest.m14_5_report import RunFacts, Switches
    from backtest.m14_9_report import EntryFacts, decision
    from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR

    v2 = NIFTY500_WF_SET[2].label
    zero = EntrySwitches(0, 0, 0, 0, 0, 0, 0)

    def fact(universe: str, window: str, floor: Decimal, label: str, xirr: str) -> EntryFacts:
        base = RunFacts(
            window=window,
            universe=universe,
            floor=floor,
            label=label,
            digest="d",
            replay_digest="r",
            xirr=Decimal(xirr),
            max_drawdown=Decimal("0.2"),
            excess=Decimal(0),
            charges=Decimal(1),
            final_nav=Decimal(1),
            trades=1,
            traded_value=Decimal(1),
            turnover=Decimal(1),
            switches=Switches(0, 0, 0, 0, 0),
            floor_refusals=0,
        )
        return EntryFacts(base=base, switches=zero)

    windows = ("decade", "six-year", "wf-selection", "wf-verification")
    facts = []
    for window in windows:
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            facts.append(fact("nifty500", window, floor, D13_PAPER_BASELINE.label, "0.20"))
            facts.append(fact("nifty500", window, floor, v2, "0.25"))  # V2 better on NIFTY 500
            facts.append(fact("turnover_floor", window, floor, D13_PAPER_BASELINE.label, "0.30"))
            facts.append(fact("turnover_floor", window, floor, v2, "0.10"))  # worse floor-only
    nifty = "\n".join(decision(facts, v2, family=NIFTY500_WF_SET, universe="nifty500"))
    floor_only = "\n".join(decision(facts, v2, family=NIFTY500_WF_SET))
    row = next(line for line in nifty.splitlines() if line.startswith(f"| {v2} "))
    assert "| yes | 8 / 8 |" in row and row.endswith("| yes |")
    inverted = next(line for line in floor_only.splitlines() if line.startswith(f"| {v2} "))
    assert "| no | 0 / 8 |" in inverted and inverted.endswith("| no |")
