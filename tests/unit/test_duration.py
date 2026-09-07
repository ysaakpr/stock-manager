"""M12.3 — the duration-and-window report, unit-tested offline (EXECUTION_PLAN §7, X2).

:mod:`backtest.sweep`'s tests pin the *runner* — one lake pass per window, the arm grid, the
floors. This file pins what the runner cannot: the report those results are rendered into, whose
whole job is to keep four windows readable without inviting anyone to average them.

Three claims are tested here rather than read off the output:

* **no figure is averaged across windows** — the same arm on two windows renders both figures and
  never their mean, and each window's section is self-contained.
* **the holding-period arithmetic is arithmetic, including where it is negative** — the fast arms
  are supposed to come out underwater against the modelled round trip, and an arm with no closed
  round trip is supposed to have *no* figure rather than a zero that reads as a measurement.
* **the bar is answered with its conditions attached** — the window, the floor, the drawdown and
  the duration, or the word "no".

Offline: no lake, no network, no wall clock. The rows are built from stub runs, because what is
under test is the rendering and the arithmetic, never the replay.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backtest.duration import (
    MANDATED_WINDOWS,
    MODELLED_ROUND_TRIP,
    _ad_hoc,
    holding_period_math,
    render_duration_report,
)
from backtest.sweep import (
    DURATION_ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    MultiWindowSweep,
    SweepResult,
    SweepRow,
    Window,
    WindowRole,
    WindowSweep,
)

_ARMS = {arm.label: arm for arm in DURATION_ARMS}
_REFERENCE = "Swing composite (M10.7)"
_FAST = "M10.7 @ weekly / 10-session hold"


def _run(*, xirr: str, drawdown: str, excess: str, charges: str = "200000") -> Any:
    return SimpleNamespace(
        comparison=SimpleNamespace(
            portfolio_xirr=Decimal(xirr),
            benchmark_xirr=Decimal("0.0856"),
            excess_over_benchmark=Decimal(excess),
        ),
        max_drawdown=Decimal(drawdown),
        total_charges=Decimal(charges),
        benchmark_index_name="NIFTY-TRI L1 proxy",
        mean_universe=Decimal("908.9"),
        result=SimpleNamespace(journal=(), digest=lambda: "d" * 64),
    )


def _row(
    label: str,
    *,
    xirr: str,
    drawdown: str,
    excess: str = "0",
    trips: int = 1200,
    floor: Decimal = LOW_FLOOR,
) -> SweepRow:
    return SweepRow(
        arm=_ARMS[label],
        floor=floor,
        run=cast(Any, _run(xirr=xirr, drawdown=drawdown, excess=excess)),
        round_trips=trips,
        median_hold_days=43,
    )


def _result(rows: list[SweepRow], *, start: date, terminal: date) -> SweepResult:
    return SweepResult(
        rows=rows,
        start=start,
        terminal=terminal,
        sessions=len(rows) * 100,
        feature_dates=250,
        benchmark_xirr=Decimal("0.0856"),
        benchmark_name="NIFTY-TRI L1 proxy",
    )


# ── the mandated windows ─────────────────────────────────────────────────────────────────────────


def test_the_mandated_windows_are_the_three_the_owner_stated() -> None:
    """Decade, six-year, and a walk-forward split — stated before any of them was run."""
    spans = {(w.label, w.start, w.end, w.role) for w in MANDATED_WINDOWS}
    assert spans == {
        ("Decade", date(2016, 9, 1), date(2026, 8, 31), WindowRole.STANDALONE),
        ("Six-year", date(2019, 7, 1), date(2026, 8, 31), WindowRole.STANDALONE),
        (
            "Walk-forward selection",
            date(2016, 9, 1),
            date(2021, 8, 31),
            WindowRole.SELECTION,
        ),
        (
            "Walk-forward verification",
            date(2021, 9, 1),
            date(2026, 8, 31),
            WindowRole.VERIFICATION,
        ),
    }
    labels = [w.label for w in MANDATED_WINDOWS]
    assert labels.index("Walk-forward selection") < labels.index("Walk-forward verification")


# ── no figure is averaged across windows ─────────────────────────────────────────────────────────


def _two_windows() -> MultiWindowSweep:
    """The same arm on two windows, at figures whose mean is a distinctive string."""
    return MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Rich", start=date(2019, 7, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(_REFERENCE, xirr="0.31", drawdown="0.25")],
                    start=date(2019, 7, 1),
                    terminal=date(2026, 8, 31),
                ),
            ),
            WindowSweep(
                window=Window(label="Lean", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(_REFERENCE, xirr="0.13", drawdown="0.35")],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            ),
        ]
    )


def test_the_report_states_both_windows_and_never_their_mean() -> None:
    """31 % on one window and 13 % on another is not 22 % — the blend is the deleted finding."""
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "31.00%" in report and "13.00%" in report
    assert "22.00%" not in report, "a mean XIRR across windows appeared in the report"
    assert "30.00%" not in report, "a mean drawdown across windows appeared in the report"


def test_each_window_gets_its_own_section_with_its_own_span() -> None:
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert report.count("## Window — ") == 2
    assert "## Window — Rich: 2019-07-01 → 2026-08-31" in report
    assert "## Window — Lean: 2016-09-01 → 2026-08-31" in report
    assert "never averaged" in report or "no combined ranking" in report


def test_the_report_states_both_liquidity_floors_on_every_window() -> None:
    sweep = _two_windows()
    for entry in sweep.windows:
        entry.result.rows.append(_row(_REFERENCE, xirr="0.09", drawdown="0.22", floor=HIGH_FLOOR))
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert report.count("### Ranked — ₹1 crore/day liquidity floor") == 2
    assert report.count("### Ranked — ₹10 crore/day liquidity floor") == 2


def test_a_failed_arm_keeps_its_row_in_the_rendered_table() -> None:
    sweep = _two_windows()
    sweep.windows[0].result.rows.append(
        SweepRow(arm=_ARMS[_FAST], floor=LOW_FLOOR, error="no sessions in window")
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "**failed**" in report
    assert "no sessions in window" in report


# ── the holding-period arithmetic ────────────────────────────────────────────────────────────────


def test_the_arithmetic_divides_excess_by_book_turns_not_by_round_trips() -> None:
    """One round trip moves 1/top_n of the capital, so the denominator is book rotations."""
    row = _row(_REFERENCE, xirr="0.17", drawdown="0.25", excess="0.06", trips=1000)
    math = holding_period_math(row, years=Decimal("10"))
    assert math is not None
    assert math.turns_per_year == Decimal("100")
    assert math.book_turns_per_year == Decimal("5")  # 100 trips / a 20-name basket
    assert math.net_excess_per_book_turn == Decimal("0.06") / Decimal("5")
    assert math.gross_excess_per_book_turn == math.net_excess_per_book_turn + MODELLED_ROUND_TRIP
    assert math.realised_cost_per_trip == Decimal("200000") / Decimal("1000")


def test_the_arithmetic_goes_negative_on_a_fast_arm_that_lost_to_the_benchmark() -> None:
    """M10.7's whole argument: friction is per trade while alpha accrues per week held."""
    row = _row(_FAST, xirr="0.06", drawdown="0.40", excess="-0.03", trips=4000)
    math = holding_period_math(row, years=Decimal("10"))
    assert math is not None
    assert math.net_excess_per_book_turn < Decimal("0")


