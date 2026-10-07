"""M12.R — the M12 strategy review re-run on today's lake and engine; resumable, two workers max.

The M12 reports (``ops/gates/M12-strategy-sweep-*.md``, ``M12-strategy-verdict.md``,
``M12-swing-duration-window-report.md``) were struck on 2026-09-07 against the lake and engine of
that day. Since then L2 reaches 2006, three issuers' pre-event prices are quarantined (D22), the
book takes corporate actions and earns interest on idle cash, every order walks through A8's rails
and the universe is the PIT NIFTY 500 screen. This module re-runs the same arms on the owner's
windows so the old and new tables can be read side by side; it never edits the old reports.

**The arms are the existing ones, unchanged.** The M12.2 sweep's :data:`~backtest.sweep.ARMS`, the
M12.3 duration grid's :data:`~backtest.sweep.DURATION_ARMS` (the three rows it shares with ``ARMS``
run once) and :data:`~backtest.sweep.D13_PAPER_BASELINE`, the configuration paper trading runs.

**The windows are the owner mandate's (2026-09-07), stated here rather than in**
``backtest/windows.yaml``: that file is the after-tax campaign's, whose walk-forward is the
equal-session split of a 2012 span. These are the decade, the six-year window and the 2016-09 /
2021-09 walk-forward the M12 reports used, plus an optional long window that opens once L2's
2006 history fills every lookback — supplementary, and run only when asked.

**The universe is the floor-only screen** (``turnover_floor``), on every window: the PIT NIFTY 500
membership history opens 2016-10-24, after the decade and selection windows open, and the engine
refuses an uncovered date rather than guess. It is also the screen the 2026-09-07 reports ran.

**Resumable, run by run** (``backtest.run_ledger``): a killed driver restarted with the same
command replays only what had not finished. The directory carries a manifest (commit, lake, arms,
windows, switches) and is never resumed by a different one (``backtest.campaign.check_manifest``).

What this module never does: write under the lake, read a wall clock into a result, average a
figure across windows, or re-rank the selection window after the verification window is read.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from backtest.book_actions import (
    BookActionSource,
    corporate_actions_in_force,
    load_store_book_actions,
)
from backtest.campaign import MAX_WORKERS, CampaignError, check_manifest
from backtest.cash_interest import (
    RepoRateSchedule,
    accrue_cash_interest,
    load_repo_rate_schedule,
)
from backtest.run import UNIVERSE_CHOICES, UNIVERSE_TURNOVER_FLOOR, _L1Reader
from backtest.run_ledger import (
    ledger_path,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
)
from backtest.sweep import (
    ARMS,
    D13_PAPER_BASELINE,
    DURATION_ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    run_digests,
    run_sweep,
)
from backtest.verdict import WalkForward, run_walk_forward
from dataplatform.logging import get_logger

__all__ = [
    "LONG",
    "RERUN_ARMS",
    "UNITS",
    "WALK_FORWARD",
    "WINDOWS",
    "RerunPlan",
    "UnitOutcome",
    "build_manifest",
    "load_results",
    "main",
    "missing_runs",
    "rerun_arms",
    "run_rerun",
    "run_unit",
]

_LOG = get_logger(__name__)

FLOORS: tuple[Decimal, ...] = (LOW_FLOOR, HIGH_FLOOR)

WALK_FORWARD = "walk-forward"
LONG = "long"

#: The owner's windows (2026-09-07), as the M12 reports requested them.
WINDOWS: dict[str, tuple[date, date]] = {
    "decade": (date(2016, 9, 1), date(2026, 8, 31)),
    "six-year": (date(2019, 7, 1), date(2026, 8, 31)),
    "wf-selection": (date(2016, 9, 1), date(2021, 8, 31)),
    "wf-verification": (date(2021, 9, 1), date(2026, 8, 31)),
    # Supplementary: L2 reaches 2006 since #74/#75; 2007-01-01 leaves 2006 for the lookbacks.
    LONG: (date(2007, 1, 1), date(2026, 8, 31)),
}

#: Work units, mandate first; ``long`` only on request. The walk-forward is one unit so the
#: selection sweep finishes, and its winner is frozen, before the verification sweep starts.
UNITS: tuple[str, ...] = (WALK_FORWARD, "decade", "six-year", LONG)
_DEFAULT_UNITS: tuple[str, ...] = (WALK_FORWARD, "decade", "six-year")


def rerun_arms() -> tuple[Arm, ...]:
    """``ARMS``, the duration grid's own arms, then the D13 paper baseline — each label once."""
    seen: set[str] = set()
    out: list[Arm] = []
    for arm in (*ARMS, *DURATION_ARMS, D13_PAPER_BASELINE):
        if arm.label not in seen:
            seen.add(arm.label)
            out.append(arm)
    return tuple(out)


