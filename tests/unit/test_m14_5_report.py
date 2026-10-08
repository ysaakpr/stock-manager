"""M14.5 report: the regime-switch count replays the policy's own triggers, per variant."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from backtest.m14_5_report import MARKER, Switches, render, switches
from backtest.policies.momentum_v2 import PAPER_RATIFIED_2026_09_06, RegimeReading

D13 = PAPER_RATIFIED_2026_09_06
REENTRY = replace(D13, regime_daily_reentry=True)
BOTH = replace(D13, regime_daily_reentry=True, regime_daily_exit=True)
BANDED = replace(REENTRY, regime_daily_band=Decimal("0.02"))

SESSIONS = [d for d in (date(2024, 1, 1) + timedelta(days=n) for n in range(91)) if d.weekday() < 5]


def _readings(levels: dict[date, str]) -> dict[date, RegimeReading]:
    """Level 105 (risk-on) unless ``levels`` says otherwise from that date on."""
    out: dict[date, RegimeReading] = {}
    level = "105"
    for session in SESSIONS:
        level = levels.get(session, level)
        out[session] = RegimeReading(
            index_level=Decimal(level), moving_average=Decimal("100"), knowable_date=session
        )
    return out


# Risk-off at the January rebalance, recovers to 101 (inside a 2% band) on the 10th and to 105 on
# the 17th, breaks down on Feb 15, recovers on Feb 29.
_SCRIPT = {
    date(2024, 1, 1): "95",
    date(2024, 1, 10): "101",
    date(2024, 1, 17): "105",
    date(2024, 2, 15): "95",
    date(2024, 2, 29): "105",
}


def _count(params: object) -> Switches:
    return switches(params, SESSIONS, _readings(_SCRIPT), sold=set(), bought=set())  # type: ignore[arg-type]


def test_d13_switches_only_on_rebalance_sessions() -> None:
    # Parked at the January rebalance, re-entered at February's; the mid-Feb breakdown is unread.
    assert _count(D13) == Switches(1, 1, 0, 0)


def test_daily_reentry_counts_the_mid_month_reentry_but_no_mid_month_exit() -> None:
    assert _count(REENTRY) == Switches(1, 1, 0, 0)  # re-enters Jan 10, Feb 15 is not acted on


def test_daily_both_ways_counts_the_breakdown_and_the_recovery() -> None:
    assert _count(BOTH) == Switches(2, 2, 0, 0)


def test_the_band_delays_reentry_but_not_the_count() -> None:
    # 101 is inside the 2% band: the banded arm waits for the 17th; still one re-entry.
    assert _count(BANDED) == Switches(1, 1, 0, 0)


def test_switches_are_confirmed_only_by_a_fill_on_the_following_session() -> None:
    readings = _readings(_SCRIPT)
    jan10, jan11 = date(2024, 1, 10), date(2024, 1, 11)
    feb15, feb16 = date(2024, 2, 15), date(2024, 2, 16)
    confirmed = switches(BOTH, SESSIONS, readings, sold={feb16}, bought={jan11})
    assert (confirmed.parks_confirmed, confirmed.reentries_confirmed) == (1, 1)
    same_day = switches(BOTH, SESSIONS, readings, sold={feb15}, bought={jan10})
    assert (same_day.parks_confirmed, same_day.reentries_confirmed) == (0, 0)


def test_render_keeps_the_hand_written_section() -> None:
    text = render([], manifests={}, selected={}, hand_written="\n## Analysis\n\nkept\n")
    assert text.split(MARKER, 1)[1].strip() == "## Analysis\n\nkept"