def test_a_negative_per_turn_figure_is_rendered_not_hidden() -> None:
    sweep = MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Decade", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(_FAST, xirr="0.06", drawdown="0.40", excess="-0.03", trips=4000)],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            )
        ]
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "Holding-period arithmetic" in report
    assert "-0.1" in report or "-0.0" in report, "the negative per-turn figure was not printed"


def test_an_arm_with_no_closed_round_trip_has_no_arithmetic_rather_than_a_zero() -> None:
    """An undefined ratio is not a measurement, and a zero in its place is a claim."""
    assert (
        holding_period_math(
            _row(_REFERENCE, xirr="0.1", drawdown="0.2", trips=0), years=Decimal("10")
        )
        is None
    )
    assert (
        holding_period_math(
            SweepRow(arm=_ARMS[_REFERENCE], floor=LOW_FLOOR, error="boom"), years=Decimal("10")
        )
        is None
    )
    assert (
        holding_period_math(_row(_REFERENCE, xirr="0.1", drawdown="0.2"), years=Decimal("0"))
        is None
    )


# ── the walk-forward, and the bar ────────────────────────────────────────────────────────────────


def _split(*, selection_xirr: str, verification_xirr: str) -> MultiWindowSweep:
    return MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(
                    label="Walk-forward selection",
                    start=date(2016, 9, 1),
                    end=date(2021, 8, 31),
                    role=WindowRole.SELECTION,
                ),
                result=_result(
                    [
                        _row(_REFERENCE, xirr=selection_xirr, drawdown="0.20"),
                        _row(_FAST, xirr="0.10", drawdown="0.40"),
                    ],
                    start=date(2016, 9, 1),
                    terminal=date(2021, 8, 31),
                ),
            ),
            WindowSweep(
                window=Window(
                    label="Walk-forward verification",
                    start=date(2021, 9, 1),
                    end=date(2026, 8, 31),
                    role=WindowRole.VERIFICATION,
                ),
                result=_result(
                    [
                        _row(_REFERENCE, xirr=verification_xirr, drawdown="0.30"),
                        _row(_FAST, xirr="0.20", drawdown="0.25"),
                    ],
                    start=date(2021, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            ),
        ],
        selected=_REFERENCE,
    )


