"""Per-stock rebalance study: D13 against its daily band exit and daily drift trim, on the folds.

``ops/studies/preregistration-per-stock-rebalance-2026-10-15.md`` fixes the question, the arms and
the rule before any run: two switches on D13 (``backtest.sweep.PER_STOCK_REBALANCE_ARMS``) judged
by the round-2 four-criterion rule (``backtest.decision_rule``) against D13 itself, on the fold test
windows F1-F3 (``backtest.folds``), at the ₹10 crore/day floor, after tax. The ₹1 crore/day floor is
run beside it and reported for information only.

The driver is the fold campaign's (``backtest.fold_campaign``) — the same windows, contexts, run
path, after-tax figures and decision renderer — with three differences the study needs:

* **The baseline is D13**, not the round-2 M10.7 pair, so it runs here, first, and is frozen
  (``frozen-baseline.json``: every run's digest and file hashes, the commit, lake and panel) before
  either arm is replayed. The arms phase refuses to start unless that record exists and the current
  code gives the same digests and the files still hash the same.
* **Both floors.** Every run is made at ₹10 crore (decides) and ₹1 crore (informational).
* **A daily signal panel** (``backtest.signal_panel``, M14.6) can serve the momentum-v2 candidate
  sets: arm 1 ranks the universe every session. The panel does not enter a run's digest, and
  baseline and arms read the same one.

``python -m backtest.per_stock_rebalance run --out DIR --workers N [--signal-panel P]``
    Runs D13 on every fold test window and the continuous F1-F3 window at both floors, freezes it,
    then runs the two arms the same way. Resumable run by run.

``python -m backtest.per_stock_rebalance render --out DIR --trials N --trial-sharpes FILE``
    Replays nothing: reads the runs under ``DIR`` and writes ``DIR/reports/decision.md`` — the
    four criteria per arm with their figures, the continuous headline, and per arm, fold and floor
    the after-tax XIRR, max drawdown, sells, exits and trims fired, charges and tax paid.

What it never does: write under the lake, run an arm before the baseline is frozen, tune a
parameter, or round a figure before the rule compares it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from backtest.book_actions import corporate_actions_in_force, load_store_book_actions
from backtest.campaign import MAX_WORKERS, CampaignError, _git_commit
from backtest.cash_interest import (
    accrue_cash_interest,
    describe_cash_interest,
    load_repo_rate_schedule,
)
from backtest.decision_rule import (
    PREREGISTERED_MIN_TRIALS,
    ArmFolds,
    TrialSharpe,
    evaluate,
    rail_blocks_cell,
    render_decision,
    trial_sharpe_variance,
)
from backtest.fold_campaign import (
    CONTINUOUS,
    PROFILE,
    FoldWindow,
    build_arm_folds,
    continuous_fold,
    continuous_window,
    headline_lines,
    universe_line,
)
from backtest.folds import FOLDS_PATH, FoldPlan, load_folds
from backtest.nav import nav_file
from backtest.run import UNIVERSE_TURNOVER_FLOOR, _L1Reader
from backtest.run_ledger import (
    ledger_path,
    load_run,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
)
from backtest.signal_panel import SignalPanel, signal_panel_in_force
from backtest.sweep import (
    D13_PAPER_BASELINE,
    HIGH_FLOOR,
    LOW_FLOOR,
    PER_STOCK_REBALANCE_ARMS,
    Arm,
    l1_grandfathering,
    run_digests,
    run_sweep,
)
from backtest.tax import GrandfatheringPrices, TaxError, compute_after_tax, load_tax_schedule
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "BASELINE",
    "DECIDING_FLOOR",
    "FLOORS",
    "FROZEN_NAME",
    "PREREGISTRATION_PATH",
    "StudyError",
    "StudyPlan",
    "load_supplied_sharpes",
    "main",
    "study_plan",
]

_LOG = get_logger(__name__)

#: The frozen baseline: the paper book's configuration (D13), by its sweep label.
BASELINE: Arm = D13_PAPER_BASELINE
#: The floor the rule is struck at (pre-registration §5), and the one reported for information.
DECIDING_FLOOR = HIGH_FLOOR
FLOORS: tuple[Decimal, ...] = (HIGH_FLOOR, LOW_FLOOR)
PREREGISTRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "ops/studies/preregistration-per-stock-rebalance-2026-10-15.md"
)
FROZEN_NAME = "frozen-baseline.json"
_TEST = "test"
_KINDS = ("daily band exit", "drift trim")
#: The idle-cash convention a supplied trial file must share (``backtest.cash_interest``).
_INTEREST_CONVENTION = "repo-50bp:"


class StudyError(RuntimeError):
    """The study cannot proceed as asked — fails loud, before any run or render."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class StudyPlan:
    """What one phase runs: these arms on every fold test window and the continuous window."""

    out_dir: Path
    arms: tuple[Arm, ...]
    windows: tuple[FoldWindow, ...]
    data_root: Path | None
    signal_panel: Path | None
    floors: tuple[Decimal, ...] = FLOORS
    universe: str = UNIVERSE_TURNOVER_FLOOR

    @property
    def units(self) -> tuple[tuple[int, Decimal], ...]:
        """(window, floor) pairs, longest window first so two workers finish close together."""
        spans = [(w.window.end - w.window.start).days for w in self.windows]
        order = sorted(range(len(self.windows)), key=lambda i: (-spans[i], i))
        return tuple((i, floor) for i in order for floor in self.floors)


