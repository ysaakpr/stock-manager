"""M12.3 — the swing composite across multiple durations and multiple windows (X2).

The owner asked to try the M10.7 composite "in multiple duration and window". Neither axis
existed. :mod:`backtest.sweep` varies the *signal* across twenty-three arms but pins M10.7's own
three legs to one cadence, and its holding-period family sits on the short composite instead;
and every comparison report in this repo, M10.7's included, measured one window. This module is
both axes at once: :data:`~backtest.sweep.DURATION_ARMS` over :data:`MANDATED_WINDOWS`.

**The windows are stated, and never averaged.** The full decade, the 2019-07 window the repo's
existing ~23 % figures came from, and a walk-forward split that chooses on 2016-09..2021-08 and
verifies on 2021-09..2026-08 (owner decision, 2026-09-07). Each gets its own lake pass, its own
benchmark and its own table. A mean across them would erase the finding that made three windows
necessary — the same policies earn ~22 % over six years and 12-15 % over ten, so a single blended
number is the six-year window's optimism wearing the decade's authority.

**The choice is made before the answer is read.** The selection window is swept first and its winner
frozen (:attr:`~backtest.sweep.MultiWindowSweep.selected`) before the verification window is opened,
and both ranks are printed side by side so the decay from selection to verification is a column
rather than an inference.

**The holding period is a cost decision, so the report states the arithmetic.** M10.7 argued the
composite's excess accrues at a near-constant rate per week while friction is paid per trade, which
makes a fast cadence underwater before it starts. Every window here carries that arithmetic against
its own *measured* turnover — net excess per book turn against the modelled ~0.45 % round trip —
including the arms where it comes out negative, because those are the arms the argument predicted.

What this module never does: fit an arm or a window to a result (both lists are stated before any
run), average a figure across windows, pool two windows into one ranking, or read a wall clock —
every replay drives a ``FrozenClock`` exactly as the rest of the backtest package does.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.sweep import (
    DURATION_ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    MultiWindowSweep,
    SweepResult,
    SweepRow,
    Window,
    WindowRole,
    WindowSweep,
    rank_of,
    row_of,
    run_multi_window_sweep,
)
from backtest.verdict import BAR
from dataplatform.logging import get_logger

__all__ = [
    "MANDATED_WINDOWS",
    "MODELLED_ROUND_TRIP",
    "HoldingPeriodMath",
    "holding_period_math",
    "render_duration_report",
]

_LOG = get_logger(__name__)

_ZERO = Decimal("0")
_DAYS_PER_YEAR = Decimal("365.25")

_REPORT_PATH = Path("ops/gates/M12-swing-duration-window-report.md")

#: The modelled cost of one position round trip: 0.223 % statutory (``execution/costs/rates.yaml``
#: — STT both sides, stamp, exchange/SEBI/GST) plus roughly 0.22 % of modelled slippage. This is the
#: figure M10.7's holding-period argument is priced against, carried here unchanged so the two
#: reports' arithmetic is comparable. It is a *model*, not a measurement — see the honest limits.
MODELLED_ROUND_TRIP = Decimal("0.0045")

#: The three windows the owner mandated (2026-09-07), stated before any of them was run. The
#: walk-forward pair is two windows, not one, because a split reported as a single figure is not a
#: split. Order matters: the selection window is swept before the verification window.
MANDATED_WINDOWS: tuple[Window, ...] = (
    Window(
        label="Decade",
        start=date(2016, 9, 1),
        end=date(2026, 8, 31),
        role=WindowRole.STANDALONE,
    ),
    Window(
        label="Six-year",
        start=date(2019, 7, 1),
        end=date(2026, 8, 31),
        role=WindowRole.STANDALONE,
    ),
    Window(
        label="Walk-forward selection",
        start=date(2016, 9, 1),
        end=date(2021, 8, 31),
        role=WindowRole.SELECTION,
    ),
    Window(
        label="Walk-forward verification",
        start=date(2021, 9, 1),
        end=date(2026, 8, 31),
        role=WindowRole.VERIFICATION,
    ),
)


# ── the holding-period arithmetic ────────────────────────────────────────────────────────────────


class HoldingPeriodMath:
    """One arm's turnover priced against the modelled round trip, on one window and one floor.

    The question M10.7 posed and this axis exists to answer: a book that rotates faster pays the
    round trip more often, and the composite's excess accrues per *week held* rather than per trade,
    so somewhere on the cadence axis the two cross. These are the terms of that comparison.

    * ``turns_per_year`` — closed position round trips per year. Realised, not modelled: a name
      still held at the terminal has no holding period yet and is not counted.
    * ``book_turns_per_year`` — ``turns_per_year`` divided by the basket size, so one unit is the
      whole book rotating once. This is the denominator excess has to be divided by, because a
      single position round trip moves only ``1/top_n`` of the capital.
    * ``net_excess_per_book_turn`` — the window's annualised excess over the benchmark divided by
      ``book_turns_per_year``: what one full rotation earned above the market, *net* of every cost
      the replay charged. **This is the figure that goes negative**, and the fast arms are where.
    * ``gross_excess_per_book_turn`` — the same figure with the modelled round trip added back, so
      it can be read against ``MODELLED_ROUND_TRIP`` directly.
    * ``realised_cost_per_trip`` — total charges divided by round trips, in rupees. The measured
      counterpart to the 0.45 % model, and the reason to distrust it when the two disagree.

    Assumes the arm produced a result; a failed or unmeasured arm has no arithmetic and is
    represented by ``None`` rather than by zeros that would read as measurements.
    """

    __slots__ = (
        "book_turns_per_year",
        "gross_excess_per_book_turn",
        "label",
        "median_hold_days",
        "net_excess_per_book_turn",
        "realised_cost_per_trip",
        "round_trips",
        "top_n",
        "turns_per_year",
    )

    def __init__(
        self,
        *,
        label: str,
        top_n: int,
        round_trips: int,
        median_hold_days: int,
        turns_per_year: Decimal,
        book_turns_per_year: Decimal,
        net_excess_per_book_turn: Decimal,
        gross_excess_per_book_turn: Decimal,
        realised_cost_per_trip: Decimal,
    ) -> None:
        self.label = label
        self.top_n = top_n
        self.round_trips = round_trips
        self.median_hold_days = median_hold_days
        self.turns_per_year = turns_per_year
        self.book_turns_per_year = book_turns_per_year
        self.net_excess_per_book_turn = net_excess_per_book_turn
        self.gross_excess_per_book_turn = gross_excess_per_book_turn
        self.realised_cost_per_trip = realised_cost_per_trip


def _top_n(arm: Arm) -> int:
    """The basket size this arm holds, whichever policy it drives."""
    if arm.swing is not None:
        return arm.swing.top_n
    if arm.naive is not None:
        return arm.naive.top_n
    assert arm.v2 is not None
    return arm.v2.top_n


def _duration_of(arm: Arm) -> str:
    """The arm's whole holding-period machinery in words, or ``—`` for a policy that states none.

    All three knobs, not just the cadence: two arms on the same cadence and re-underwrite but
    different sell bands hold for different lengths, and a column that showed them as identical
    would make the band rows unreadable in a table that exists to compare durations.
    """
    if arm.swing is None:
        return "—"
    swing = arm.swing
    cadence = {5: "weekly", 10: "fortnightly", 21: "monthly", 63: "quarterly"}.get(
        swing.rebalance_interval_sessions,
        f"every {swing.rebalance_interval_sessions} sessions",
    )
    band = (Decimal(swing.sell_band) / Decimal(swing.top_n)).normalize()
    return f"{cadence} / {swing.max_hold_sessions}-session re-underwrite / {band}x band"


def holding_period_math(row: SweepRow, *, years: Decimal) -> HoldingPeriodMath | None:
    """Price ``row``'s realised turnover against the modelled round trip, or ``None`` (M12.3).

    Assumes ``years`` is the window's own span — never a blend of windows, which is why it is passed
    in rather than derived from a pooled result. Returns ``None`` for an arm that failed, closed no
    round trip, or ran on a window of no length: an undefined ratio is not a measurement, and
    printing a zero in its place is how a report states a finding it does not have.
    """
    if not row.ok or row.round_trips <= 0 or years <= _ZERO:
        return None
    assert row.run is not None
    top_n = _top_n(row.arm)
    turns_per_year = Decimal(row.round_trips) / years
    book_turns_per_year = turns_per_year / Decimal(top_n)
    if book_turns_per_year <= _ZERO:
        return None
    net = row.excess / book_turns_per_year
    return HoldingPeriodMath(
        label=row.arm.label,
        top_n=top_n,
        round_trips=row.round_trips,
        median_hold_days=row.median_hold_days,
        turns_per_year=turns_per_year,
        book_turns_per_year=book_turns_per_year,
        net_excess_per_book_turn=net,
        gross_excess_per_book_turn=net + MODELLED_ROUND_TRIP,
        realised_cost_per_trip=row.run.total_charges / Decimal(row.round_trips),
    )


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value:.2%}"


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.0f}"


def _floor_label(floor: Decimal) -> str:
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


def _years(result: SweepResult) -> Decimal:
    return Decimal((result.terminal - result.start).days) / _DAYS_PER_YEAR


def _ranked_table(result: SweepResult, floor: Decimal) -> list[str]:
    """One window, one floor: every arm ranked on XIRR / max drawdown, failures kept."""
    lines = [
        "| # | Strategy | Duration | XIRR | Max DD | **XIRR/DD** | Round trips | Median hold | "
        "Cost | Excess |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(result.ranked(floor), start=1):
        duration = _duration_of(row.arm)
        if not row.ok:
            lines.append(
                f"| — | {row.arm.label} | {duration} | **failed** | — | — | — | — | — | "
                f"{row.error} |"
            )
            continue
        assert row.run is not None
        lines.append(
            f"| {position} | {row.arm.label} | {duration} | {_pct(row.xirr)} | "
            f"{_pct(row.max_drawdown)} | **{row.return_per_drawdown:.2f}** | {row.round_trips} | "
            f"{row.median_hold_days}d | {_rupees(row.run.total_charges)} | {_pct(row.excess)} |"
        )
    return lines


def _arithmetic_table(result: SweepResult, floor: Decimal) -> list[str]:
    """The holding-period arithmetic for one window and one floor, negatives included."""
    years = _years(result)
    lines = [
        "| Strategy | Duration | Median hold | Trips/yr | Book turns/yr | "
        "Gross excess / book turn | Modelled round trip | **Net / book turn** | Realised ₹/trip |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result.ranked(floor):
        math = holding_period_math(row, years=years)
        if math is None:
            reason = row.error if not row.ok else "no closed round trip in the window"
            lines.append(
                f"| {row.arm.label} | {_duration_of(row.arm)} | — | — | — | — | "
                f"{_pct(MODELLED_ROUND_TRIP)} | — | {reason} |"
            )
            continue
        net = math.net_excess_per_book_turn
        marker = f"**{_pct(net)}**" if net < _ZERO else _pct(net)
        lines.append(
            f"| {row.arm.label} | {_duration_of(row.arm)} | {math.median_hold_days}d | "
            f"{math.turns_per_year:.0f} | {math.book_turns_per_year:.1f} | "
            f"{_pct(math.gross_excess_per_book_turn)} | {_pct(MODELLED_ROUND_TRIP)} | {marker} | "
            f"{_rupees(math.realised_cost_per_trip)} |"
        )
    return lines


def _window_section(entry: WindowSweep, floors: Sequence[Decimal]) -> list[str]:
    """Everything about one window, in one place — so nothing invites reading across two."""
    result, window = entry.result, entry.window
    best = next((row for row in result.ranked(floors[0]) if row.ok), None)
    universe = best.run.mean_universe if best is not None and best.run is not None else _ZERO
    lines = [
        f"## Window — {window.label}: {result.start.isoformat()} → {result.terminal.isoformat()}",
        "",
        f"- Role: **{window.role.value}** (requested {window.start.isoformat()} → "
        f"{window.end.isoformat()})",
        f"- {result.sessions} sessions, {_years(result):.2f} years",
        f"- Benchmark: **{_pct(result.benchmark_xirr)}** ({result.benchmark_name}) on identical "
        "cashflows",
        f"- Mean investable universe per decision: {universe}",
        f"- One windowed lake pass over **{result.feature_dates}** decision dates, built in "
        f"{result.lake_seconds:.0f}s and shared by every arm on this window",
        f"- {len(result.rows)} arm-runs in {result.total_seconds / 60:.0f} min",
        "",
    ]
    for floor in floors:
        lines += [
            f"### Ranked — {_floor_label(floor)} liquidity floor",
            "",
            *_ranked_table(result, floor),
            "",
            f"#### Holding-period arithmetic — {_floor_label(floor)}",
            "",
            *_arithmetic_table(result, floor),
            "",
        ]
    return lines


def _walk_forward_section(sweep: MultiWindowSweep, floor: Decimal) -> list[str]:
    """The selection winner, named from the selection window alone, and both ranks side by side."""
    selection = sweep.with_role(WindowRole.SELECTION)
    verification = sweep.with_role(WindowRole.VERIFICATION)
    if selection is None or verification is None:
        return []
    chosen = sweep.selected
    lines = [
        "## Walk-forward: what survives being chosen",
        "",
        f"- Selection window: **{selection.result.start.isoformat()} → "
        f"{selection.result.terminal.isoformat()}**",
        f"- Verification window: **{verification.result.start.isoformat()} → "
        f"{verification.result.terminal.isoformat()}**",
        f"- Chosen on the selection window's ranking at the {_floor_label(floor)} floor, before "
        "any verification figure was read",
        "",
        f"**Chosen: {chosen or '(no arm produced a result)'}**",
        "",
    ]
    picked = row_of(selection.result, chosen, floor)
    verified = row_of(verification.result, chosen, floor)
    if picked is not None and picked.ok:
        lines += [
            f"On the selection window it earned {_pct(picked.xirr)} against a "
            f"{_pct(picked.max_drawdown)} drawdown ({picked.return_per_drawdown:.2f}), at "
            f"{_duration_of(picked.arm)}.",
            "",
        ]
    if verified is not None and verified.ok:
        lines += [
            f"On the verification window — unseen when it was chosen — it earned "
            f"**{_pct(verified.xirr)}** against a {_pct(verified.max_drawdown)} drawdown "
            f"({verified.return_per_drawdown:.2f}), ranking "
            f"**{rank_of(verification.result, chosen, floor)}** of "
            f"{len(verification.result.ranked(floor))}.",
            "",
        ]
    else:
        lines += ["It produced no result on the verification window.", ""]

    lines += [
        "### Selection rank against verification rank",
        "",
        "| Strategy | Duration | Selection rank | Selection XIRR/DD | Verification rank | "
        "Verification XIRR/DD | Verification XIRR |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(selection.result.ranked(floor), start=1):
        label = row.arm.label
        marker = " ←" if label == chosen else ""
        other = row_of(verification.result, label, floor)
        if other is None or not other.ok:
            lines.append(
                f"| {label}{marker} | {_duration_of(row.arm)} | {position} | "
                f"{row.return_per_drawdown:.2f} | — | — | — |"
            )
            continue
        lines.append(
            f"| {label}{marker} | {_duration_of(row.arm)} | {position} | "
            f"{row.return_per_drawdown:.2f} | {rank_of(verification.result, label, floor)} | "
            f"{other.return_per_drawdown:.2f} | {_pct(other.xirr)} |"
        )
    return [*lines, ""]


def _bar_section(sweep: MultiWindowSweep, floors: Sequence[Decimal]) -> list[str]:
    """Whether 25 % was cleared — with the window, the floor, the drawdown and the duration."""
    lines = [
        f"## The bar: was {_pct(BAR)} XIRR reached, and on what",
        "",
    ]
    cleared: list[tuple[str, Decimal, SweepRow]] = []
    for entry in sweep.windows:
        result = entry.result
        hits = [
            (entry.window.label, floor, row)
            for floor in floors
            for row in result.ranked(floor)
            if row.ok and row.xirr > BAR
        ]
        cleared += hits
        if hits:
            for label, floor, row in hits:
                lines.append(
                    f"- **{label}** ({result.start} → {result.terminal}), "
                    f"{_floor_label(floor)}: **{row.arm.label}** at {_pct(row.xirr)} XIRR, "
                    f"{_pct(row.max_drawdown)} max drawdown, "
                    f"{row.return_per_drawdown:.2f} return per drawdown, at "
                    f"{_duration_of(row.arm)}."
                )
            continue
        best = next((row for row in result.ranked(floors[0]) if row.ok), None)
        top = (
            f"the best was {_pct(best.xirr)} ({best.arm.label}, {_duration_of(best.arm)})"
            if best is not None
            else "no arm produced a result"
        )
        lines.append(
            f"- **{entry.window.label}** ({result.start} → {result.terminal}): "
            f"no arm cleared {_pct(BAR)} on either floor — {top}."
        )
    lines += ["", _bar_verdict(cleared, floors), ""]
    return lines


def _bar_verdict(
    cleared: Sequence[tuple[str, Decimal, SweepRow]], floors: Sequence[Decimal]
) -> str:
    """The one sentence a reader who reads nothing else should get."""
    if not cleared:
        return (
            f"**Answer: no.** No duration of the swing composite cleared a {_pct(BAR)} XIRR on any "
            "of these windows at either liquidity floor. The honest reading is the ranking and the "
            "holding-period arithmetic above, not a number that was not reached."
        )
    reachable = [hit for hit in cleared if hit[1] == max(floors)]
    if not reachable:
        return (
            f"**Answer: only on the discovery floor.** Every arm that cleared {_pct(BAR)} did so "
            f"at the {_floor_label(min(floors))} floor and none did at "
            f"{_floor_label(max(floors))}, where the fill model's slippage is defensible for a "
            "real book. That is a finding about the thinness of the names, not a strategy that "
            "clears the bar."
        )
    return (
        f"**Answer: the bar was cleared** — including at the {_floor_label(max(floors))} floor. "
        "Read each line above with its window, its floor, its drawdown and its duration attached; "
        "those are conditions, not footnotes, and a bar cleared on one window of four is one "
        "window of four."
    )


def _digest_section(sweep: MultiWindowSweep, floors: Sequence[Decimal]) -> list[str]:
    """Every run's digest, so the whole campaign is reproducible arm by arm (determinism)."""
    lines = [
        "## Run digests (determinism)",
        "",
        "*sha256 of journal + book. Same inputs → byte-identical journal and book; a digest that "
        "moves without an input moving is a defect, not noise.*",
        "",
    ]
    for entry in sweep.windows:
        lines += [f"### {entry.window.label}", ""]
        for floor in floors:
            lines.append(f"**{_floor_label(floor)}**")
            lines.append("")
            for row in entry.result.ranked(floor):
                if not row.ok or row.run is None:
                    lines.append(f"- **{row.arm.label}:** failed — {row.error}")
                    continue
                lines.append(f"- **{row.arm.label}:** `{row.run.result.digest()}`")
            lines.append("")
    return lines