def test_the_choice_is_named_before_any_verification_figure() -> None:
    """The frozen name appears above the side-by-side table, not after it."""
    report = render_duration_report(
        _split(selection_xirr="0.30", verification_xirr="0.05"), floors=[LOW_FLOOR]
    )
    assert report.index(f"**Chosen: {_REFERENCE}**") < report.index(
        "Selection rank against verification rank"
    )


def test_both_ranks_are_printed_so_the_decay_is_visible() -> None:
    """Chosen first on selection, second on verification — a column, not an inference."""
    report = render_duration_report(
        _split(selection_xirr="0.30", verification_xirr="0.05"), floors=[LOW_FLOOR]
    )
    table = report[report.index("Selection rank against verification rank") :]
    chosen_line = next(line for line in table.splitlines() if line.startswith(f"| {_REFERENCE} ←"))
    cells = [cell.strip() for cell in chosen_line.split("|")]
    assert cells[3] == "1", "the chosen arm did not rank first on the selection window"
    assert cells[5] == "2", "the verification rank was not printed beside the selection rank"


def test_the_bar_is_answered_no_when_nothing_cleared_it() -> None:
    report = render_duration_report(
        _split(selection_xirr="0.14", verification_xirr="0.05"), floors=[LOW_FLOOR]
    )
    assert "**Answer: no.**" in report


def test_clearing_the_bar_only_on_the_discovery_floor_is_reported_as_that() -> None:
    """A number reachable only where the fill model is a claim has not cleared the bar."""
    sweep = _split(selection_xirr="0.31", verification_xirr="0.28")
    for entry in sweep.windows:
        entry.result.rows.append(_row(_REFERENCE, xirr="0.11", drawdown="0.30", floor=HIGH_FLOOR))
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "**Answer: only on the discovery floor.**" in report


def test_a_cleared_bar_carries_its_window_floor_drawdown_and_duration() -> None:
    sweep = _split(selection_xirr="0.31", verification_xirr="0.28")
    for entry in sweep.windows:
        entry.result.rows.append(_row(_REFERENCE, xirr="0.29", drawdown="0.26", floor=HIGH_FLOOR))
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "**Answer: the bar was cleared**" in report
    bar = report[report.index("## The bar:") :]
    assert "Walk-forward selection" in bar and "₹10 crore/day" in bar
    assert "26.00%" in bar  # the drawdown
    assert "fortnightly / 63-session re-underwrite" in bar  # the duration


# ── the rest of the report's obligations ─────────────────────────────────────────────────────────


def test_the_report_says_what_each_arm_changed_and_keeps_its_digests() -> None:
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "## What each arm changed" in report
    for arm in DURATION_ARMS:
        assert f"| {arm.label} |" in report
    assert "Run digests (determinism)" in report
    assert "d" * 64 in report


def test_the_report_states_its_honest_limits() -> None:
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "## Honest limits of this measurement" in report
    assert "in-sample by construction" in report
    assert "126 sessions" in report  # the arms outside M10.7's stated 7-90 day band


# ── the CLI's window selection ───────────────────────────────────────────────────────────────────


def test_an_ad_hoc_window_may_carry_a_role_so_the_split_can_be_smoke_tested() -> None:
    window = _ad_hoc("Sel:2023-09-01:2024-02-29:selection")
    assert window.role is WindowRole.SELECTION
    assert window.start == date(2023, 9, 1) and window.end == date(2024, 2, 29)
    assert _ad_hoc("Short:2023-09-01:2024-08-31").role is WindowRole.STANDALONE


def test_a_malformed_ad_hoc_window_is_refused() -> None:
    for spec in ("Short", "Short:2023-09-01", "Short:2023-09-01:2024-08-31:nonsense", "::"):
        with pytest.raises(ValueError):
            _ad_hoc(spec)