RERUN_ARMS: tuple[Arm, ...] = rerun_arms()


@dataclass(frozen=True, slots=True)
class RerunPlan:
    """What one re-run directory measures, under which switches, and where it keeps it."""

    out_dir: Path
    data_root: Path | None
    units: tuple[str, ...] = _DEFAULT_UNITS
    arms: tuple[Arm, ...] = RERUN_ARMS
    floors: tuple[Decimal, ...] = FLOORS
    #: Today's engine defaults; switched off only for an attribution run.
    book_actions: bool = True
    cash_interest: bool = True
    #: Not the engine's ``nifty500`` default: its PIT membership history opens 2016-10-24, and the
    #: mandate's decade and selection windows open 2016-09-01, where that screen raises rather than
    #: answer. The floor-only screen is also what the 2026-09-07 reports ran, so rows compare.
    universe: str = UNIVERSE_TURNOVER_FLOOR

    def __post_init__(self) -> None:
        unknown = [u for u in self.units if u not in UNITS]
        if unknown:
            raise CampaignError(f"unknown units {unknown}; one of {', '.join(UNITS)}")
        if self.universe not in UNIVERSE_CHOICES:
            raise CampaignError(f"unknown universe {self.universe!r}")


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    """What one unit did: runs replayed, loaded from disk, failed."""

    unit: str
    replayed: int
    resumed: int
    failed: int


def _interest(plan: RerunPlan) -> RepoRateSchedule | None:
    return load_repo_rate_schedule() if plan.cash_interest else None


def _actions(plan: RerunPlan) -> BookActionSource | None:
    # data_root passed explicitly: the configured default is the checkout's data/, which in a
    # worktree does not exist and would read as "no listing windows" rather than failing.
    return load_store_book_actions(data_root=plan.data_root) if plan.book_actions else None


def _sweep(plan: RerunPlan, name: str, floors: Sequence[Decimal] | None = None) -> SweepResult:
    start, end = WINDOWS[name]
    return run_sweep(
        start=start,
        end=end,
        arms=plan.arms,
        floors=plan.floors if floors is None else floors,
        data_root=plan.data_root,
        universe_name=plan.universe,
    )


def _walk(plan: RerunPlan, floors: Sequence[Decimal] | None = None) -> WalkForward:
    return run_walk_forward(
        selection=WINDOWS["wf-selection"],
        verification=WINDOWS["wf-verification"],
        arms=plan.arms,
        floors=plan.floors if floors is None else floors,
        data_root=plan.data_root,
        universe_name=plan.universe,
    )


def run_unit(plan: RerunPlan, name: str, floor: Decimal | None = None) -> UnitOutcome:
    """Run (or resume) one unit under the plan's switches, persisting every run as it finishes.

    ``floor`` narrows the unit to one liquidity floor, so the two workers can share a window's
    floors instead of one worker carrying a whole long window. A run's identity does not depend
    on it: the same (window, arm, floor) has the same digest either way. A walk-forward narrowed
    to the ₹10 crore floor "selects" on that floor; the choice on record is re-made over both
    floors, ₹1 crore first, when the results are loaded (:func:`load_results`).

    Module-level so a spawned worker can import it; each worker reads the corporate-action
    calendar itself rather than receiving thirty-odd thousand actions pickled across.
    """
    floors = None if floor is None else (floor,)
    with (
        corporate_actions_in_force(_actions(plan), apply_to_book=True),
        accrue_cash_interest(_interest(plan)),
        persist_run_ledgers(plan.out_dir),
    ):
        if name == WALK_FORWARD:
            walk = _walk(plan, floors)
            results: tuple[SweepResult, ...] = (walk.selection, walk.verification)
        else:
            results = (_sweep(plan, name, floors),)
    replayed = resumed = failed = 0
    for result in results:
        bad = sum(1 for row in result.rows if not row.ok)
        replayed += len(result.rows) - result.resumed - bad
        resumed += result.resumed
        failed += bad
    unit = name if floor is None else f"{name}@{floor}"
    outcome = UnitOutcome(unit, replayed, resumed, failed)
    _LOG.info("m12_rerun.unit_done", unit=unit, replayed=replayed, resumed=resumed, failed=failed)
    return outcome