def render_duration_report(sweep: MultiWindowSweep, *, floors: Sequence[Decimal]) -> str:
    """The M12.3 markdown: every window on its own, then what the whole thing can be asked to prove.

    Assumes ``sweep`` carries every window the campaign ran. Never averages a figure across windows
    and never prints a combined ranking — each window's section is self-contained by construction,
    so there is no cross-window number for a reader to mistake for a summary.
    """
    low = floors[0]
    lines = [
        "# M12.3 — The swing composite in multiple durations and multiple windows",
        "",
        "*Generated by `python -m backtest.duration --report`. The M10.7 composite's own three "
        "legs (52-week-high proximity, delivery share, 12-1 momentum) held at a grid of cadences "
        "and re-underwrite horizons, run over four stated windows. Ranked on XIRR divided by max "
        "drawdown — an owner decision (2026-09-07), because ranked on return alone the winner is "
        "whichever arm carried the most risk.*",
        "",
        "## What was run, and what is deliberately absent",
        "",
        f"- **{len(DURATION_ARMS)} arms**: the M10.7 reference, {len(DURATION_ARMS) - 3} duration "
        "and band variations of it, and the two momentum baselines every row is priced against.",
        "- **Every arm scores on exactly M10.7's three legs.** Every M12.1 leg is at zero, the "
        "trailing stop, the volatility screen and the basket size are at their defaults. A row is "
        "the price of the holding-period machinery and of nothing else.",
        f"- **Both liquidity floors, every arm, every window**: {_floor_label(min(floors))} (the "
        f"inherited M9.3 discovery floor) and {_floor_label(max(floors))} (what a real book could "
        "reach).",
        "- **Signal off L2 back-adjusted closes; execution, sizing and marks off raw** "
        "(invariant #3). Every read goes through the point-in-time guard (invariant #7).",
        "- **No figure below is averaged across windows, and there is no combined ranking.** The "
        "same policies earn ~22 % over six years and 12-15 % over ten; a blended number would be "
        "the six-year window's optimism wearing the decade's authority. Each window is reported "
        "on its own, with its own benchmark.",
        "- **A failed arm keeps its row** with the error in it. Silently shrinking a table is how "
        "a sweep reports a survivor bias it created.",
        "",
        "### The windows",
        "",
        "| Window | Role | Span | Sessions | Benchmark XIRR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in sweep.windows:
        lines.append(
            f"| {entry.window.label} | {entry.window.role.value} | "
            f"{entry.result.start.isoformat()} → {entry.result.terminal.isoformat()} | "
            f"{entry.result.sessions} | {_pct(entry.result.benchmark_xirr)} |"
        )
    lines.append("")

    for entry in sweep.windows:
        lines += _window_section(entry, floors)

    lines += _walk_forward_section(sweep, low)
    lines += _bar_section(sweep, floors)

    lines += [
        "## What each arm changed",
        "",
        "*Each row differs from its named reference by exactly the change stated here. The chain "
        "runs through intermediate arms rather than pretending a grid cell is one step from the "
        "default: `weekly / 10` differs from `weekly / 21`, which differs from `fortnightly / 21`, "
        "which differs from the M10.7 default.*",
        "",
        "| Strategy | Duration | Differs from | By |",
        "| --- | --- | --- | --- |",
    ]
    for arm in DURATION_ARMS:
        lines.append(f"| {arm.label} | {_duration_of(arm)} | {arm.reference} | {arm.note} |")

    lines += [
        "",
        "## Honest limits of this measurement",
        "",
        "- **Four windows are four draws, not a distribution.** The walk-forward has one split, so "
        "it says whether the selected duration held up across a single boundary in 2021 — a "
        "boundary that happens to sit just after the sharpest drawdown and just before the "
        "sharpest recovery in the lake. It does not say the duration holds up across boundaries "
        "in general.",
        "- **The decade window contains the six-year window.** They are not independent evidence. "
        "An arm that clears the bar on the six-year window and not the decade has told you when "
        "its edge was, not that it has one.",
        "- **The duration axis is still a search.** Ten arms over four windows is forty numbers, "
        "and the best of forty is flattered by having been the best of forty. The walk-forward "
        "columns are the only out-of-sample figures in this report; every other cell is in-sample "
        "by construction.",
        "- **Two arms re-underwrite at 126 sessions, outside M10.7's stated 7-90 day band.** They "
        "are here because an axis that stops at its own assumption cannot test the assumption. If "
        "one of them wins, the finding is that the band was too narrow, not that the band was "
        "obeyed.",
        f"- **The {_pct(MODELLED_ROUND_TRIP)} round trip is a model.** 0.223 % of it is statutory "
        "and known (`execution/costs/rates.yaml`); the rest is modelled slippage, and at the "
        f"{_floor_label(min(floors))} floor the median name a basket picks trades a few crore a "
        "day, where that model is a claim rather than a measurement. The realised ₹/trip column "
        "is the measured counterpart; where the two disagree, believe neither and raise the floor.",
        "- **Excess is against a price-return L1 proxy** (M9.4), not a licensed total-return "
        "index, so every excess figure overstates by roughly the market's dividend yield. It is "
        "consistent across arms and windows, so the *relative* standing survives it; the absolute "
        "excess does not.",
        "- **Return per drawdown is a ratio of two noisy numbers.** Max drawdown is one worst "
        "path, not a distribution. Two arms within a few hundredths are not distinguishable on "
        "this evidence, and reading the duration grid as a smooth surface with a peak is reading "
        "sampling noise as structure.",
        "- **Turnover is realised round trips, not journal entries.** A name still held at the "
        "terminal has no holding period yet and is excluded, and an order that never filled is "
        "not a trade. The slower arms therefore carry less of the fill model's error than the "
        "faster ones — a reason to prefer them at equal measured return.",
        "",
    ]
    lines += _digest_section(sweep, floors)
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.duration",
        description="Run the M10.7 duration grid over every mandated window and report (M12.3).",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const=str(_REPORT_PATH),
        default=None,
        help=f"write the markdown report (default path {_REPORT_PATH})",
    )
    parser.add_argument(
        "--windows",
        default=None,
        help="comma-separated window labels to run, or `none` for only the --window ones; default "
        "is all four mandated windows. For smoke-testing the wiring, never for reporting a "
        "subset as the campaign",
    )
    parser.add_argument(
        "--window",
        action="append",
        default=None,
        metavar="LABEL:START:END[:ROLE]",
        help="an extra ad-hoc window, repeatable. For validating on a short span without touching "
        "the mandated list",
    )
    parser.add_argument(
        "--arms",
        default=None,
        help="comma-separated substrings; only arms whose label matches one are run. For "
        "smoke-testing the wiring, never for reporting a subset as the grid",
    )
    parser.add_argument(
        "--floors",
        default="low,high",
        help="which liquidity floors to run: low (₹1cr), high (₹10cr), or both (default)",
    )
    parser.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def _ad_hoc(spec: str) -> Window:
    """``LABEL:START:END[:ROLE]`` as a window. Raises ``ValueError`` on anything else.

    The optional role exists so the selection/verification path can be smoke-tested on a short
    span. It buys nothing for a real campaign — the mandated windows already carry their roles.
    """
    parts = spec.split(":")
    if len(parts) not in (3, 4) or not all(part.strip() for part in parts):
        raise ValueError(f"a window is LABEL:START:END[:ROLE], got {spec!r}")
    label, start, end = parts[0], parts[1], parts[2]
    try:
        role = WindowRole(parts[3]) if len(parts) == 4 else WindowRole.STANDALONE
    except ValueError:
        roles = ", ".join(member.value for member in WindowRole)
        raise ValueError(f"unknown window role {parts[3]!r}; use one of {roles}") from None
    return Window(
        label=label, start=date.fromisoformat(start), end=date.fromisoformat(end), role=role
    )


