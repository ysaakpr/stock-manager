"""M12.3 — walk-forward the sweep's finalists, and answer the >25 % question honestly (X2).

A sweep of twenty-odd arms over one window *selects* on that window, so the arm that wins it is
partly the arm that window flattered. Everything in :mod:`backtest.sweep` is therefore in-sample by
construction, and this module is the out-of-sample check that says how much of the ranking survives.

**The order of operations is the method, and it is enforced rather than described.**
:func:`run_walk_forward` runs the sweep on the *selection* window, freezes the winner and the whole
selection ranking, and only then runs the verification window. The frozen name is carried in
:attr:`WalkForward.selected` and rendered before any verification figure, so a reader can see the
choice that was actually made rather than a choice made with the answer in hand. Re-ranking after
seeing the verification window would be the entire error this task exists to avoid.

**Three windows, and none of them averaged.** The decade, the six-year window the repo's existing
~23 % figures came from, and the split. A figure averaged across windows would hide the one fact
`ops/gates/algo-reevaluation-2026-09-07.md` established: the same policies earn 22 % over six years
and 12-15 % over ten, and the six-year window is the flattering one. Each window is reported on its
own, with its own benchmark.

**The verdict states the bar's answer with its conditions attached.** ">25 %" is not a property of a
strategy; it is a property of a strategy, a window, a liquidity floor and a drawdown together. An
arm that clears the bar only on the six-year window, or only on the ₹1 crore floor where the fill
model's slippage is a claim, has not cleared it, and the report says which of those it is instead of
printing the number.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    SweepRow,
    rank_of,
    row_of,
    run_sweep,
)
from dataplatform.logging import get_logger

__all__ = [
    "BAR",
    "WalkForward",
    "render_verdict",
    "run_walk_forward",
]

_LOG = get_logger(__name__)
_ZERO = Decimal("0")

#: The owner's stated bar for this search (2026-09-07): a backtested XIRR above this, net of costs.
BAR = Decimal("0.25")

_VERDICT_PATH = Path("ops/gates/M12-strategy-verdict.md")


@dataclass(slots=True)
class WalkForward:
    """A selection window's ranking, the name frozen from it, and the verification window's."""

    selection: SweepResult
    verification: SweepResult
    #: The arm chosen on the selection window alone — set before verification is run.
    selected: str = ""
    #: Every other window the verdict reports, keyed by a label like "decade" or "six-year".
    context: dict[str, SweepResult] = field(default_factory=dict)

    def rank_of(self, result: SweepResult, label: str, floor: Decimal) -> int | None:
        """Where ``label`` placed in ``result`` on ``floor``, or ``None`` if it has no row."""
        return rank_of(result, label, floor)

    def row_of(self, result: SweepResult, label: str, floor: Decimal) -> SweepRow | None:
        """``label``'s row in ``result`` on ``floor``, or ``None`` if it has none."""
        return row_of(result, label, floor)


def run_walk_forward(
    *,
    selection: tuple[date, date],
    verification: tuple[date, date],
    arms: Sequence[Arm] = ARMS,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
    data_root: Path | None = None,
) -> WalkForward:
    """Sweep the selection window, freeze its winner, then sweep the verification window (M12.3).

    Assumes ``selection`` ends before ``verification`` begins; an overlap would leak the answer into
    the choice. Never re-ranks the selection window after the verification figures exist.
    """
    if selection[1] >= verification[0]:
        raise ValueError(
            f"the selection window must close before verification opens: "
            f"{selection[1].isoformat()} is not before {verification[0].isoformat()}"
        )
    chosen_on = run_sweep(
        start=selection[0], end=selection[1], arms=arms, floors=floors, data_root=data_root
    )
    ranked = chosen_on.ranked(floors[0])
    winner = next((row.arm.label for row in ranked if row.ok), "")
    _LOG.info(
        "walkforward.selected",
        arm=winner,
        window=f"{selection[0].isoformat()}..{selection[1].isoformat()}",
    )
    # The name is frozen above this line. Nothing below may change it.
    verified_on = run_sweep(
        start=verification[0], end=verification[1], arms=arms, floors=floors, data_root=data_root
    )
    return WalkForward(selection=chosen_on, verification=verified_on, selected=winner)


