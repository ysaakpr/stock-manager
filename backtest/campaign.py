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

**Idle cash earns interest by default.** Every run accrues RBI repo - 0.50% on settled idle cash
(``backtest.cash_interest``) unless ``--no-cash-interest`` says otherwise; either way the first
line of every report states which, the run specs (and so the digests) record it, and the manifest
records it when on — an interest-on campaign never resumes into an interest-off directory.

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

from backtest.book_actions import (
    BookActionSource,
    book_corporate_actions,
    load_store_book_actions,
)
from backtest.cash_interest import (
    RepoRateSchedule,
    accrue_cash_interest,
    describe_cash_interest,
    load_repo_rate_schedule,
)
from backtest.run import _L1Reader
from backtest.run_ledger import (
    ledger_path,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
)
from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    attach_after_tax,
    l1_grandfathering,
    render_sweep_report,
    run_digests,
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
    #: Idle settled cash earns repo - 50 bp (``backtest.cash_interest``). On for every campaign
    #: unless switched off for a before/after measurement.
    cash_interest: bool = True

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


def _interest(plan: CampaignPlan) -> RepoRateSchedule | None:
    """The cash-interest schedule ``plan`` runs under, or ``None`` when it is switched off."""
    return load_repo_rate_schedule() if plan.cash_interest else None


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
    with (
        book_corporate_actions(actions),
        accrue_cash_interest(_interest(plan)),
        persist_run_ledgers(plan.out_dir),
    ):
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
    or no digest would match — the caller puts it in force. Cash interest is the plan's own, put in
    force here, and the first line of every report states it.
    """
    reports: dict[str, str] = {}
    with persist_run_ledgers(plan.out_dir), accrue_cash_interest(_interest(plan)):
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
    header = f"> {describe_cash_interest(plan.cash_interest)}\n\n"
    return {name: header + text for name, text in reports.items()}


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
    manifest: dict[str, Any] = {
        "version": 1,
        "commit": commit,
        "data_root": str(plan.data_root.resolve()) if plan.data_root else "(configured)",
        "lake_last_session": last_session.isoformat(),
        "windows_sha256": hashlib.sha256(config).hexdigest(),
        "book_corporate_actions": plan.book_actions,
        "arms": [arm.label for arm in plan.arms],
        "floors": [str(floor) for floor in plan.floors],
    }
    if plan.cash_interest:
        # Only when on: a directory made before cash interest existed keeps its manifest, and one
        # made with interest refuses to resume the other.
        manifest["cash_interest"] = load_repo_rate_schedule().identity()
    return manifest


def check_manifest(
    out_dir: Path, manifest: dict[str, Any], *, runs_from_commit: str | None = None
) -> None:
    """Write the manifest on a fresh directory; refuse a directory whose manifest differs.

    ``runs_from_commit`` is the render-only exception (see :func:`check_render_only`): the
    directory's runs were replayed at that earlier commit, and only ``commit`` may differ.
    """
    path = out_dir / "manifest.json"
    if runs_from_commit is not None:
        if not path.is_file():
            raise CampaignError(f"{out_dir} has no manifest; there are no runs to render")
        existing = json.loads(path.read_text(encoding="utf-8"))
        check_render_only(existing, manifest, runs_from_commit)
        return
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


def check_render_only(existing: dict[str, Any], manifest: dict[str, Any], runs_commit: str) -> None:
    """Refuse a render-only pass unless the runs are provably one engine's, and named as such.

    The manifest pins the commit so one table never compares two engines. A render that replays
    nothing cannot mix engines — every row is still the pinned commit's run — so rendering at a
    later commit is honest when: every field but ``commit`` matches; the operator names the
    directory's commit (``runs_commit``) rather than having it waved through; that commit is an
    ancestor of a clean HEAD; and (checked separately, :func:`missing_runs`) every run is on disk.
    """
    differs = sorted(k for k in manifest if existing.get(k) != manifest[k] and k != "commit")
    if differs:
        raise CampaignError(
            f"render-only: the directory differs on more than the commit ({', '.join(differs)})"
        )
    pinned = str(existing.get("commit", ""))
    if len(runs_commit) < 7 or not pinned.startswith(runs_commit) or pinned.endswith("-dirty"):
        raise CampaignError(
            f"render-only: --runs-from-commit {runs_commit} does not name this directory's "
            f"clean commit {pinned}"
        )
    head = str(manifest["commit"])
    if head.endswith("-dirty"):
        raise CampaignError("render-only: the rendering tree is dirty; commit first")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", pinned, head], check=False, capture_output=True
    )
    if ancestor.returncode != 0:
        raise CampaignError(f"render-only: {pinned} is not an ancestor of HEAD {head}")


def missing_runs(plan: CampaignPlan, actions: BookActionSource | None) -> list[str]:
    """Every run the reports need that has no summary and ledger on disk, as ``window arm floor``.

    A render-only pass refuses on a non-empty list: rendering would replay those runs at the
    rendering commit and put two engines in one table. ``actions`` is the corporate-action source
    the render runs under: a run's digest covers it, so it is put in force here rather than left
    to the caller — derived outside it, every digest would miss. The plan's cash interest likewise.
    """
    windows = [*plan.windows.sweeps, plan.windows.selection, plan.windows.verification]
    missing: list[str] = []
    for window in windows:
        with book_corporate_actions(actions), accrue_cash_interest(_interest(plan)):
            digests = run_digests(
                start=window.start, end=window.end, arms=plan.arms, floors=plan.floors
            )
        for (label, floor), digest in digests.items():
            files = (summary_path(plan.out_dir, digest), ledger_path(plan.out_dir, digest))
            if not all(f.is_file() for f in files):
                missing.append(f"{window.name} {label} {floor}")
    return missing


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
        "--no-cash-interest",
        dest="cash_interest",
        action="store_false",
        help="idle cash earns 0%% instead of RBI repo - 0.50%% (before/after measurement only); "
        "every report states which",
    )
    parser.add_argument(
        "--reports-only",
        action="store_true",
        help="render the reports from the runs already on disk; replay only what is missing",
    )
    parser.add_argument(
        "--runs-from-commit",
        default=None,
        metavar="SHA",
        help="with --reports-only: render at this (later) commit runs that were replayed at SHA, "
        "the directory's pinned commit; refused unless every run is on disk and only the commit "
        "differs. Nothing is replayed.",
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
            cash_interest=args.cash_interest,
        )
        if args.runs_from_commit is not None and not args.reports_only:
            raise CampaignError("--runs-from-commit renders only; add --reports-only")
        head = _git_commit()
        check_manifest(
            out_dir,
            build_manifest(plan, commit=head, last_session=sessions[-1]),
            runs_from_commit=args.runs_from_commit,
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
    if args.runs_from_commit is not None:
        missing = missing_runs(plan, actions)
        if missing:
            print(
                f"error: render-only: {len(missing)} run(s) not on disk, and replaying them here "
                f"would mix engines: {', '.join(missing[:5])}",
                file=sys.stderr,
            )
            return 2
    service, fmv = l1_grandfathering(args.data_root)
    with service, book_corporate_actions(actions):
        reports = render_campaign(plan, profile, fmv=fmv)
    report_dir = out_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    if args.runs_from_commit is not None:
        pinned = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))["commit"]
        stamp = (
            f"> Runs replayed at `{pinned}`; this report rendered at `{head}` "
            "(`--reports-only --runs-from-commit`). "
            "No run was replayed at the rendering commit.\n\n"
        )
        reports = {name: stamp + text for name, text in reports.items()}
    for name, text in reports.items():
        (report_dir / name).write_text(text, encoding="utf-8")
        print(f"  report written to {report_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