def _select_windows(args: argparse.Namespace) -> tuple[Window, ...]:
    """The windows this invocation runs: the mandated list, filtered, plus any ad-hoc ones."""
    windows = MANDATED_WINDOWS
    if args.windows == "none":
        windows = ()
    elif args.windows:
        wanted = [part.strip().lower() for part in args.windows.split(",") if part.strip()]
        windows = tuple(w for w in windows if any(p in w.label.lower() for p in wanted))
        if not windows and not args.window:
            raise ValueError(f"no mandated window matches {args.windows}")
    extra = tuple(_ad_hoc(spec) for spec in (args.window or []))
    return windows + extra


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.duration``. Returns a process exit code."""
    args = _parse_args(argv)

    names = {"low": LOW_FLOOR, "high": HIGH_FLOOR}
    try:
        floors = tuple(names[part.strip()] for part in args.floors.split(","))
    except KeyError as error:
        print(f"error: unknown floor {error}; use low, high or low,high", file=sys.stderr)
        return 2

    arms = DURATION_ARMS
    if args.arms:
        wanted = [part.strip().lower() for part in args.arms.split(",") if part.strip()]
        arms = tuple(a for a in DURATION_ARMS if any(w in a.label.lower() for w in wanted))
        if not arms:
            print(f"error: no arm matches {args.arms}", file=sys.stderr)
            return 2

    try:
        windows = _select_windows(args)
        sweep = run_multi_window_sweep(
            windows=windows, arms=arms, floors=floors, data_root=args.data_root
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for entry in sweep.windows:
        print(f"\n  {entry.window.label}: {entry.result.start} → {entry.result.terminal}")
        for floor in floors:
            print(f"    {_floor_label(floor)}:")
            for position, row in enumerate(entry.result.ranked(floor), start=1):
                if not row.ok:
                    print(f"      --  {row.arm.label}: FAILED — {row.error}")
                    continue
                print(
                    f"      {position:>2}. {row.arm.label:<40} XIRR {_pct(row.xirr):>8}  "
                    f"DD {_pct(row.max_drawdown):>7}  ratio {row.return_per_drawdown:>5.2f}  "
                    f"trips {row.round_trips:>5}"
                )
    if sweep.selected:
        print(f"\n  selected on the selection window alone: {sweep.selected}")

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_duration_report(sweep, floors=floors), encoding="utf-8")
        print(f"\n  report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
