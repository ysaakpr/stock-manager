"""M14.12 report: the exit-aware switch replay, the whipsaw counts and the two-comparator rule."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backtest.m12_rerun import EARLY_EXIT_COMBO_SET
from backtest.m14_5_report import RunFacts, Switches
from backtest.m14_9_report import EntryFacts, EntrySwitches, Readings, entry_switches
from backtest.m14_12_report import conditions, decision_table, entry_view, exit_switches, verdict
from backtest.policies.momentum_v2 import (
    PAPER_CANDIDATE_V2_SREFILL_2026_11_07,
    PAPER_RATIFIED_2026_09_06,
    V2_SREFILL_DAILY_EXIT,
    V2_SREFILL_DAILY_EXIT_BANDED,
    RegimeReading,
)
from backtest.sweep import HIGH_FLOOR, LOW_FLOOR
from tests.unit.test_m14_9_report import READINGS, SESSIONS, _regime, _trend

C0 = PAPER_CANDIDATE_V2_SREFILL_2026_11_07
C1 = V2_SREFILL_DAILY_EXIT_BANDED
C2 = V2_SREFILL_DAILY_EXIT
EARLY_BUY = {date(2024, 1, 2)}  # a purchase filled before every session below


# The script (tests.unit.test_m14_9_report): risk-off (99, inside the 2% band) at the January
# rebalance, on from 10 January to 1 February, off (99) from 5 February, on from 4 March. The
# momentum turn reads true from 8 January onwards whenever the level is 99.


def test_without_the_exit_the_replay_is_m14_9s_for_d13_and_c0() -> None:
    for params in (PAPER_RATIFIED_2026_09_06, C0):
        for bought in (set(), EARLY_BUY | {date(2024, 1, 9)}):
            sw = exit_switches(params, SESSIONS, READINGS, bought=bought, sold=set())
            view = entry_view(sw, SESSIONS, READINGS, bought=bought)
            assert view == entry_switches(params, SESSIONS, READINGS, bought=bought)


def test_c2_parks_below_the_ma_and_v2_reenters_each_new_spell() -> None:
    sw = exit_switches(C2, SESSIONS, READINGS, bought=EARLY_BUY, sold=set())
    # January: the monthly park, V2 on the 8th, the exit on the 9th (99 < MA), V2 on the 10th.
    assert sw.parks[:2] == ((date(2024, 1, 1), "monthly"), (date(2024, 1, 9), "daily"))
    assert sw.reentries[:2] == ((date(2024, 1, 8), "early"), (date(2024, 1, 10), "early"))
    # February in the gap [0.98, 1.00) x MA: park and V2 re-entry alternate session by session.
    feb = [(s, k) for s, k in sw.parks if date(2024, 2, 5) <= s < date(2024, 3, 1)]
    back = [(s, k) for s, k in sw.reentries if date(2024, 2, 5) <= s < date(2024, 3, 1)]
    assert feb[0] == (date(2024, 2, 5), "daily") and back[0] == (date(2024, 2, 6), "early")
    assert len(feb) == 10 and len(back) == 9  # the 29 Feb park re-enters on 4 March
    assert sw.parks_reversed == len(sw.parks)  # every park re-entered within 21 sessions
    assert sw.reentries_reversed >= len(feb)


def test_c1_waits_for_the_band_inversion() -> None:
    # 99 is inside the 2% band: C1 never exits daily on this script; C0's switches result.
    c1 = exit_switches(C1, SESSIONS, READINGS, bought=EARLY_BUY, sold=set())
    c0 = exit_switches(C0, SESSIONS, READINGS, bought=EARLY_BUY, sold=set())
    assert c1 == c0
    assert c1.count("daily", parks=True) == 0

    def deep(session: date) -> RegimeReading:
        r = _regime(session)
        level = Decimal("97.9") if r.index_level < r.moving_average else r.index_level
        return RegimeReading(
            index_level=level, moving_average=r.moving_average, knowable_date=session
        )

    readings = Readings(regime=deep, trend=_trend, breadth=None)
    sw = exit_switches(C1, SESSIONS, readings, bought=EARLY_BUY, sold=set())
    assert (date(2024, 2, 5), "daily") in sw.parks


def test_no_daily_exit_before_any_purchase_has_settled() -> None:
    # The re-entry of 8 January fills on the 9th: on the 9th no settled shares are held yet.
    sw = exit_switches(C2, SESSIONS, READINGS, bought={date(2024, 1, 9)}, sold=set())
    assert (date(2024, 1, 9), "daily") not in sw.parks
    assert sw.reentries[0] == (date(2024, 1, 8), "early")


def test_the_ledger_confirms_parks_and_reentries_on_the_next_session() -> None:
    sw = exit_switches(
        C2,
        SESSIONS,
        READINGS,
        bought=EARLY_BUY | {date(2024, 1, 9)},
        sold={date(2024, 1, 10)},
    )
    # The 8 January re-entry (bought 9th) and the 9 January park (sold 10th) are confirmed.
    assert sw.reentries_confirmed == 1 and sw.parks_confirmed == 1


def test_d13_has_no_reversal_within_21_sessions_on_monthly_switches() -> None:
    sw = exit_switches(PAPER_RATIFIED_2026_09_06, SESSIONS, READINGS, bought=set(), sold=set())
    # Parks 1 Jan and 1 Mar, re-entry 1 Feb: 23 and 20 sessions apart.
    assert sw.parks == ((date(2024, 1, 1), "monthly"), (date(2024, 3, 1), "monthly"))
    assert sw.reentries == ((date(2024, 2, 1), "monthly"),)
    assert sw.parks_reversed == 0  # 1 Jan -> 1 Feb is 23 sessions
    assert sw.reentries_reversed == 1  # 1 Feb -> 1 Mar is 21 sessions


# ── the two-comparator decision rule ─────────────────────────────────────────────────────────────


_LABELS = [arm.label for arm in EARLY_EXIT_COMBO_SET]
D13_L, C0_L, C1_L, C2_L = _LABELS
_WINDOWS = ("decade", "six-year", "wf-selection", "wf-verification")
_ZERO_SW = EntrySwitches(0, 0, 0, 0, 0, 0, 0)


def _fact(window: str, floor: Decimal, label: str, xirr: str, dd: str = "0.2") -> EntryFacts:
    base = RunFacts(
        window=window,
        universe="nifty500",
        floor=floor,
        label=label,
        digest="d",
        replay_digest="r",
        xirr=Decimal(xirr),
        max_drawdown=Decimal(dd),
        excess=Decimal(0),
        charges=Decimal(1),
        final_nav=Decimal(1),
        trades=1,
        traded_value=Decimal(1),
        turnover=Decimal(1),
        switches=Switches(0, 0, 0, 0, 0),
        floor_refusals=0,
    )
    return EntryFacts(base=base, switches=_ZERO_SW)


def _facts(xirr: dict[str, str]) -> list[EntryFacts]:
    return [
        _fact(window, floor, label, value)
        for window in _WINDOWS
        for floor in (LOW_FLOOR, HIGH_FLOOR)
        for label, value in xirr.items()
    ]


def test_an_exit_arm_must_beat_c0_as_well_as_d13() -> None:
    # C1 beats D13 everywhere but trails C0 everywhere: not recommended, C0 named.
    facts = _facts({D13_L: "0.20", C0_L: "0.30", C1_L: "0.25", C2_L: "0.10"})
    assert conditions(facts, C1_L, D13_L).met
    assert not conditions(facts, C1_L, C0_L).met
    text = "\n".join(verdict(facts, C1_L))
    assert "not recommended" in text and "against C0" in text and "against D13" not in text
    # Inversion: C1 above both is recommended.
    better = _facts({D13_L: "0.20", C0_L: "0.22", C1_L: "0.25", C2_L: "0.10"})
    assert "recommended as a candidate" in "\n".join(verdict(better, C1_L))


def test_choosing_d13_or_c0_recommends_no_change() -> None:
    facts = _facts({D13_L: "0.20", C0_L: "0.22", C1_L: "0.25", C2_L: "0.26"})
    for chosen in (D13_L, C0_L):
        text = "\n".join(verdict(facts, chosen))
        assert "no change is recommended" in text and "recommended as a candidate" not in text


def test_a_deeper_drawdown_fails_the_two_point_condition() -> None:
    facts = _facts({D13_L: "0.20", C0_L: "0.20", C1_L: "0.20"})
    facts = [
        _fact(f.base.window, f.base.floor, f.base.label, "0.3", "0.23")
        if f.base.label == C1_L and f.base.window == "decade" and f.base.floor == LOW_FLOOR
        else f
        for f in facts
    ]
    c = conditions(facts, C1_L, D13_L)
    assert not c.drawdown and c.worst == Decimal("0.03")
    table = "\n".join(decision_table(facts, C1_L))
    assert f"| {C1_L} | D13 | yes | 8 / 8 | no (worst +3.00pp) | no |" in table