def run_rerun(plan: RerunPlan, *, workers: int) -> list[UnitOutcome]:
    """Every unit of ``plan``, at most :data:`~backtest.campaign.MAX_WORKERS` at a time."""
    if not 1 <= workers <= MAX_WORKERS:
        raise CampaignError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    _LOG.info("m12_rerun.start", out=str(plan.out_dir), units=list(plan.units), workers=workers)
    if workers == 1:
        return [run_unit(plan, name) for name in plan.units]
    # One item per (unit, floor), floor-major: both floors of the long units start before the
    # short ones, so the two workers finish close together.
    items = [(name, floor) for floor in plan.floors for name in plan.units]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(run_unit, plan, name, floor) for name, floor in items]
        return [future.result() for future in futures]


def _windows_of(units: Sequence[str]) -> list[str]:
    names: list[str] = []
    for unit in units:
        names.extend(["wf-selection", "wf-verification"] if unit == WALK_FORWARD else [unit])
    return names


def missing_runs(plan: RerunPlan) -> list[str]:
    """Every run the plan needs that has no summary and ledger on disk, as ``window arm floor``."""
    missing: list[str] = []
    with (
        corporate_actions_in_force(_actions(plan), apply_to_book=True),
        accrue_cash_interest(_interest(plan)),
    ):
        for name in _windows_of(plan.units):
            start, end = WINDOWS[name]
            digests = run_digests(
                start=start,
                end=end,
                arms=plan.arms,
                floors=plan.floors,
                universe_name=plan.universe,
            )
            for (label, floor), digest in digests.items():
                files = (summary_path(plan.out_dir, digest), ledger_path(plan.out_dir, digest))
                if not all(f.is_file() for f in files):
                    missing.append(f"{name} {label} {floor}")
    return missing


def load_results(plan: RerunPlan) -> tuple[dict[str, SweepResult], str]:
    """Every window's sweep, loaded from the persisted runs, and the walk-forward's frozen choice.

    Replays nothing when every run is on disk (call :func:`missing_runs` first to be sure); the
    walk-forward is re-entered through ``run_walk_forward`` so the choice is made by the same code,
    in the same order, as when it ran.
    """
    results: dict[str, SweepResult] = {}
    selected = ""
    with (
        corporate_actions_in_force(_actions(plan), apply_to_book=True),
        accrue_cash_interest(_interest(plan)),
        persist_run_ledgers(plan.out_dir),
    ):
        for unit in plan.units:
            if unit == WALK_FORWARD:
                walk = _walk(plan)
                results["wf-selection"], results["wf-verification"] = (
                    walk.selection,
                    walk.verification,
                )
                selected = walk.selected
            else:
                results[unit] = _sweep(plan, unit)
    return results, selected


def build_manifest(plan: RerunPlan, *, commit: str, last_session: date) -> dict[str, Any]:
    """The facts a resumed re-run must share with the one that started its directory."""
    manifest: dict[str, Any] = {
        "version": 1,
        "driver": "backtest.m12_rerun",
        "commit": commit,
        "data_root": str(plan.data_root.resolve()) if plan.data_root else "(configured)",
        "lake_last_session": last_session.isoformat(),
        "units": list(plan.units),
        "windows": {
            name: [WINDOWS[name][0].isoformat(), WINDOWS[name][1].isoformat()]
            for name in _windows_of(plan.units)
        },
        "arms": [arm.label for arm in plan.arms],
        "floors": [str(floor) for floor in plan.floors],
        "book_corporate_actions": plan.book_actions,
        "universe": plan.universe,
        "cash_interest": load_repo_rate_schedule().identity() if plan.cash_interest else None,
    }
    return manifest


