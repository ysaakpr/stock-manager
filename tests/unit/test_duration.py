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

import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backtest.duration import (
    MANDATED_WINDOWS,
    MODELLED_ROUND_TRIP,
    _ad_hoc,
    _duration_of,
    _inventory,
    _overlapping_pairs,
    arms_that_ran,
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
    # The exact cell, computed here rather than matched loosely: -3% annual excess over 4000 trips
    # in a 20-name basket across ~9.997 years is -0.03 / (4000 / 9.997 / 20) per book turn.
    math = holding_period_math(
        _row(_FAST, xirr="0.06", drawdown="0.40", excess="-0.03", trips=4000),
        years=Decimal((date(2026, 8, 31) - date(2016, 9, 1)).days) / Decimal("365.25"),
    )
    assert math is not None
    cell = f"**{math.net_excess_per_book_turn:.2%}**"
    assert cell in report, f"expected the exact rendered cell {cell}"
    assert cell.startswith("**-")


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


def test_clearing_only_on_an_in_sample_window_is_not_clearing_the_bar() -> None:
    """A bar cleared on the window that selected for it is the selection, not an edge.

    This is the finding the old verdict erased: it treated all four windows alike, so a 31 % on
    the *selection* window produced the headline "the bar was cleared".
    """
    sweep = _split(selection_xirr="0.31", verification_xirr="0.09")
    for entry in sweep.windows:
        entry.result.rows.append(_row(_REFERENCE, xirr="0.08", drawdown="0.30", floor=HIGH_FLOOR))
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "**Answer: in-sample only.**" in report
    assert "the selection, not" in report


def test_a_cleared_bar_carries_its_window_floor_drawdown_and_duration() -> None:
    """Cleared out-of-sample and at the reachable floor — the only combination that counts."""
    sweep = _split(selection_xirr="0.31", verification_xirr="0.28")
    for entry in sweep.windows:
        entry.result.rows.append(_row(_REFERENCE, xirr="0.29", drawdown="0.26", floor=HIGH_FLOOR))
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "**Answer: the bar was cleared out-of-sample**" in report
    bar = report[report.index("## The bar:") :]
    assert "Walk-forward verification" in bar and "₹10 crore/day" in bar
    assert "26.00%" in bar  # the drawdown
    assert "fortnightly / 63-session re-underwrite" in bar  # the duration


def test_the_bar_section_marks_which_windows_were_in_sample() -> None:
    """Four overlapping windows are not four draws, and the section must not read as though."""
    sweep = _split(selection_xirr="0.31", verification_xirr="0.28")
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    bar = report[report.index("## The bar:") : report.index("## What each arm changed")]
    # "in-sample" is a substring of "out-of-sample", so the presence of both words proves
    # nothing. Check the two tags where they actually distinguish rows, below.
    assert "**out-of-sample**" in bar
    assert re.search(r"\(in-sample,", bar), "no row was tagged in-sample"
    # The overlap claim is counted, not asserted in prose. A selection/verification pair
    # *partitions*, so no pair shares a day and the sentence must say so rather than claim an
    # overlap that is not there.
    assert "do not overlap" in bar
    assert "pieces of evidence" not in bar
    # The selection window's clear is tagged in-sample; the verification window's is not.
    selection_line = next(
        line for line in bar.splitlines() if line.startswith("- **Walk-forward selection**")
    )
    verification_line = next(
        line for line in bar.splitlines() if line.startswith("- **Walk-forward verification**")
    )
    assert "in-sample" in selection_line and "out-of-sample" not in selection_line
    assert "**out-of-sample**" in verification_line


def test_a_single_floor_verdict_says_only_one_floor_was_run() -> None:
    """With one floor, max(floors) is min(floors): the discovery floor is not the reachable one."""
    sweep = _split(selection_xirr="0.31", verification_xirr="0.28")
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "Only one liquidity floor was run" in report
    assert "a verdict needs both" in report
    assert "what a real book could reach" not in report


def test_a_single_floor_run_says_so_in_the_setup_too() -> None:
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "**one floor**" in report


# ── the rest of the report's obligations ─────────────────────────────────────────────────────────


def test_the_report_lists_exactly_the_arms_that_ran_and_no_others() -> None:
    """The inventory is read off the rows. A one-arm sweep is a one-arm report, and says so.

    This is the defect the earlier version of this test locked in: it rendered a one-arm sweep and
    then asserted all twelve labels appeared, which passed only because the renderer iterated the
    module constant instead of the rows. A filtered run could therefore write a file to the
    deliverable's path claiming the whole grid ran.
    """
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "## What each arm changed" in report
    assert f"| {_REFERENCE} |" in report
    for arm in DURATION_ARMS:
        if arm.label != _REFERENCE:
            assert f"| {arm.label} |" not in report, f"{arm.label} never ran but was listed"
    assert "**1 arm**" in report, "the count came from the module, not from the rows"
    assert "**1 arms**" not in report, "the singular case is ungrammatical"
    assert "Run digests (determinism)" in report
    assert "d" * 64 in report


def test_a_subset_run_is_banner_marked_as_a_subset() -> None:
    """A filtered report must not read as the campaign, whatever path it was written to."""
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "filtered subset, not the campaign" in report
    assert "Do not read this file as the campaign" in report
    assert "stated arms did not run" in report


def _full_campaign() -> MultiWindowSweep:
    """Every mandated window with every stated arm — what the real campaign produces."""
    return MultiWindowSweep(
        windows=[
            WindowSweep(
                window=window,
                result=_result(
                    [_row(arm.label, xirr="0.20", drawdown="0.25") for arm in DURATION_ARMS],
                    start=window.start,
                    terminal=window.end,
                ),
            )
            for window in MANDATED_WINDOWS
        ],
        selected=_REFERENCE,
    )


def test_a_full_run_carries_no_subset_banner() -> None:
    report = render_duration_report(_full_campaign(), floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "filtered subset" not in report
    assert f"**{len(DURATION_ARMS)} arms**" in report
    for arm in DURATION_ARMS:
        assert f"| {arm.label} |" in report


def test_a_narrowed_window_set_is_banner_marked_even_with_every_arm() -> None:
    """--windows narrows a report exactly as much as --arms does, and must disclose it."""
    sweep = MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Decade", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(arm.label, xirr="0.20", drawdown="0.25") for arm in DURATION_ARMS],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            )
        ]
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "filtered subset, not the campaign" in report
    assert "not the 4 mandated windows" in report
    assert "stated arms did not run" not in report, "every arm ran; only the windows narrowed"


def test_arms_that_ran_reads_the_rows_in_first_appearance_order() -> None:
    sweep = _two_windows()
    assert [arm.label for arm in arms_that_ran(sweep)] == [_REFERENCE]


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


# ── the report's remaining honesty obligations ───────────────────────────────────────────────────


def test_an_arm_the_sampler_never_caught_falling_is_marked_not_printed_as_zero() -> None:
    """`0.00` for a missing denominator is the defect 4306e24 fixed; it must not reappear here."""
    sweep = MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Decade", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [
                        _row(_REFERENCE, xirr="0.25", drawdown="0"),
                        _row(_FAST, xirr="-0.10", drawdown="0.30"),
                    ],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            )
        ]
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    line = next(line for line in report.splitlines() if line.startswith(f"| 1 | {_REFERENCE}"))
    assert "no drawdown sampled" in line
    assert "**0.00**" not in line, "a missing denominator was printed as a real ratio"
    # The negative-XIRR arm, which does have a measured drawdown, keeps its real figures.
    other = next(line for line in report.splitlines() if line.startswith(f"| 2 | {_FAST}"))
    assert "30.00%" in other and "no drawdown sampled" not in other
    # And the legend explains the marker rather than leaving it to be guessed.
    assert "It is not a zero drawdown and it is not a perfect ratio" in report