def study_plan(
    out_dir: Path,
    folds: FoldPlan,
    arms: Sequence[Arm],
    *,
    data_root: Path | None,
    signal_panel: Path | None,
) -> StudyPlan:
    """The plan for ``arms``: F1-F3 test windows plus the continuous window over their union."""
    windows = [FoldWindow(f.name, _TEST, f.test) for f in folds.folds]
    windows.append(FoldWindow(continuous_fold(folds), CONTINUOUS, continuous_window(folds)))
    return StudyPlan(
        out_dir=out_dir,
        arms=tuple(arms),
        windows=tuple(windows),
        data_root=data_root,
        signal_panel=signal_panel,
    )


def _contexts(stack: ExitStack, plan: StudyPlan) -> None:
    """The run conditions every run and digest of the study is made under (as the fold campaign)."""
    stack.enter_context(
        corporate_actions_in_force(
            load_store_book_actions(data_root=plan.data_root), apply_to_book=True
        )
    )
    stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))


def run_unit(plan: StudyPlan, index: int, floor: Decimal) -> tuple[str, int]:
    """Run (or resume) every arm of ``plan`` on one window at one floor; ``(unit, failed)``."""
    target = plan.windows[index]
    panel = SignalPanel(plan.signal_panel) if plan.signal_panel is not None else None
    with ExitStack() as stack:
        _contexts(stack, plan)
        stack.enter_context(persist_run_ledgers(plan.out_dir))
        stack.enter_context(signal_panel_in_force(panel))
        result = run_sweep(
            start=target.window.start,
            end=target.window.end,
            arms=plan.arms,
            floors=(floor,),
            data_root=plan.data_root,
            universe_name=plan.universe,
        )
    failed = sum(1 for row in result.rows if not row.ok)
    for row in result.rows:
        if not row.ok:
            _LOG.error("per_stock_rebalance.run_failed", arm=row.arm.label, error=row.error)
    unit = f"{target.fold}-{target.role}@{floor}"
    _LOG.info("per_stock_rebalance.unit_done", unit=unit, failed=failed)
    return unit, failed


def run_units(plan: StudyPlan, *, workers: int) -> int:
    """Every (window, floor) unit of ``plan``; returns the number of failed runs."""
    if not 1 <= workers <= MAX_WORKERS:
        raise StudyError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    if workers == 1:
        outcomes = [run_unit(plan, i, floor) for i, floor in plan.units]
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = [pool.submit(run_unit, plan, i, floor) for i, floor in plan.units]
            outcomes = [future.result() for future in futures]
    for unit, failed in outcomes:
        print(f"  {unit:<32} failed {failed}")
    return sum(failed for _, failed in outcomes)


def _digests(plan: StudyPlan, floor: Decimal) -> dict[tuple[str, str, str], str]:
    """``(arm label, fold, role) -> digest`` at ``floor``, under the study's contexts."""
    out: dict[tuple[str, str, str], str] = {}
    with ExitStack() as stack:
        _contexts(stack, plan)
        for target in plan.windows:
            digests = run_digests(
                start=target.window.start,
                end=target.window.end,
                arms=plan.arms,
                floors=(floor,),
                universe_name=plan.universe,
            )
            for arm in plan.arms:
                out[(arm.label, target.fold, target.role)] = digests[(arm.label, floor)]
    return out


def _files(out_dir: Path, digest: str) -> dict[str, Path]:
    return {
        "summary": summary_path(out_dir, digest),
        "ledger": ledger_path(out_dir, digest),
        "nav": nav_file(out_dir, digest),
    }