def _rows_document(results: dict[str, SweepResult], selected: str) -> dict[str, Any]:
    """Every row as plain data — the input the gate report is rendered from."""
    windows: dict[str, Any] = {}
    for name, result in results.items():
        floors: dict[str, Any] = {}
        for floor in sorted({row.floor for row in result.rows}):
            rows = []
            for position, row in enumerate(result.ranked(floor), start=1):
                rows.append(
                    {
                        "rank": position if row.ok else None,
                        "label": row.arm.label,
                        "family": row.arm.family,
                        "ok": row.ok,
                        "error": row.error,
                        "xirr": str(row.xirr),
                        "max_drawdown": str(row.max_drawdown),
                        "ratio": str(row.return_per_drawdown),
                        "round_trips": row.round_trips,
                        "median_hold_days": row.median_hold_days,
                        "cost": str(row.total_charges),
                        "excess": str(row.excess),
                        "benchmark_source": row.benchmark_source,
                    }
                )
            floors[str(floor)] = rows
        windows[name] = {
            "start": result.start.isoformat(),
            "terminal": result.terminal.isoformat(),
            "sessions": result.sessions,
            "benchmark": result.benchmark_name,
            "benchmark_xirr": str(result.benchmark_xirr),
            "floors": floors,
        }
    return {"selected": selected, "windows": windows}


def _git_commit() -> str:
    # The campaign's own pin, so both drivers mean the same thing by "the commit".
    from backtest.campaign import _git_commit as commit

    return commit()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.m12_rerun",
        description="Re-run the M12 strategy review (every arm, both floors, the owner's windows). "
        "Resumable; at most two workers.",
    )
    parser.add_argument("--out", type=Path, required=True, help="run directory (never data/)")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=1, help=f"1..{MAX_WORKERS}")
    parser.add_argument(
        "--units",
        default=",".join(_DEFAULT_UNITS),
        help=f"comma-separated units, from {', '.join(UNITS)} (long is supplementary)",
    )
    parser.add_argument(
        "--arms",
        default=None,
        help="exact labels separated by '|': an attribution run over a few arms, never the "
        "re-run's own tables (the manifest records the arm list)",
    )
    parser.add_argument("--no-book-corporate-actions", dest="book_actions", action="store_false")
    parser.add_argument("--no-cash-interest", dest="cash_interest", action="store_false")
    parser.add_argument("--universe", default=UNIVERSE_TURNOVER_FLOOR, choices=UNIVERSE_CHOICES)
    parser.add_argument(
        "--results-only",
        action="store_true",
        help="write results.json from the runs on disk; refused if any run is missing",
    )
    parser.add_argument(
        "--runs-from-commit",
        default=None,
        metavar="SHA",
        help="with --results-only: the directory's pinned commit, when rendering at a later one",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.m12_rerun``. Returns a process exit code."""
    args = _parse_args(argv)
    units = tuple(part.strip() for part in args.units.split(",") if part.strip())
    try:
        arms = RERUN_ARMS
        if args.arms:
            wanted = [part.strip() for part in args.arms.split("|") if part.strip()]
            unknown = sorted(set(wanted) - {arm.label for arm in RERUN_ARMS})
            if unknown:
                raise CampaignError(f"no arm labelled {', '.join(unknown)}")
            arms = tuple(arm for arm in RERUN_ARMS if arm.label in wanted)
        plan = RerunPlan(
            out_dir=refuse_lake_location(args.out, args.data_root),
            data_root=args.data_root,
            units=units,
            arms=arms,
            book_actions=args.book_actions,
            cash_interest=args.cash_interest,
            universe=args.universe,
        )
        if args.runs_from_commit is not None and not args.results_only:
            raise CampaignError("--runs-from-commit renders only; add --results-only")
        reader = _L1Reader(data_root=args.data_root)
        try:
            last_session = reader.all_sessions()[-1]
        finally:
            reader.close()
        check_manifest(
            plan.out_dir,
            build_manifest(plan, commit=_git_commit(), last_session=last_session),
            runs_from_commit=args.runs_from_commit,
        )
        if args.results_only:
            missing = missing_runs(plan)
            if missing:
                raise CampaignError(
                    f"{len(missing)} runs are not on disk (first: {missing[0]}); run them first"
                )
        else:
            for outcome in run_rerun(plan, workers=args.workers):
                print(
                    f"  {outcome.unit:<16} replayed {outcome.replayed:>3}  "
                    f"resumed {outcome.resumed:>3}  failed {outcome.failed:>3}"
                )
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    results, selected = load_results(plan)
    path = plan.out_dir / "results.json"
    path.write_text(
        json.dumps(_rows_document(results, selected), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"  results written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