def test_a_pipe_in_an_error_message_cannot_break_the_table() -> None:
    """An unescaped `|` shifts every later cell under the wrong header."""
    sweep = _two_windows()
    sweep.windows[0].result.rows.append(
        SweepRow(arm=_ARMS[_FAST], floor=LOW_FLOOR, error="bad SQL: a | b")
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    line = next(line for line in report.splitlines() if _FAST in line and "failed" in line)
    assert r"a \| b" in line
    header = next(line for line in report.splitlines() if line.startswith("| # | Strategy |"))
    # Count only *delimiting* pipes — an escaped one is content, which is the whole point.
    delimiters = len(re.findall(r"(?<!\\)\|", line))
    assert delimiters == header.count("|"), "the failed row has a different column count"


def test_the_report_states_the_baselines_own_traversals_not_one_pass_per_window() -> None:
    """ "One lake pass per window" was false: the two baselines each open their own."""
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "One shared lake pass" in report
    assert "further traversals, one per momentum-baseline run" in report
    assert "shared by every arm on this window" not in report


def test_the_benchmark_line_does_not_claim_identical_cashflows() -> None:
    """`SweepResult.benchmark_xirr` is the first successful arm's, not a figure all arms share."""
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "on identical cashflows" not in report
    assert "first arm on this window to produce a result" in report


def test_the_baselines_state_their_duration_rather_than_a_dash() -> None:
    """The duration column is the table's whole point; both baselines rotate monthly."""
    naive = next(arm for arm in DURATION_ARMS if arm.naive is not None)
    v2 = next(arm for arm in DURATION_ARMS if arm.v2 is not None)
    assert _duration_of(naive).startswith("monthly")
    assert _duration_of(v2).startswith("monthly")
    assert "—" not in _duration_of(naive) and "—" not in _duration_of(v2)


def test_the_two_weekly_ten_arms_are_distinguishable_in_the_duration_column() -> None:
    """The arm that moves only the min-hold floor must not render identically to its reference."""
    base = next(a for a in DURATION_ARMS if a.label == "M10.7 @ weekly / 10-session hold")
    floored = next(a for a in DURATION_ARMS if a.label.endswith("2-session floor"))
    assert _duration_of(base) != _duration_of(floored)
    assert "2-session floor" in _duration_of(floored)


def test_the_legend_states_that_rank_numbers_skip() -> None:
    """Inherited from the M12.2 renderer, so it is disclosed rather than restructured."""
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    assert "Rank numbers skip" in report
    assert "counts failed arms in M" in report


def test_the_overlap_claim_is_counted_rather_than_asserted() -> None:
    """The mandated set overlaps heavily; a set that does not must not be described as if it did."""
    mandated = MultiWindowSweep(
        windows=[
            WindowSweep(window=w, result=_result([], start=w.start, terminal=w.end))
            for w in MANDATED_WINDOWS
        ]
    )
    # Decade x Six-year, Decade x each walk-forward leg, Six-year x each leg — every pair but the
    # walk-forward's own, which partitions. Five of the six pairs.
    assert _overlapping_pairs(mandated) == 5
    report = render_duration_report(mandated, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "5 of the window pairs below share sessions" in report
    assert "do not overlap" not in report
    # And the non-overlapping case says the opposite rather than nothing.
    assert _overlapping_pairs(_split(selection_xirr="0.1", verification_xirr="0.1")) == 0


def test_the_inventory_sentence_names_only_the_arms_that_are_there() -> None:
    """Prose describing a reference or a baseline that did not run is the same defect as a count."""
    assert _inventory(DURATION_ARMS) == (
        "the M10.7 reference, 10 duration and band variations of it and 2 momentum baselines "
        "every row is priced against"
    )
    subset = [arm for arm in DURATION_ARMS if "weekly / 10" in arm.label or arm.naive is not None]
    sentence = _inventory(subset)
    assert "the M10.7 reference" not in sentence, "named a reference row that never ran"
    assert "of the M10.7 composite" in sentence, "'of it' dangles with no reference to refer to"
    assert "1 momentum baseline every" in sentence, "said '1 baselines'"
    assert _inventory([]) == "no arms at all — every one of them failed to produce a row"


# ── the guard reaches every site that prints a ratio ─────────────────────────────────────────────


def test_the_walk_forward_section_never_prints_an_unsampled_drawdown_as_zero() -> None:
    """The report's only out-of-sample claim must not read a missing denominator as measured.

    `ranked()` sorts on -return_per_drawdown and an unsampled drawdown yields zero, which outranks
    every negative-ratio arm — so in an underwater window the arm printed as **Chosen:** is exactly
    the kind of arm that got there on a missing denominator.
    """
    walk = _split(selection_xirr="0.30", verification_xirr="0.05")
    for entry in walk.windows:
        entry.result.rows[:] = [
            _row(_REFERENCE, xirr="0.30", drawdown="0"),  # never caught falling
            _row(_FAST, xirr="-0.05", drawdown="0.40"),
        ]
    report = render_duration_report(walk, floors=[LOW_FLOOR])
    section = report[report.index("## Walk-forward") : report.index("## The bar:")]
    assert "0.00% drawdown" not in section
    assert "(0.00)" not in section
    assert "no drawdown sampled" in section
    # The side-by-side table uses the dash rather than a fabricated ratio.
    chosen_line = next(
        line for line in section.splitlines() if line.startswith(f"| {_REFERENCE} ←")
    )
    # The two ratio cells (selection XIRR/DD, verification XIRR/DD) are dashes, not fabricated
    # zeros. Checked cell-by-cell: "30.00%" legitimately contains "0.00".
    cells = [cell.strip() for cell in chosen_line.split("|")]
    assert cells[4] == "—", f"selection ratio was {cells[4]!r}, expected a dash"
    assert cells[6] == "—", f"verification ratio was {cells[6]!r}, expected a dash"


def test_every_rendered_ratio_site_is_guarded() -> None:
    """A sweep in which nothing was ever sampled falling must print no `0.00` ratio anywhere."""
    sweep = _split(selection_xirr="0.30", verification_xirr="0.30")
    for entry in sweep.windows:
        entry.result.rows[:] = [
            _row(_REFERENCE, xirr="0.30", drawdown="0", floor=floor)
            for floor in (LOW_FLOOR, HIGH_FLOOR)
        ]
    report = render_duration_report(sweep, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "**0.00**" not in report
    assert "(0.00)" not in report
    assert "0.00% max drawdown" not in report
    assert "0.00% drawdown" not in report


# ── multiplicity, derived ────────────────────────────────────────────────────────────────────────


def test_the_multiplicity_bullet_counts_what_ran() -> None:
    """11 swing arms x 4 windows x 2 floors is 88 numbers, and the bullet must say 88."""
    report = render_duration_report(_full_campaign(), floors=[LOW_FLOOR, HIGH_FLOOR])
    swing = sum(1 for arm in DURATION_ARMS if arm.swing is not None)
    expected = swing * len(MANDATED_WINDOWS) * 2
    assert expected == 88
    assert f"**{expected} numbers**" in report
    assert "forty numbers" not in report
    assert f"{swing} swing arms over 4 windows on 2 liquidity floors" in report


def test_the_multiplicity_bullet_follows_a_narrower_run() -> None:
    report = render_duration_report(_two_windows(), floors=[LOW_FLOOR])
    # One swing arm, two windows, one floor.
    assert "**2 numbers**" in report
    assert "1 swing arm over 2 windows on 1 liquidity floor" in report


# ── relational prose is derived, never asserted ──────────────────────────────────────────────────


def test_a_walk_forward_only_sweep_does_not_claim_an_overlap_it_counted_as_zero() -> None:
    """The honest-limits bullet and the bar section's overlap line must agree."""
    report = render_duration_report(
        _split(selection_xirr="0.1", verification_xirr="0.1"), floors=[LOW_FLOOR]
    )
    assert "do not overlap" in report
    assert "no two of them share a session" in report
    assert "decade *contains* the six-year window" not in report
    assert "partitions* the decade" not in report


def test_a_sweep_with_no_verification_window_does_not_claim_a_selection_window() -> None:
    """The "chosen on, or overlapping it" clause is false when neither window exists."""
    sweep = MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Decade", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(_REFERENCE, xirr="0.31", drawdown="0.25")],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            )
        ]
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "**Answer: in-sample only.**" in report
    assert "held out no window at all" in report
    assert "the window an arm was chosen on" not in report
    assert "No window here was held out" in report


# ── scope, and the divergence section ────────────────────────────────────────────────────────────


def test_the_report_says_it_is_not_the_m12_3_gate() -> None:
    report = render_duration_report(_full_campaign(), floors=[LOW_FLOOR])
    assert "It is **not** the M12.3 gate" in report
    assert "M12-strategy-verdict.md" in report
    assert not report.startswith("# M12.3")


def test_arms_that_diverge_by_window_are_named() -> None:
    """M12.3's criterion, adopted: an arm clearing on one window and not another is named."""
    windows = []
    # Six-year clears, decade does not — the divergence the criterion asks about.
    for label, start, end, xirr in [
        ("Decade", date(2016, 9, 1), date(2026, 8, 31), "0.14"),
        ("Six-year", date(2019, 7, 1), date(2026, 8, 31), "0.31"),
    ]:
        windows.append(
            WindowSweep(
                window=Window(label=label, start=start, end=end),
                result=_result(
                    [
                        _row(_REFERENCE, xirr=xirr, drawdown="0.25"),
                        _row(_FAST, xirr="0.05", drawdown="0.25"),
                    ],
                    start=start,
                    terminal=end,
                ),
            )
        )
    report = render_duration_report(MultiWindowSweep(windows=windows), floors=[LOW_FLOOR])
    section = report[report.index("## Where the verdict changes with the window") :]
    assert "clears on the six-year window but not the decade" in section
    assert f"**{_REFERENCE}**" in section
    # The arm that cleared on neither is not named as diverging.
    diverging = section[: section.index("## ", 5)] if "## " in section[5:] else section
    assert _FAST not in diverging


def test_divergence_reports_selection_against_verification_too() -> None:
    walk = _split(selection_xirr="0.31", verification_xirr="0.09")
    report = render_duration_report(walk, floors=[LOW_FLOOR])
    section = report[report.index("## Where the verdict changes with the window") :]
    assert "clears on the selection window but not on verification" in section
    assert f"**{_REFERENCE}**" in section


def test_divergence_says_so_plainly_when_nothing_diverges() -> None:
    report = render_duration_report(
        _split(selection_xirr="0.09", verification_xirr="0.09"), floors=[LOW_FLOOR]
    )
    section = report[report.index("## Where the verdict changes with the window") :]
    assert "No arm's answer" in section


def test_divergence_needs_a_comparable_pair() -> None:
    """A decade-only run has no pair to compare, and says that rather than nothing."""
    sweep = MultiWindowSweep(
        windows=[
            WindowSweep(
                window=Window(label="Decade", start=date(2016, 9, 1), end=date(2026, 8, 31)),
                result=_result(
                    [_row(_REFERENCE, xirr="0.31", drawdown="0.25")],
                    start=date(2016, 9, 1),
                    terminal=date(2026, 8, 31),
                ),
            )
        ]
    )
    report = render_duration_report(sweep, floors=[LOW_FLOOR])
    assert "## Where the verdict changes" not in report
    # ...and the scope paragraph does not promise a section that is not there.
    assert "there is no divergence to name" in report
    assert "*Where the verdict changes with the window* below" not in report