def _lake_last_session(data_root: Path | None) -> date:
    reader = _L1Reader(data_root=data_root)
    try:
        return reader.all_sessions()[-1]
    finally:
        reader.close()


def freeze_baseline(plan: StudyPlan, *, commit: str, lake_last_session: date) -> dict[str, Any]:
    """Write ``frozen-baseline.json`` for the finished baseline phase and return it.

    Refuses a partial phase, and a record already on disk that differs in any run (a frozen record
    is never rewritten).
    """
    runs: list[dict[str, str]] = []
    missing: list[str] = []
    for floor in plan.floors:
        for (label, fold, role), digest in sorted(_digests(plan, floor).items()):
            files = _files(plan.out_dir, digest)
            absent = [kind for kind, path in files.items() if not path.is_file()]
            if absent:
                missing.append(f"{label} {fold} {role} @{floor} ({', '.join(absent)})")
                continue
            runs.append(
                {
                    "arm": label,
                    "fold": fold,
                    "role": role,
                    "floor": str(floor),
                    "digest": digest,
                    **{f"{kind}_sha256": _sha256(path) for kind, path in files.items()},
                }
            )
    if missing:
        raise StudyError(f"cannot freeze: {len(missing)} baseline run(s) incomplete: {missing[:4]}")
    panel = SignalPanel(plan.signal_panel).identity() if plan.signal_panel else None
    document = {
        "version": 1,
        "baseline": BASELINE.label,
        "commit": commit,
        "lake_last_session": lake_last_session.isoformat(),
        "universe": plan.universe,
        "folds_sha256": _sha256(FOLDS_PATH),
        "preregistration_sha256": _sha256(PREREGISTRATION_PATH),
        "cash_interest": load_repo_rate_schedule().identity(),
        "signal_panel": panel,
        "runs": runs,
    }
    path = plan.out_dir / FROZEN_NAME
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["runs"] != runs:
            raise StudyError(f"{path} records a different frozen baseline; it is never rewritten")
        return dict(existing)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _LOG.info("per_stock_rebalance.frozen", path=str(path), runs=len(runs))
    return document


def verify_frozen(plan: StudyPlan) -> dict[str, Any]:
    """The frozen record, or ``StudyError`` if the current code or files no longer match it."""
    path = plan.out_dir / FROZEN_NAME
    if not path.is_file():
        raise StudyError(f"no frozen baseline at {path}: the baseline phase has not finished")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if frozen.get("preregistration_sha256") != _sha256(PREREGISTRATION_PATH):
        problems.append("the pre-registration changed since the freeze")
    if frozen.get("folds_sha256") != _sha256(FOLDS_PATH):
        problems.append("the folds changed since the freeze")
    recorded = {(r["arm"], r["fold"], r["role"], r["floor"]): r for r in frozen["runs"]}
    for floor in plan.floors:
        for (label, fold, role), digest in _digests(plan, floor).items():
            record = recorded.get((label, fold, role, str(floor)))
            if record is None:
                problems.append(f"{label} {fold} @{floor}: not in the frozen record")
                continue
            if record["digest"] != digest:
                problems.append(f"{label} {fold} @{floor}: digest moved")
                continue
            for kind, file in _files(plan.out_dir, digest).items():
                if not file.is_file() or _sha256(file) != record[f"{kind}_sha256"]:
                    problems.append(f"{label} {fold} @{floor}: {kind} file changed")
    if problems:
        raise StudyError("the frozen baseline does not match: " + "; ".join(problems[:6]))
    return dict(frozen)


# ── render ─────────────────────────────────────────────────────────────────────────────────────