# ── the verdict ──────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value:.2%}"


def _floor_label(floor: Decimal) -> str:
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


def _clears(row: SweepRow | None) -> bool:
    return row is not None and row.ok and row.xirr > BAR


def _bar_rows(
    result: SweepResult, floors: Sequence[Decimal]
) -> list[tuple[str, Decimal, SweepRow]]:
    """Every (floor label, floor, row) in ``result`` whose XIRR clears the bar."""
    out: list[tuple[str, Decimal, SweepRow]] = []
    for floor in floors:
        out += [(_floor_label(floor), floor, row) for row in result.ranked(floor) if _clears(row)]
    return out


def render_verdict(
    walk: WalkForward,
    *,
    floors: Sequence[Decimal],
    selection_window: tuple[date, date],
    verification_window: tuple[date, date],
) -> str:
    """The M12.3 markdown: the frozen choice, its decay, the other windows, and the bar's answer."""
    low = floors[0]
    lines = [
        "# M12.3 — The verdict: what survives being chosen, and whether 25 % was reached",
        "",
        "*Generated by `python -m backtest.verdict --report`. The selection window's winner is "
        "named before any verification figure is read; both ranks are printed so the decay from "
        "selection to verification is visible rather than inferred. No figure here is averaged "
        "across windows.*",
        "",
        "## The choice, made on the selection window alone",
        "",
        f"- Selection window: **{selection_window[0].isoformat()} → "
        f"{selection_window[1].isoformat()}**",
        f"- Verification window: **{verification_window[0].isoformat()} → "
        f"{verification_window[1].isoformat()}**",
        f"- Ranked on XIRR / max drawdown at the {_floor_label(low)} floor",
        "",
        f"**Chosen: {walk.selected or '(no arm produced a result)'}**",
        "",
    ]

    chosen_selection = walk.row_of(walk.selection, walk.selected, low)
    chosen_verification = walk.row_of(walk.verification, walk.selected, low)
    if chosen_selection is not None and chosen_selection.ok:
        lines += [
            f"On the selection window it earned {_pct(chosen_selection.xirr)} against a "
            f"{_pct(chosen_selection.max_drawdown)} drawdown "
            f"({chosen_selection.return_per_drawdown:.2f}).",
            "",
        ]
    if chosen_verification is not None and chosen_verification.ok:
        rank = walk.rank_of(walk.verification, walk.selected, low)
        lines += [
            f"On the verification window — never seen when it was chosen — it earned "
            f"**{_pct(chosen_verification.xirr)}** against a "
            f"{_pct(chosen_verification.max_drawdown)} drawdown "
            f"({chosen_verification.return_per_drawdown:.2f}), ranking **{rank}** of "
            f"{len(walk.verification.ranked(low))}.",
            "",
        ]
    else:
        lines += ["It produced no result on the verification window.", ""]

    lines += [
        "## Selection rank against verification rank",
        "",
        "| Strategy | Selection rank | Selection XIRR/DD | Verification rank | "
        "Verification XIRR/DD | Verification XIRR |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(walk.selection.ranked(low), start=1):
        label = row.arm.label
        verified = walk.row_of(walk.verification, label, low)
        vrank = walk.rank_of(walk.verification, label, low)
        marker = " ←" if label == walk.selected else ""
        if verified is None or not verified.ok:
            lines.append(
                f"| {label}{marker} | {position} | {row.return_per_drawdown:.2f} | — | — | — |"
            )
            continue
        lines.append(
            f"| {label}{marker} | {position} | {row.return_per_drawdown:.2f} | {vrank} | "
            f"{verified.return_per_drawdown:.2f} | {_pct(verified.xirr)} |"
        )

    lines += ["", "## The bar: was 25 % reached, and on what", ""]
    windows: list[tuple[str, SweepResult]] = [
        ("selection", walk.selection),
        ("verification", walk.verification),
        *walk.context.items(),
    ]
    any_cleared = False
    for name, result in windows:
        cleared = _bar_rows(result, floors)
        if not cleared:
            best = next((row for row in result.ranked(floors[0]) if row.ok), None)
            top = f"best {_pct(best.xirr)} ({best.arm.label})" if best is not None else "no result"
            lines.append(
                f"- **{name}** ({result.start} → {result.terminal}): no arm cleared 25 % — {top}."
            )
            continue
        any_cleared = True
        for floor_label, _floor, row in cleared:
            lines.append(
                f"- **{name}** ({result.start} → {result.terminal}), {floor_label}: "
                f"**{row.arm.label}** at {_pct(row.xirr)} XIRR, "
                f"{_pct(row.max_drawdown)} max drawdown, "
                f"{row.return_per_drawdown:.2f} return per drawdown."
            )
    lines += [
        "",
        (
            "**Answer: the bar was cleared** — but read each line above with its window, its floor "
            "and its drawdown attached; those are conditions, not footnotes."
            if any_cleared
            else "**Answer: no.** No arm in this sweep cleared a 25 % XIRR on any window at any "
            "liquidity floor. The honest reading is the ranking, not a number that was not reached."
        ),
        "",
        "## What this verdict cannot be asked to prove",
        "",
        "- **A walk-forward with one split is one draw.** It says the selected arm did or did not "
        "hold up across a single boundary, not that it holds up across boundaries in general.",
        "- **Both windows share a lake, a cost model and a fill model.** An error common to both — "
        "the 10 bp base slippage at the low liquidity floor, the price-return benchmark proxy — "
        "moves selection and verification together and is invisible to this comparison.",
        "- **Survivorship in the universe is handled; survivorship in the *signals* is not.** "
        "The legs swept here are the ones this repo built because earlier work suggested they "
        "worked, which is a selection effect no split inside this lake can undo.",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.verdict",
        description="Walk-forward the strategy sweep and write the verdict (M12.3).",
    )
    parser.add_argument("--selection", default="2016-09-01:2021-08-31", help="START:END")
    parser.add_argument("--verification", default="2021-09-01:2026-08-31", help="START:END")
    parser.add_argument(
        "--report",
        nargs="?",
        const=str(_VERDICT_PATH),
        default=None,
        help=f"write the verdict markdown (default {_VERDICT_PATH})",
    )
    parser.add_argument("--floors", default="low,high")
    parser.add_argument("--arms", default=None, help="substring filter, for smoke-testing only")
    parser.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def _window(spec: str) -> tuple[date, date]:
    start, _, end = spec.partition(":")
    return date.fromisoformat(start), date.fromisoformat(end)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.verdict``. Returns a process exit code."""
    args = _parse_args(argv)
    try:
        selection, verification = _window(args.selection), _window(args.verification)
    except ValueError as error:
        print(f"error: bad window: {error}", file=sys.stderr)
        return 2

    names = {"low": LOW_FLOOR, "high": HIGH_FLOOR}
    try:
        floors = tuple(names[part.strip()] for part in args.floors.split(","))
    except KeyError as error:
        print(f"error: unknown floor {error}; use low, high or low,high", file=sys.stderr)
        return 2

    arms = ARMS
    if args.arms:
        wanted = [p.strip().lower() for p in args.arms.split(",") if p.strip()]
        arms = tuple(a for a in ARMS if any(w in a.label.lower() for w in wanted))

    try:
        walk = run_walk_forward(
            selection=selection,
            verification=verification,
            arms=arms,
            floors=floors,
            data_root=args.data_root,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(f"\n  selected on {selection[0]}..{selection[1]}: {walk.selected}")
    verified = walk.row_of(walk.verification, walk.selected, floors[0])
    if verified is not None and verified.ok:
        print(
            f"  verified on {verification[0]}..{verification[1]}: "
            f"XIRR {_pct(verified.xirr)}, DD {_pct(verified.max_drawdown)}, "
            f"rank {walk.rank_of(walk.verification, walk.selected, floors[0])}"
        )
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_verdict(
                walk,
                floors=floors,
                selection_window=selection,
                verification_window=verification,
            ),
            encoding="utf-8",
        )
        print(f"  verdict written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
