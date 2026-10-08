"""M14.5 report: the regime-switch count replays the policy's own triggers, per variant."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from backtest.m14_5_report import MARKER, RunFacts, Switches, render, scorecard, switches
from backtest.policies.momentum_v2 import PAPER_RATIFIED_2026_09_06, RegimeReading
from backtest.sweep import D13_DAILY_REENTRY, D13_PAPER_BASELINE, LOW_FLOOR

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
    assert _count(D13) == Switches(1, 1, 0, 0, park_sessions=1)


def test_daily_reentry_counts_the_mid_month_reentry_but_no_mid_month_exit() -> None:
    assert _count(REENTRY) == Switches(
        1, 1, 0, 0, park_sessions=1
    )  # re-enters Jan 10, Feb 15 is not acted on


def test_daily_both_ways_counts_the_breakdown_and_the_recovery() -> None:
    assert _count(BOTH) == Switches(2, 2, 0, 0, park_sessions=2)


def test_the_band_delays_reentry_but_not_the_count() -> None:
    # 101 is inside the 2% band: the banded arm waits for the 17th; still one re-entry.
    assert _count(BANDED) == Switches(1, 1, 0, 0, park_sessions=1)


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


def test_floor_labels_read_in_whole_crore() -> None:
    from backtest.m14_5_report import _floor_label
    from backtest.sweep import HIGH_FLOOR, LOW_FLOOR

    assert _floor_label(LOW_FLOOR) == "₹1 cr/day floor"
    assert _floor_label(HIGH_FLOOR) == "₹10 cr/day floor"


def test_a_park_held_across_rebalances_is_one_park_but_reissued_each_risk_off_rebalance() -> None:
    # Risk-off at the January and February rebalances: one entry into the parked state, two
    # sessions on which the monthly rule issues the park (each can meet A8's floor again).
    held = switches(
        D13,
        SESSIONS,
        _readings({date(2024, 1, 1): "95", date(2024, 2, 20): "105"}),
        sold=set(),
        bought=set(),
    )
    assert (held.parks, held.park_sessions) == (1, 2)


# ── the scorecard ────────────────────────────────────────────────────────────────────────────────


def _facts(label: str, window: str, xirr: str, dd: str, *, charges: str = "100") -> RunFacts:
    return RunFacts(
        window=window,
        universe="turnover_floor",
        floor=LOW_FLOOR,
        label=label,
        digest="d",
        replay_digest="r",
        xirr=Decimal(xirr),
        max_drawdown=Decimal(dd),
        excess=Decimal("0"),
        charges=Decimal(charges),
        final_nav=Decimal("1"),
        trades=1,
        traded_value=Decimal("1"),
        turnover=Decimal("2.00"),
        switches=Switches(0, 0, 0, 0, park_sessions=0),
        floor_refusals=0,
    )


def _row(lines: list[str], label: str) -> list[str]:
    (line,) = [line for line in lines if line.startswith(f"| {label} |")]
    return [cell.strip() for cell in line.strip("|").split("|")]


def test_scorecard_counts_wins_ties_and_names_the_lost_cells() -> None:
    base = D13_PAPER_BASELINE.label
    facts = [
        _facts(base, "decade", "0.25", "0.25"),
        _facts(base, "six-year", "0.30", "0.20"),
        _facts(base, "wf-selection", "0.20", "0.20"),
        _facts(D13_DAILY_REENTRY, "decade", "0.30", "0.25", charges="150"),  # better, DD tie
        _facts(D13_DAILY_REENTRY, "six-year", "0.30", "0.30"),  # worse, DD worse
        _facts(D13_DAILY_REENTRY, "wf-selection", "0.20", "0.20"),  # exact tie both ways
    ]
    cells = _row(scorecard(facts), D13_DAILY_REENTRY)
    assert cells[1] == "3"
    assert cells[2] == "1 / 1 / 1"
    assert cells[3] == "floor-only six-year ₹1 cr"
    assert cells[4] == "1 / 2 / 0"
    assert cells[5] == "+10.00pp"
    assert cells[6] == "2 / 3"  # 0.30 twice clears the bar; 0.20 does not
    assert cells[8] == "+0% to +50%"


def test_scorecard_flips_with_the_comparison_inversion() -> None:
    base = D13_PAPER_BASELINE.label
    facts = [
        _facts(base, "decade", "0.30", "0.20"),
        _facts(D13_DAILY_REENTRY, "decade", "0.25", "0.25"),
    ]
    assert _row(scorecard(facts), D13_DAILY_REENTRY)[2] == "0 / 0 / 1"
    flipped = [
        _facts(base, "decade", "0.25", "0.25"),
        _facts(D13_DAILY_REENTRY, "decade", "0.30", "0.20"),
    ]
    assert _row(scorecard(flipped), D13_DAILY_REENTRY)[2] == "1 / 0 / 0"