def load_supplied_sharpes(path: Path) -> tuple[tuple[TrialSharpe, ...], str]:
    """The trial Sharpes of a ``backtest.fold_campaign trial-sharpes`` file, with provenance.

    Accepts only a file struck on these folds, at the deciding floor, on the floor-only universe,
    with book corporate actions and interest on idle cash at repo - 50 bp; its commit, lake,
    pre-registration and schedule identity are the round-1 campaign's and are named in the
    provenance, not refused (pre-registration §6: the schedule has since gained only a row
    effective after the last fold closes).
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    wrong = [
        name
        for name, ok in (
            ("folds", document.get("folds_sha256") == _sha256(FOLDS_PATH)),
            ("floor", document.get("floor") == str(DECIDING_FLOOR)),
            ("universe", document.get("universe") == UNIVERSE_TURNOVER_FLOOR),
            ("book corporate actions", document.get("book_corporate_actions") is True),
            (
                "cash interest",
                str(document.get("cash_interest", "")).startswith(_INTEREST_CONVENTION),
            ),
        )
        if not ok
    ]
    if wrong:
        raise StudyError(f"{path} was struck under different conditions: {', '.join(wrong)}")
    sharpes = tuple(TrialSharpe(str(t["arm"]), float(t["sharpe"])) for t in document["trials"])
    source = (
        f"{path.name} @ {str(document['commit'])[:12]}, lake through "
        f"{document['lake_last_session']}, interest {document['cash_interest']} "
        "(round-1 arms on F1-F3)"
    )
    return sharpes, source


@dataclass(frozen=True, slots=True)
class _RunFacts:
    pre_tax_xirr: Decimal
    after_tax_xirr: Decimal | None
    after_tax_error: str | None
    max_drawdown: Decimal
    sells: int
    buys: int
    exits: int
    trims: int
    charges: Decimal
    tax: Decimal | None
    rail_blocks: tuple[tuple[str, int], ...] | None


def _facts(out_dir: Path, digest: str, fmv: GrandfatheringPrices) -> _RunFacts:
    loaded = load_run(out_dir, digest)
    if loaded is None:
        raise StudyError(f"run {digest} not on disk under {out_dir}")
    summary, ledger = loaded
    tally = summary.policy_sells or {}
    after: Decimal | None
    tax: Decimal | None
    try:
        taxed = compute_after_tax(ledger, PROFILE, schedule=load_tax_schedule(), fmv=fmv)
        after, error, tax = (
            taxed.after_tax_xirr_realised,
            taxed.realised_xirr_error,
            taxed.total_tax,
        )
    except TaxError as failure:
        after, error, tax = None, str(failure), None
    blocks = summary.rail_blocks
    return _RunFacts(
        pre_tax_xirr=summary.xirr,
        after_tax_xirr=after,
        after_tax_error=error if after is None else None,
        max_drawdown=summary.max_drawdown,
        sells=sum(1 for t in ledger.trades if t.side is Side.SELL),
        buys=sum(1 for t in ledger.trades if t.side is Side.BUY),
        exits=tally.get(_KINDS[0], 0),
        trims=tally.get(_KINDS[1], 0),
        charges=summary.total_charges,
        tax=tax,
        rail_blocks=None if blocks is None else tuple(sorted(blocks.items())),
    )


def _pct(value: Decimal | None, error: str | None = None) -> str:
    return f"{value:.2%}" if value is not None else f"n/a ({error or 'not computable'})"


def _inr(value: Decimal | None) -> str:
    return "n/a" if value is None else f"₹{value:,.0f}"


def _floor_label(floor: Decimal) -> str:
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


def _figures_table(
    out_dir: Path,
    labels: Sequence[str],
    digests: dict[tuple[str, str, str], str],
    windows: Sequence[FoldWindow],
    fmv: GrandfatheringPrices,
) -> list[str]:
    lines = [
        "| Arm | Window | Pre-tax XIRR | After-tax XIRR (realised) | Max drawdown | Buys | Sells "
        "| of which daily band exits | of which drift trims | Charges | Tax paid | Rail blocks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label in labels:
        for window in windows:
            facts = _facts(out_dir, digests[(label, window.fold, window.role)], fmv)
            lines.append(
                f"| {label} | {window.fold} ({window.window.start} → {window.window.end}) | "
                f"{facts.pre_tax_xirr:.2%} | {_pct(facts.after_tax_xirr, facts.after_tax_error)} | "
                f"{facts.max_drawdown:.2%} | {facts.buys} | {facts.sells} | {facts.exits} | "
                f"{facts.trims} | {_inr(facts.charges)} | {_inr(facts.tax)} | "
                f"{rail_blocks_cell(facts.rail_blocks)} |"
            )
    return lines


def render(
    plan: StudyPlan,
    folds: FoldPlan,
    fmv: GrandfatheringPrices,
    *,
    trials: int,
    supplied: Sequence[TrialSharpe],
    supplied_source: str,
    frozen: dict[str, Any],
) -> str:
    """The study's decision and figures, from the runs on disk under ``plan.out_dir``."""
    labels = [arm.label for arm in plan.arms]
    decide = _digests(plan, DECIDING_FLOOR)
    arm_folds: list[ArmFolds] = [
        build_arm_folds(plan.out_dir, label, decide, folds, fmv=fmv, nav_dir=plan.out_dir)
        for label in labels
    ]
    variance = trial_sharpe_variance(arm_folds, supplied=supplied, supplied_source=supplied_source)
    baseline = arm_folds[0]
    verdicts = [
        evaluate(candidate, baseline, trials=trials, sharpe_variance=variance)
        for candidate in arm_folds[1:]
    ]
    headline = headline_lines([(label, plan.out_dir) for label in labels], decide, folds, fmv=fmv)
    panel = frozen.get("signal_panel")
    text = render_decision(
        verdicts,
        arm_folds,
        trials=trials,
        sharpe_variance=variance,
        floor_label=_floor_label(DECIDING_FLOOR),
        assumptions=[
            universe_line(plan.universe),
            describe_cash_interest(True),
            "Investor: resident individual, 30% slab, no surcharge, tax paid at FY end; "
            "after-tax XIRR on realised gains.",
            f"Baseline frozen at commit `{str(frozen['commit'])[:12]}` on the lake through "
            f"{frozen['lake_last_session']} before either arm ran (`{FROZEN_NAME}`).",
            "Momentum-v2 candidate sets served from the signal panel "
            + (f"`{panel['path']}` (cut {panel['first']} … {panel['last']})" if panel else "(none)")
            + "; baseline and arms read the same one.",
        ],
        command="render",
        headline=headline,
        title="Per-stock rebalance — the pre-registered decision rule",
        provenance=(
            "*Rule: `backtest/decision_rule.py` (round 2's four criteria), as pre-registered in "
            "`ops/studies/preregistration-per-stock-rebalance-2026-10-15.md` before any run. "
            "Generated by `python -m backtest.per_stock_rebalance render`.*"
        ),
    )
    lines = [text.rstrip("\n"), ""]
    for floor in plan.floors:
        role = "decides" if floor == DECIDING_FLOOR else "information only — decides nothing"
        lines += [
            f"## Every run at {_floor_label(floor)} ({role})",
            "",
            "*Sells and buys are fills from the run's ledger. Daily band exits and drift trims "
            "are the policy's SELL decisions of that kind (decided and offered to the rails; a "
            "rail-refused one counts here and under rail blocks too). Charges are every rupee "
            "of the shared cost model; tax paid is the realised-gains tax over the run.*",
            "",
            *_figures_table(plan.out_dir, labels, _digests(plan, floor), plan.windows, fmv),
            "",
        ]
    return "\n".join(lines) + "\n"


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.per_stock_rebalance")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run and freeze D13, then run the two arms")
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--signal-panel", type=Path, default=None)
    rend = sub.add_parser("render", help="render the decision from runs on disk")
    rend.add_argument("--trials", type=int, required=True)
    rend.add_argument("--trial-sharpes", type=Path, required=True)
    for p in (run, rend):
        p.add_argument("--out", type=Path, required=True, help="study directory (not data/)")
        p.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.per_stock_rebalance``. Returns a process exit code."""
    args = _parse_args(argv)
    folds = load_folds()
    try:
        out_dir = refuse_lake_location(args.out, args.data_root)
        panel = args.signal_panel.resolve() if getattr(args, "signal_panel", None) else None
        base = study_plan(out_dir, folds, (BASELINE,), data_root=args.data_root, signal_panel=panel)
        arms = study_plan(
            out_dir,
            folds,
            PER_STOCK_REBALANCE_ARMS,
            data_root=args.data_root,
            signal_panel=panel,
        )
        if args.command == "run":
            out_dir.mkdir(parents=True, exist_ok=True)
            commit = _git_commit()
            if run_units(base, workers=args.workers):
                raise StudyError("a baseline run failed; nothing frozen (re-run to retry)")
            freeze_baseline(
                base, commit=commit, lake_last_session=_lake_last_session(args.data_root)
            )
            verify_frozen(base)
            print(f"  baseline frozen: {out_dir / FROZEN_NAME}")
            if run_units(arms, workers=args.workers):
                raise StudyError("an arm run failed; re-run to retry")
            return 0
        if args.trials < PREREGISTERED_MIN_TRIALS:
            raise StudyError(f"--trials is at least {PREREGISTERED_MIN_TRIALS}")
        frozen = verify_frozen(base)
        supplied, source = load_supplied_sharpes(args.trial_sharpes)
        everything = study_plan(
            out_dir,
            folds,
            (BASELINE, *PER_STOCK_REBALANCE_ARMS),
            data_root=args.data_root,
            signal_panel=panel,
        )
        service, fmv = l1_grandfathering(args.data_root)
        with service:
            text = render(
                everything,
                folds,
                fmv,
                trials=args.trials,
                supplied=supplied,
                supplied_source=source,
                frozen=frozen,
            )
    except (StudyError, CampaignError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    report = out_dir / "reports" / "decision.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    print(f"  report written to {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
