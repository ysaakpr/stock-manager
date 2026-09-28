"""X2: the after-tax campaign — every arm, every configured window, both floors, one command.

``python -m backtest.campaign --out DIR ...`` runs the whole measurement the sweep and verdict
modules exist for: all arms (:data:`backtest.sweep.ARMS`) on each configured sweep window
(``backtest/windows.yaml``: full, decade, six-year) at both liquidity floors, plus the walk-forward
split of the full span, with every run's fill ledger persisted and the after-tax columns struck
from those ledgers. The runbook section is ``ops/runbooks/backtest-campaign.md``.

**Units of work are windows, not runs.** A sweep builds its lake state once per window and shares
it across every arm (``backtest.sweep``), so the unit a worker takes is one window's sweep — or the
walk-forward, whose selection and verification sweeps run in order inside one unit so the choice is
frozen before the verification figures exist (``backtest.verdict``). At most two worker processes:
the box has four cores and one sweep keeps about two of them busy (DuckDB's own threads), so two
is the ceiling CLAUDE.md sets for long replays side by side.

**Resumable, run by run.** Every run persists ``runs/<digest>.json`` and ``ledgers/<digest>.json``
under ``--out`` as it finishes (``backtest.run_ledger``), and a sweep loads a run whose files exist
instead of replaying it. A campaign killed at any point is restarted with the same command; only
the runs that had not finished are replayed, and a second invocation over a finished campaign
replays nothing. Reports are rendered last, in the parent, from the persisted runs.

**Pinned to the code and the lake that started it.** A run's digest covers what it was asked, not
the engine that answered or the lake it read, so ``--out`` carries a ``manifest.json`` (commit,
lake root, last L1 session, window config, corporate actions on/off). Resuming into a directory
whose manifest disagrees is refused: mixing runs from two commits in one table is how a ranking
silently compares two different engines.

What this module never does: read a wall clock into a result, write under the lake, or rank on an
after-tax figure (ranking stays pre-tax XIRR / max drawdown, the owner decision).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from backtest.book_actions import book_corporate_actions, load_store_book_actions
from backtest.run import _L1Reader
from backtest.run_ledger import persist_run_ledgers, refuse_lake_location
from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    attach_after_tax,
    l1_grandfathering,
    render_sweep_report,
    run_sweep,
)
from backtest.tax import GrandfatheringPrices, InvestorProfile
from backtest.tax_report import add_investor_flags, investor_profile_from_args
from backtest.verdict import attach_walk_after_tax, render_verdict, run_walk_forward
from backtest.windows import WINDOWS_PATH, CampaignWindows, load_windows, verify_windows
from dataplatform.logging import get_logger

__all__ = [
    "MAX_WORKERS",
    "CampaignError",
    "CampaignPlan",
    "UnitOutcome",
    "main",
    "render_campaign",
    "run_campaign",
]

_LOG = get_logger(__name__)

#: The ceiling on parallel sweeps (four cores, two busy per sweep). Not a default — a hard cap.
MAX_WORKERS = 2

_FLOORS: tuple[Decimal, ...] = (LOW_FLOOR, HIGH_FLOOR)
_WALK_FORWARD = "walk-forward"


class CampaignError(RuntimeError):
    """The campaign cannot start or resume as asked — fails loud, before any run."""


@dataclass(frozen=True, slots=True)
class CampaignPlan:
    """What one campaign measures and where it keeps it."""

    out_dir: Path
    windows: CampaignWindows
    data_root: Path | None
    book_actions: bool
    arms: tuple[Arm, ...] = ARMS
    floors: tuple[Decimal, ...] = _FLOORS

    @property
    def units(self) -> tuple[str, ...]:
        """Work units, longest first so two workers finish close together."""
        names = [w.name for w in self.windows.sweeps]
        ordered = ["full", _WALK_FORWARD, *(n for n in names if n != "full")]
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class UnitOutcome:
    """What one unit did: how many runs it replayed, loaded from disk, and saw fail."""

    unit: str
    replayed: int
    resumed: int
    failed: int


def _count(result: SweepResult) -> tuple[int, int, int]:
    failed = sum(1 for row in result.rows if not row.ok)
    return len(result.rows) - result.resumed - failed, result.resumed, failed


def _sweep_unit(plan: CampaignPlan, name: str) -> tuple[SweepResult, ...]:
    if name == _WALK_FORWARD:
        walk = run_walk_forward(
            selection=(plan.windows.selection.start, plan.windows.selection.end),
            verification=(plan.windows.verification.start, plan.windows.verification.end),
            arms=plan.arms,
            floors=plan.floors,
            data_root=plan.data_root,
        )
        return walk.selection, walk.verification
    window = plan.windows.named(name)
    return (
        run_sweep(
            start=window.start,
            end=window.end,
            arms=plan.arms,
            floors=plan.floors,
            data_root=plan.data_root,
        ),
    )


def run_unit(plan: CampaignPlan, name: str) -> UnitOutcome:
    """Run (or resume) one unit with persistence and the corporate actions in force.

    Module-level so a spawned worker can import it. Each worker loads the corporate-action
    calendar itself: the calendar is a Postgres read, and handing one across a process boundary
    would pickle thirty-odd thousand actions for no gain.
    """
    actions = load_store_book_actions() if plan.book_actions else None
    with book_corporate_actions(actions), persist_run_ledgers(plan.out_dir):
        results = _sweep_unit(plan, name)
    replayed = resumed = failed = 0
    for result in results:
        r, s, f = _count(result)
        replayed, resumed, failed = replayed + r, resumed + s, failed + f
    outcome = UnitOutcome(name, replayed, resumed, failed)
    _LOG.info("campaign.unit_done", unit=name, replayed=replayed, resumed=resumed, failed=failed)
    return outcome


UnitRunner = Callable[[CampaignPlan, str], UnitOutcome]


def run_campaign(
    plan: CampaignPlan, *, workers: int, unit_runner: UnitRunner = run_unit
) -> list[UnitOutcome]:
    """Run every unit of ``plan``, at most :data:`MAX_WORKERS` at a time; resumes by construction.

    ``workers=1`` runs the units in this process, in order (what a test drives); more spawns
    worker processes. Raises ``CampaignError`` for a worker count above the cap.
    """
    if not 1 <= workers <= MAX_WORKERS:
        raise CampaignError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    _LOG.info("campaign.start", out=str(plan.out_dir), units=list(plan.units), workers=workers)
    if workers == 1:
        return [unit_runner(plan, name) for name in plan.units]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(unit_runner, plan, name) for name in plan.units]
        return [future.result() for future in futures]


# ── the reports, from the persisted runs ───────────────────────────────────────────────────────


def render_campaign(
    plan: CampaignPlan, profile: InvestorProfile, *, fmv: GrandfatheringPrices
) -> dict[str, str]:
    """Every campaign report, keyed by file name, rendered from the runs already on disk.

    Re-enters each sweep under persistence, which loads every finished run rather than replaying
    it; a run that is still missing (a failed arm) is retried here, so call this after
    :func:`run_campaign`. The corporate-action switch must match the one the runs were made under,
    or no digest would match — the caller puts it in force.
    """
    reports: dict[str, str] = {}
    with persist_run_ledgers(plan.out_dir):
        context: dict[str, SweepResult] = {}
        for window in plan.windows.sweeps:
            result = run_sweep(
                start=window.start,
                end=window.end,
                arms=plan.arms,
                floors=plan.floors,
                data_root=plan.data_root,
            )
            result = attach_after_tax(result, profile, fmv=fmv)
            context[window.name] = result
            reports[f"sweep-{window.name}.md"] = render_sweep_report(result, floors=plan.floors)
        walk = run_walk_forward(
            selection=(plan.windows.selection.start, plan.windows.selection.end),
            verification=(plan.windows.verification.start, plan.windows.verification.end),
            arms=plan.arms,
            floors=plan.floors,
            data_root=plan.data_root,
        )
    walk.context.update(context)
    attach_walk_after_tax(walk, profile, fmv=fmv)
    reports["verdict-walk-forward.md"] = render_verdict(
        walk,
        floors=plan.floors,
        selection_window=(plan.windows.selection.start, plan.windows.selection.end),
        verification_window=(plan.windows.verification.start, plan.windows.verification.end),
    )
    return reports


# ── the manifest: one directory, one commit, one lake ──────────────────────────────────────────


def _git_commit() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise CampaignError(
            f"cannot read the git commit to pin the campaign to: {error}"
        ) from error
    return f"{head}{'-dirty' if dirty else ''}"


def build_manifest(plan: CampaignPlan, *, commit: str, last_session: date) -> dict[str, Any]:
    """The facts a resumed campaign must share with the one that started the directory."""
    config = WINDOWS_PATH.read_bytes()
    return {
        "version": 1,
        "commit": commit,
        "data_root": str(plan.data_root.resolve()) if plan.data_root else "(configured)",
        "lake_last_session": last_session.isoformat(),
        "windows_sha256": hashlib.sha256(config).hexdigest(),
        "book_corporate_actions": plan.book_actions,
        "arms": [arm.label for arm in plan.arms],
        "floors": [str(floor) for floor in plan.floors],
    }


def check_manifest(out_dir: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest on a fresh directory; refuse a directory whose manifest differs."""
    path = out_dir / "manifest.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != manifest:
            differs = sorted(k for k in manifest if existing.get(k) != manifest[k])
            raise CampaignError(
                f"{out_dir} was started by a different campaign (differs on: "
                f"{', '.join(differs)}); resume only with the commit and lake that started it, "
                "or use a fresh --out"
            )
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.campaign",
        description="Every arm x every configured window x both floors, plus the walk-forward, "
        "with fill ledgers and after-tax columns. Resumable; at most two workers.",
    )
    parser.add_argument("--out", type=Path, required=True, help="campaign directory (not data/)")
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument(
        "--workers", type=int, required=True, help=f"worker processes, 1..{MAX_WORKERS}"
    )
    parser.add_argument(
        "--no-book-corporate-actions",
        dest="book_corporate_actions",
        action="store_false",
        help="run without splits/bonuses/dividends in the book (before/after measurement only)",
    )
    parser.add_argument(
        "--reports-only",
        action="store_true",
        help="render the reports from the runs already on disk; replay only what is missing",
    )
    add_investor_flags(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.campaign``. Returns a process exit code."""
    args = _parse_args(argv)
    profile = investor_profile_from_args(args)
    windows = load_windows()
    reader = _L1Reader(data_root=args.data_root)
    try:
        sessions = reader.all_sessions()
    finally:
        reader.close()
    try:
        for line in verify_windows(windows, sessions):
            _LOG.info("campaign.window_check", detail=line)
        out_dir = refuse_lake_location(args.out, args.data_root)
        plan = CampaignPlan(
            out_dir=out_dir,
            windows=windows,
            data_root=args.data_root,
            book_actions=args.book_corporate_actions,
        )
        check_manifest(
            out_dir, build_manifest(plan, commit=_git_commit(), last_session=sessions[-1])
        )
        if not args.reports_only:
            outcomes = run_campaign(plan, workers=args.workers)
            for outcome in outcomes:
                print(
                    f"  {outcome.unit:<16} replayed {outcome.replayed:>3}  "
                    f"resumed {outcome.resumed:>3}  failed {outcome.failed:>3}"
                )
    except (CampaignError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    actions = load_store_book_actions() if plan.book_actions else None
    service, fmv = l1_grandfathering(args.data_root)
    with service, book_corporate_actions(actions):
        reports = render_campaign(plan, profile, fmv=fmv)
    report_dir = out_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    for name, text in reports.items():
        (report_dir / name).write_text(text, encoding="utf-8")
        print(f"  report written to {report_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
