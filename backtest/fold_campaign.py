"""X2: the round-2 fold campaign — freeze the baseline first, then test H-arms against it.

``ops/studies/preregistration-signals-2026-09-29.md`` fixes the order: the two baseline arms run
on the folds and are **frozen, digests recorded, before any H-arm is run** (§2), and every H-arm is
then judged by the four-part rule (§4, ``backtest.decision_rule``) against that frozen record. This
module is that order, as two commands that cannot be run the other way round:

``python -m backtest.fold_campaign baseline-folds --out DIR --workers N``
    Runs **only** the two baseline arms — Swing composite (M10.7) and M10.7 + regime gate — on
    every fold's test window (``backtest/folds.yaml``) and, for the record, its selection window:
    ₹10 crore floor, cash interest on, corporate actions in the book. Every run persists its
    summary, fill ledger and daily NAV (``backtest.nav``) under ``DIR``; once all have finished it
    writes ``DIR/frozen-baseline.json``: each run's digest and the sha256 of each file, plus the
    commit, lake, folds and pre-registration they were made under, and ``DIR/reports/
    baseline-folds.md``. Resumable run by run, like ``backtest.campaign``.

``python -m backtest.fold_campaign round2-signals --baseline-dir DIR --out DIR2 --arms ...
--baseline LABEL --trials N --workers N``
    **Refuses to start** unless ``DIR/frozen-baseline.json`` exists, was made on this lake, these
    folds and this pre-registration, every baseline digest it records is the digest the *current
    code* gives the same run, and every file it names is on disk with the recorded hash. Then runs
    the named H-arms on the fold test windows under ``DIR2`` (the frozen directory is only read)
    and writes ``DIR2/reports/round2-decision.md``: PASS/FAIL per criterion per arm against the
    named baseline, with the trial count, the folds each criterion read and the Sharpe variance's
    source and n printed. ``--trial-sharpes FILE`` adds a ``trial-sharpes`` file's Sharpes to the
    variance (below ``MIN_VARIANCE_SHARPES`` distinct Sharpes criterion 4 cannot PASS).

``python -m backtest.fold_campaign trial-sharpes --out DIR3 [--arms ...] --workers N``
    Runs the round-1 sweep's arms (default: every one in ``backtest.sweep.ARMS`` that is neither a
    baseline nor a round-2 hypothesis) on the fold test windows and writes ``DIR3/trial-sharpes
    .json``: each arm's per-period Sharpe on its concatenated after-tax test returns — the trials
    §2 counts, on the same folds, as the variance source for criterion 4. An arm missing a fold is
    recorded as excluded with the reason, never scored on the folds it has.

``python -m backtest.fold_campaign render-round2 --baseline-dir DIR --runs-dir DIR2
--runs-from-commit SHA --out DIR4 --arms ... --baseline LABEL --trials N [--trial-sharpes FILE]``
    Re-renders the decision from H-arm runs already on disk, **replaying nothing**: refuses unless
    every run is there and ``DIR2`` differs from this checkout's pin on the commit alone (named by
    ``--runs-from-commit``, an ancestor of a clean HEAD). ``DIR`` and ``DIR2`` are only read; the
    report and the after-tax NAVs go under ``DIR4``.

**The investor is fixed, not a flag**: a resident individual at the 30 % slab, no surcharge, tax
paid at FY end (§2). A different investor would be a different study.

**A baseline digest changes when the baseline's specification does** — which includes the repr
of its policy parameters. Adding a field to ``SwingCompositeParameters`` (an H-arm's new weight,
say) changes the frozen arms' digests even at a neutral default, and round2-signals will refuse:
freeze the baseline *after* the H-arm code has landed and *before* any H-arm is run.

What this module never does: run an H-arm before the baseline is frozen, rewrite a frozen record,
read a wall clock into a result, write under the lake, or pick the baseline arm for the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
from backtest.campaign import (
    MAX_WORKERS,
    CampaignError,
    UnitOutcome,
    _git_commit,
    check_manifest,
)
from backtest.cash_interest import (
    accrue_cash_interest,
    describe_cash_interest,
    load_repo_rate_schedule,
)
from backtest.decision_rule import (
    PREREGISTERED_MIN_TRIALS,
    ArmFolds,
    FoldResult,
    TrialSharpe,
    evaluate,
    render_decision,
    trial_sharpe_variance,
)
from backtest.folds import FOLDS_PATH, FoldPlan, load_folds
from backtest.nav import (
    after_tax_nav,
    after_tax_nav_file,
    daily_returns,
    nav_file,
    read_nav,
    write_nav,
)
from backtest.run import _L1Reader
from backtest.run_ledger import (
    ledger_path,
    load_run,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
)
from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    Arm,
    l1_grandfathering,
    run_digests,
    run_sweep,
)
from backtest.tax import (
    GrandfatheringPrices,
    InvestorProfile,
    PaymentTiming,
    TaxError,
    compute_after_tax,
    load_tax_schedule,
)
from backtest.windows import Window, WindowError, first_full_lookback_session, load_windows
from dataplatform.logging import get_logger

__all__ = [
    "BASELINE_LABELS",
    "FLOOR",
    "FROZEN_NAME",
    "PREREGISTRATION_PATH",
    "PROFILE",
    "TRIAL_SHARPES_NAME",
    "FoldCampaignError",
    "FoldRunPlan",
    "FoldWindow",
    "Pinned",
    "TrialSharpeSet",
    "baseline_plan",
    "build_arm_folds",
    "freeze_baseline",
    "load_trial_sharpes",
    "main",
    "round1_labels",
    "round2_plan",
    "run_fold_units",
    "verify_frozen",
    "write_trial_sharpes",
]

_LOG = get_logger(__name__)

#: The two baseline arms, by their sweep labels (§2). Resolved from ``backtest.sweep.ARMS``.
BASELINE_LABELS: tuple[str, ...] = ("Swing composite (M10.7)", "M10.7 + regime gate")
#: §2: the ₹10 crore/day floor decides; ₹1 crore decides nothing and is not run here.
FLOOR = HIGH_FLOOR
#: §2: a resident individual at the 30 % slab, no surcharge, tax paid at FY end.
PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)
PREREGISTRATION_PATH = (
    Path(__file__).resolve().parent.parent / "ops/studies/preregistration-signals-2026-09-29.md"
)
FROZEN_NAME = "frozen-baseline.json"
TRIAL_SHARPES_NAME = "trial-sharpes.json"
#: The sweep family the round-2 hypotheses carry; every other non-baseline arm is a round-1 trial.
_HYPOTHESIS_FAMILY = "round-2 hypothesis"
_TEST, _SELECTION = "test", "selection"


class FoldCampaignError(RuntimeError):
    """The fold campaign cannot start as asked — fails loud, before any run."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class FoldWindow:
    """One window a fold campaign runs: which fold, which role, and its dates."""

    fold: str
    role: str
    window: Window


@dataclass(frozen=True, slots=True)
class FoldRunPlan:
    """What one fold-campaign command runs and where it keeps it."""

    out_dir: Path
    arms: tuple[Arm, ...]
    windows: tuple[FoldWindow, ...]
    data_root: Path | None
    #: Corporate actions in the book. On for every real run; tests switch it off.
    book_actions: bool = True

    @property
    def units(self) -> tuple[int, ...]:
        """Window indices, longest window first so two workers finish close together."""
        spans = [(w.window.end - w.window.start).days for w in self.windows]
        return tuple(sorted(range(len(self.windows)), key=lambda i: (-spans[i], i)))


@dataclass(frozen=True, slots=True)
class Pinned:
    """The facts a frozen record is made under, read once by the CLI (tests pass their own)."""

    commit: str
    data_root: str
    lake_last_session: date


def _resolve(labels: Sequence[str]) -> tuple[Arm, ...]:
    by_label = {arm.label: arm for arm in ARMS}
    unknown = [label for label in labels if label not in by_label]
    if unknown:
        raise FoldCampaignError(f"no sweep arm labelled {', '.join(map(repr, unknown))}")
    return tuple(by_label[label] for label in labels)


def _windows(folds: FoldPlan, *, selection: bool) -> tuple[FoldWindow, ...]:
    out = [FoldWindow(f.name, _TEST, f.test) for f in folds.folds]
    if selection:
        out += [FoldWindow(f.name, _SELECTION, f.selection) for f in folds.folds]
    return tuple(out)


def baseline_plan(
    out_dir: Path, folds: FoldPlan, *, data_root: Path | None, book_actions: bool = True
) -> FoldRunPlan:
    """The baseline-folds plan: the two baseline arms on every test and selection window."""
    return FoldRunPlan(
        out_dir=out_dir,
        arms=_resolve(BASELINE_LABELS),
        windows=_windows(folds, selection=True),
        data_root=data_root,
        book_actions=book_actions,
    )


def round2_plan(
    out_dir: Path,
    folds: FoldPlan,
    labels: Sequence[str],
    *,
    data_root: Path | None,
    book_actions: bool = True,
) -> FoldRunPlan:
    """The round2-signals plan: the named H-arms on the fold test windows only.

    Refuses a baseline arm among ``labels`` (it is frozen, not re-run), an unknown label, a
    duplicate, or an empty list.
    """
    if not labels:
        raise FoldCampaignError("name at least one H-arm to run (--arms)")
    if len(set(labels)) != len(labels):
        raise FoldCampaignError("an H-arm is named twice")
    frozen = [label for label in labels if label in BASELINE_LABELS]
    if frozen:
        raise FoldCampaignError(f"{', '.join(frozen)} is a frozen baseline arm, not an H-arm")
    return FoldRunPlan(
        out_dir=out_dir,
        arms=_resolve(labels),
        windows=_windows(folds, selection=False),
        data_root=data_root,
        book_actions=book_actions,
    )


# ── running ────────────────────────────────────────────────────────────────────────────────────


def _contexts(stack: ExitStack, plan: FoldRunPlan, actions: BookActionSource | None) -> None:
    """Put in force what every run and every digest of ``plan`` is made under."""
    stack.enter_context(book_corporate_actions(actions if plan.book_actions else None))
    stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))


def _actions(plan: FoldRunPlan) -> BookActionSource | None:
    return load_store_book_actions() if plan.book_actions else None


def run_fold_unit(plan: FoldRunPlan, index: int) -> UnitOutcome:
    """Run (or resume) every arm of ``plan`` on its ``index``-th window. Module-level for spawn."""
    target = plan.windows[index]
    with ExitStack() as stack:
        _contexts(stack, plan, _actions(plan))
        stack.enter_context(persist_run_ledgers(plan.out_dir))
        result = run_sweep(
            start=target.window.start,
            end=target.window.end,
            arms=plan.arms,
            floors=(FLOOR,),
            data_root=plan.data_root,
        )
    failed = sum(1 for row in result.rows if not row.ok)
    name = f"{target.fold}-{target.role}"
    outcome = UnitOutcome(name, len(result.rows) - result.resumed - failed, result.resumed, failed)
    _LOG.info("fold_campaign.unit_done", unit=name, replayed=outcome.replayed, failed=failed)
    return outcome


FoldUnitRunner = Callable[[FoldRunPlan, int], UnitOutcome]


def run_fold_units(
    plan: FoldRunPlan, *, workers: int, unit_runner: FoldUnitRunner = run_fold_unit
) -> list[UnitOutcome]:
    """Every window of ``plan``, at most :data:`MAX_WORKERS` at a time; resumes by construction."""
    if not 1 <= workers <= MAX_WORKERS:
        raise FoldCampaignError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    _LOG.info("fold_campaign.start", out=str(plan.out_dir), windows=len(plan.windows))
    if workers == 1:
        return [unit_runner(plan, index) for index in plan.units]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(unit_runner, plan, index) for index in plan.units]
        return [future.result() for future in futures]


def _digests(
    plan: FoldRunPlan, actions: BookActionSource | None
) -> dict[tuple[str, str, str], str]:
    """``(arm label, fold, role) -> digest`` for every run of ``plan``, under its contexts."""
    out: dict[tuple[str, str, str], str] = {}
    with ExitStack() as stack:
        _contexts(stack, plan, actions)
        for target in plan.windows:
            digests = run_digests(
                start=target.window.start, end=target.window.end, arms=plan.arms, floors=(FLOOR,)
            )
            for arm in plan.arms:
                out[(arm.label, target.fold, target.role)] = digests[(arm.label, FLOOR)]
    return out


def _run_files(out_dir: Path, digest: str) -> dict[str, Path]:
    return {
        "summary": summary_path(out_dir, digest),
        "ledger": ledger_path(out_dir, digest),
        "nav": nav_file(out_dir, digest),
    }


# ── the frozen record ──────────────────────────────────────────────────────────────────────────


def _pin_document(pinned: Pinned) -> dict[str, Any]:
    return {
        "version": 1,
        "commit": pinned.commit,
        "data_root": pinned.data_root,
        "lake_last_session": pinned.lake_last_session.isoformat(),
        "folds_sha256": _sha256(FOLDS_PATH),
        "preregistration_sha256": _sha256(PREREGISTRATION_PATH),
        "floor": str(FLOOR),
        "cash_interest": load_repo_rate_schedule().identity(),
    }


def freeze_baseline(
    plan: FoldRunPlan, pinned: Pinned, actions: BookActionSource | None
) -> dict[str, Any]:
    """Write ``frozen-baseline.json`` for a finished baseline-folds plan and return it.

    Raises ``FoldCampaignError`` if any run is missing a summary, ledger or NAV file (nothing is
    frozen from a partial campaign), or if a frozen record already on disk differs from this one
    (a frozen record is never rewritten; an identical one is left as it is).
    """
    runs: list[dict[str, str]] = []
    missing: list[str] = []
    for (label, fold, role), digest in sorted(_digests(plan, actions).items()):
        files = _run_files(plan.out_dir, digest)
        absent = [kind for kind, path in files.items() if not path.is_file()]
        if absent:
            missing.append(f"{label} {fold} {role} ({', '.join(absent)})")
            continue
        summary = json.loads(files["summary"].read_text(encoding="utf-8"))
        runs.append(
            {
                "arm": label,
                "fold": fold,
                "role": role,
                "digest": digest,
                "replay_digest": str(summary["replay_digest"]),
                **{f"{kind}_sha256": _sha256(path) for kind, path in files.items()},
            }
        )
    if missing:
        raise FoldCampaignError(
            f"cannot freeze: {len(missing)} baseline run(s) incomplete: {'; '.join(missing[:6])}"
        )
    document = {
        **_pin_document(pinned),
        "book_corporate_actions": plan.book_actions,
        "arms": [arm.label for arm in plan.arms],
        "runs": runs,
    }
    path = plan.out_dir / FROZEN_NAME
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != document:
            raise FoldCampaignError(
                f"{path} already records a different frozen baseline; it is never rewritten"
            )
        return document
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _LOG.info("fold_campaign.frozen", path=str(path), runs=len(runs))
    return document


def verify_frozen(
    baseline_dir: Path,
    pinned: Pinned,
    folds: FoldPlan,
    actions: BookActionSource | None,
    *,
    book_actions: bool = True,
) -> dict[str, Any]:
    """The frozen record in ``baseline_dir``, or ``FoldCampaignError`` naming why it is unusable.

    Refuses: no record; a record made on another lake, other folds, another pre-registration,
    another floor or interest schedule; any baseline digest the current code no longer gives the
    same run; any recorded file missing or with a different hash. The commit is *not* required to
    match — H-arm code lands after the freeze — the digests are what must.
    """
    path = baseline_dir / FROZEN_NAME
    if not path.is_file():
        raise FoldCampaignError(
            f"no frozen baseline at {path}: run `baseline-folds --out {baseline_dir}` first"
        )
    frozen = json.loads(path.read_text(encoding="utf-8"))
    current = _pin_document(pinned)
    differs = sorted(k for k in current if k != "commit" and frozen.get(k) != current[k])
    if frozen.get("book_corporate_actions") != book_actions:
        differs.append("book_corporate_actions")
    if differs:
        raise FoldCampaignError(
            f"the frozen baseline was made under different conditions ({', '.join(differs)})"
        )
    plan = baseline_plan(baseline_dir, folds, data_root=None, book_actions=book_actions)
    expected = _digests(plan, actions)
    recorded = {(r["arm"], r["fold"], r["role"]): r for r in frozen["runs"]}
    problems: list[str] = []
    if set(recorded) != set(expected):
        problems.append("the recorded runs are not the baseline-folds plan's runs")
    for key, digest in sorted(expected.items()):
        record = recorded.get(key)
        if record is None:
            continue
        if record["digest"] != digest:
            problems.append(f"{' '.join(key)}: digest {record['digest'][:12]} != {digest[:12]}")
            continue
        for kind, file in _run_files(baseline_dir, digest).items():
            if not file.is_file():
                problems.append(f"{' '.join(key)}: {kind} file missing")
            elif _sha256(file) != record[f"{kind}_sha256"]:
                problems.append(f"{' '.join(key)}: {kind} file changed since the freeze")
    if problems:
        raise FoldCampaignError(
            "the frozen baseline does not match the current code: " + "; ".join(problems[:6])
        )
    return dict(frozen)


# ── from persisted runs to the decision rule's inputs ──────────────────────────────────────────


def build_arm_folds(
    out_dir: Path,
    arm_label: str,
    digests: dict[tuple[str, str, str], str],
    folds: FoldPlan,
    *,
    fmv: GrandfatheringPrices,
    nav_dir: Path | None,
    role: str = _TEST,
) -> ArmFolds:
    """One arm's :class:`ArmFolds` from its persisted runs under ``out_dir`` (only read).

    Writes each after-tax NAV file under ``nav_dir``, or nowhere when it is ``None`` (a frozen
    or render-only directory is never written to). A fold whose ledger cannot be taxed keeps its
    drawdown and pre-tax returns out of the rule: its after-tax XIRR is ``None`` with the reason,
    and its returns are empty — which the rule reads as a *missing* fold, never a shorter one.
    """
    schedule = load_tax_schedule()
    results: list[FoldResult] = []
    for fold in folds.folds:
        digest = digests[(arm_label, fold.name, role)]
        loaded = load_run(out_dir, digest)
        if loaded is None:
            raise FoldCampaignError(f"{arm_label} {fold.name} {role}: run not on disk ({digest})")
        summary, ledger = loaded
        pre_tax = read_nav(nav_file(out_dir, digest), digest=digest)
        try:
            taxed = compute_after_tax(ledger, PROFILE, schedule=schedule, fmv=fmv)
        except TaxError as error:
            results.append(FoldResult(fold.name, None, summary.max_drawdown, (), str(error)))
            continue
        net = after_tax_nav(pre_tax, taxed.fy_taxes, PROFILE)
        if nav_dir is not None:
            write_nav(net, after_tax_nav_file(nav_dir, digest, PROFILE))
        results.append(
            FoldResult(
                fold=fold.name,
                after_tax_xirr=taxed.after_tax_xirr_realised,
                max_drawdown=summary.max_drawdown,
                after_tax_returns=tuple(daily_returns(net.points)),
                error=taxed.realised_xirr_error,
            )
        )
    return ArmFolds(arm_label, tuple(results))


def _floor_label() -> str:
    return f"₹{FLOOR / Decimal('10000000'):.0f} crore/day"


def _assumptions() -> list[str]:
    return [
        describe_cash_interest(True),
        "Investor: resident individual, 30% slab, no surcharge, tax paid at FY end "
        "(pre-registration §2); after-tax XIRR on realised gains.",
    ]


def render_baseline_report(
    plan: FoldRunPlan, folds: FoldPlan, actions: BookActionSource | None, fmv: GrandfatheringPrices
) -> str:
    """The record of the frozen baseline: each arm's after-tax XIRR and drawdown, per fold/role."""
    digests = _digests(plan, actions)
    lines = [
        "# Round 2 — the frozen baseline on the folds",
        "",
        "*Generated by `python -m backtest.fold_campaign baseline-folds`. Frozen in "
        f"`{FROZEN_NAME}` before any H-arm was run (pre-registration §2).*",
        "",
        f"- Floor: **{_floor_label()}**",
        *(f"- {line}" for line in _assumptions()),
        "",
        "| Arm | Fold | Window | Dates | After-tax XIRR (realised) | Max drawdown | NAV points |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for role in (_TEST, _SELECTION):
        for arm in plan.arms:
            arm_folds = build_arm_folds(
                plan.out_dir, arm.label, digests, folds, fmv=fmv, nav_dir=plan.out_dir, role=role
            )
            for fold, result in zip(folds.folds, arm_folds.folds, strict=True):
                window = fold.test if role == _TEST else fold.selection
                xirr = (
                    f"{result.after_tax_xirr:.2%}"
                    if result.after_tax_xirr is not None
                    else f"n/a ({result.error})"
                )
                lines.append(
                    f"| {arm.label} | {fold.name} | {role} | {window.start} → {window.end} | "
                    f"{xirr} | {result.max_drawdown:.2%} | {len(result.after_tax_returns) + 1} |"
                )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class TrialSharpeSet:
    """Trial Sharpes read from a ``trial-sharpes`` file, with where they came from."""

    source: str
    sharpes: tuple[TrialSharpe, ...]
    excluded: tuple[str, ...]


def round1_labels() -> tuple[str, ...]:
    """The round-1 sweep's arms still in ``ARMS``: neither a baseline nor a round-2 hypothesis."""
    return tuple(
        arm.label
        for arm in ARMS
        if arm.family != _HYPOTHESIS_FAMILY and arm.label not in BASELINE_LABELS
    )


def write_trial_sharpes(
    plan: FoldRunPlan,
    pinned: Pinned,
    folds: FoldPlan,
    actions: BookActionSource | None,
    fmv: GrandfatheringPrices,
) -> Path:
    """``plan.out_dir/trial-sharpes.json``: each arm's Sharpe on its concatenated test returns.

    An arm with no after-tax returns on some fold is listed under ``excluded`` with the folds and
    the reason — it is not scored on the folds it has (that is the comparison §4 does not make).
    """
    digests = _digests(plan, actions)
    trials: list[dict[str, Any]] = []
    excluded: list[str] = []
    for arm in plan.arms:
        arm_folds = build_arm_folds(
            plan.out_dir, arm.label, digests, folds, fmv=fmv, nav_dir=plan.out_dir
        )
        if arm_folds.missing_returns:
            reasons = "; ".join(
                f"{f.fold}: {f.error or 'no after-tax returns'}"
                for f in arm_folds.folds
                if not f.has_returns
            )
            excluded.append(f"{arm.label}: {reasons}")
            continue
        try:
            stats = arm_folds.stats()
        except ValueError as error:
            excluded.append(f"{arm.label}: {error}")
            continue
        trials.append(
            {
                "arm": arm.label,
                "sharpe": stats.sharpe,
                "observations": stats.observations,
                "digests": {f.name: digests[(arm.label, f.name, _TEST)] for f in folds.folds},
            }
        )
    document = {
        **_pin_document(pinned),
        "book_corporate_actions": plan.book_actions,
        "folds": [f.name for f in folds.folds],
        "trials": trials,
        "excluded": excluded,
    }
    path = plan.out_dir / TRIAL_SHARPES_NAME
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_trial_sharpes(
    path: Path, pinned: Pinned, folds: FoldPlan, *, book_actions: bool = True
) -> TrialSharpeSet:
    """The trial Sharpes in a ``trial-sharpes`` file, refused unless struck on these folds.

    Refuses a file made on another lake, other folds, another pre-registration, floor or interest
    schedule (the commit may differ, as for the frozen baseline), or with a non-finite Sharpe.
    """
    if not path.is_file():
        raise FoldCampaignError(f"no trial-sharpes file at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    current = _pin_document(pinned)
    differs = sorted(k for k in current if k != "commit" and document.get(k) != current[k])
    if document.get("book_corporate_actions") != book_actions:
        differs.append("book_corporate_actions")
    if document.get("folds") != [f.name for f in folds.folds]:
        differs.append("folds")
    if differs:
        raise FoldCampaignError(
            f"{path} was struck under different conditions ({', '.join(sorted(set(differs)))})"
        )
    sharpes = tuple(TrialSharpe(str(t["arm"]), float(t["sharpe"])) for t in document["trials"])
    return TrialSharpeSet(
        source=f"{path.name} @ {str(document['commit'])[:12]}",
        sharpes=sharpes,
        excluded=tuple(str(e) for e in document.get("excluded", [])),
    )


def render_round2(
    plan: FoldRunPlan,
    baseline_dir: Path,
    folds: FoldPlan,
    actions: BookActionSource | None,
    fmv: GrandfatheringPrices,
    *,
    baseline_label: str,
    trials: int,
    trial_sharpes: TrialSharpeSet | None = None,
    nav_dir: Path | None = None,
) -> str:
    """The round-2 decision: every H-arm against the named frozen baseline arm (§4).

    H-arm runs are read from ``plan.out_dir``; their after-tax NAVs are written under ``nav_dir``
    (default ``plan.out_dir``). ``baseline_dir`` is only read. The Sharpe variance pools every
    evaluated arm with all its folds and ``trial_sharpes``, and the report names its source and n.
    """
    if baseline_label not in BASELINE_LABELS:
        raise FoldCampaignError(
            f"--baseline must name a frozen baseline arm ({', '.join(BASELINE_LABELS)})"
        )
    frozen = baseline_plan(baseline_dir, folds, data_root=None, book_actions=plan.book_actions)
    base_digests = _digests(frozen, actions)
    cand_digests = _digests(plan, actions)
    baselines = [
        build_arm_folds(baseline_dir, label, base_digests, folds, fmv=fmv, nav_dir=None)
        for label in BASELINE_LABELS
    ]
    written = plan.out_dir if nav_dir is None else nav_dir
    candidates = [
        build_arm_folds(plan.out_dir, arm.label, cand_digests, folds, fmv=fmv, nav_dir=written)
        for arm in plan.arms
    ]
    arms = [*baselines, *candidates]
    variance = trial_sharpe_variance(
        arms,
        supplied=trial_sharpes.sharpes if trial_sharpes else (),
        supplied_source=trial_sharpes.source if trial_sharpes else None,
    )
    if trial_sharpes and trial_sharpes.excluded:
        variance = replace(variance, excluded=(*variance.excluded, *trial_sharpes.excluded))
    named = next(b for b in baselines if b.label == baseline_label)
    verdicts = [evaluate(c, named, trials=trials, sharpe_variance=variance) for c in candidates]
    return render_decision(
        verdicts,
        arms,
        trials=trials,
        sharpe_variance=variance,
        floor_label=_floor_label(),
        assumptions=_assumptions(),
    )


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.fold_campaign",
        description="Round-2 fold campaign: freeze the baseline, then judge H-arms against it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("baseline-folds", help="run and freeze the two baseline arms")
    base.add_argument("--out", type=Path, required=True, help="baseline directory (not data/)")
    base.add_argument(
        "--smoke",
        action="store_true",
        help="run only Swing composite (M10.7) on F1's test window; freezes nothing, reports "
        "nothing (a smoke directory resumes into a full run: the digests are the same)",
    )
    h = sub.add_parser("round2-signals", help="run H-arms against the frozen baseline")
    h.add_argument("--baseline-dir", type=Path, required=True)
    h.add_argument("--out", type=Path, required=True, help="round-2 directory (not data/)")
    render = sub.add_parser(
        "render-round2", help="re-render the round-2 decision from runs on disk; replays nothing"
    )
    render.add_argument("--runs-dir", type=Path, required=True, help="a round2-signals --out")
    render.add_argument(
        "--runs-from-commit", required=True, help="the commit --runs-dir's runs were made at"
    )
    render.add_argument("--out", type=Path, required=True, help="where the report goes (new)")
    for p in (h, render):
        p.add_argument("--arms", required=True, help="comma-separated H-arm sweep labels")
        p.add_argument("--baseline", required=True, help=f"one of: {', '.join(BASELINE_LABELS)}")
        p.add_argument(
            "--trials",
            type=int,
            required=True,
            help=f"the recorded trial count (pre-registration §2; at least "
            f"{PREREGISTERED_MIN_TRIALS})",
        )
        p.add_argument(
            "--trial-sharpes",
            type=Path,
            default=None,
            help="a trial-sharpes file (round-1 arms on these folds) pooled into the Sharpe "
            "variance criterion 4 deflates with",
        )
    render.add_argument("--baseline-dir", type=Path, required=True)
    t = sub.add_parser("trial-sharpes", help="run round-1 arms on the folds; write their Sharpes")
    t.add_argument("--out", type=Path, required=True, help="trial-sharpes directory (not data/)")
    t.add_argument(
        "--arms",
        default=None,
        help="comma-separated sweep labels (default: every round-1 arm, baselines excluded)",
    )
    for p in (base, h, render, t):
        p.add_argument("--data-root", type=Path, default=None)
    for p in (base, h, t):
        p.add_argument(
            "--workers", type=int, required=True, help=f"worker processes, 1..{MAX_WORKERS}"
        )
    return parser.parse_args(argv)


def _pinned(data_root: Path | None, folds: FoldPlan) -> Pinned:
    """Read the commit and the lake's last session; check every fold window's lookback."""
    reader = _L1Reader(data_root=data_root)
    try:
        sessions = reader.all_sessions()
    finally:
        reader.close()
    first, binding = first_full_lookback_session(sessions, load_windows().joinable_from)
    for fold in folds.folds:
        for window in (fold.selection, fold.test):
            opened = next((s for s in sessions if s >= window.start), None)
            if opened is None or opened < first:
                raise WindowError(
                    f"{window.name} opens on {opened}, before the first full-lookback session "
                    f"{first} ({binding.what})"
                )
    return Pinned(
        commit=_git_commit(),
        data_root=str(data_root.resolve()) if data_root else "(configured)",
        lake_last_session=sessions[-1],
    )


def _resume_manifest(pinned: Pinned, command: str) -> dict[str, Any]:
    return {**_pin_document(pinned), "command": command}


def _labels(arms: str) -> list[str]:
    return [part.strip() for part in arms.split(",") if part.strip()]


def _absent_runs(plan: FoldRunPlan, actions: BookActionSource | None) -> list[str]:
    """Every run of ``plan`` without its summary, ledger and NAV on disk, as ``arm fold role``."""
    return [
        " ".join(key)
        for key, digest in sorted(_digests(plan, actions).items())
        if not all(path.is_file() for path in _run_files(plan.out_dir, digest).values())
    ]


def _print_outcomes(outcomes: Sequence[UnitOutcome]) -> int:
    failed = 0
    for outcome in outcomes:
        failed += outcome.failed
        print(
            f"  {outcome.unit:<14} replayed {outcome.replayed:>2}  resumed {outcome.resumed:>2}  "
            f"failed {outcome.failed:>2}"
        )
    return failed


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.fold_campaign``. Returns a process exit code."""
    args = _parse_args(argv)
    folds = load_folds()
    try:
        pinned = _pinned(args.data_root, folds)
        out_dir = refuse_lake_location(args.out, args.data_root)
        check_manifest(out_dir, _resume_manifest(pinned, args.command))
        actions = load_store_book_actions()
        if args.command == "baseline-folds":
            plan = baseline_plan(out_dir, folds, data_root=args.data_root)
            if args.smoke:
                plan = FoldRunPlan(
                    out_dir, plan.arms[:1], plan.windows[:1], args.data_root, plan.book_actions
                )
            if _print_outcomes(run_fold_units(plan, workers=args.workers)):
                raise FoldCampaignError("a baseline run failed; nothing frozen (re-run to retry)")
            if args.smoke:
                digest = next(iter(_digests(plan, actions).values()))
                for kind, path in _run_files(out_dir, digest).items():
                    print(f"  smoke {kind}: {path} ({'ok' if path.is_file() else 'MISSING'})")
                return 0
            freeze_baseline(plan, pinned, actions)
            print(f"  frozen baseline written to {out_dir / FROZEN_NAME}")
            name, service_fmv = "baseline-folds.md", l1_grandfathering(args.data_root)
            with service_fmv[0]:
                text = render_baseline_report(plan, folds, actions, service_fmv[1])
        elif args.command == "trial-sharpes":
            labels = _labels(args.arms) if args.arms else list(round1_labels())
            plan = round2_plan(out_dir, folds, labels, data_root=args.data_root)
            if _print_outcomes(run_fold_units(plan, workers=args.workers)):
                raise FoldCampaignError("a trial run failed; no trial Sharpes written (re-run)")
            service_fmv = l1_grandfathering(args.data_root)
            with service_fmv[0]:
                path = write_trial_sharpes(plan, pinned, folds, actions, service_fmv[1])
            print(f"  trial Sharpes written to {path}")
            return 0
        else:
            baseline_dir = args.baseline_dir.resolve()
            if baseline_dir == out_dir:
                raise FoldCampaignError(
                    "--out must differ from --baseline-dir; the freeze is read-only"
                )
            if args.trials < PREREGISTERED_MIN_TRIALS:
                raise FoldCampaignError(
                    f"--trials is at least {PREREGISTERED_MIN_TRIALS} (pre-registration §2)"
                )
            if args.baseline not in BASELINE_LABELS:
                raise FoldCampaignError(f"--baseline must be one of: {', '.join(BASELINE_LABELS)}")
            verify_frozen(baseline_dir, pinned, folds, actions)
            trial_sharpes = (
                load_trial_sharpes(args.trial_sharpes, pinned, folds)
                if args.trial_sharpes is not None
                else None
            )
            labels = _labels(args.arms)
            if args.command == "render-round2":
                runs_dir = args.runs_dir.resolve()
                if out_dir in (runs_dir, baseline_dir):
                    raise FoldCampaignError("render-round2 --out must be a new directory")
                check_manifest(
                    runs_dir,
                    _resume_manifest(pinned, "round2-signals"),
                    runs_from_commit=args.runs_from_commit,
                )
                plan = round2_plan(runs_dir, folds, labels, data_root=args.data_root)
                absent = _absent_runs(plan, actions)
                if absent:
                    raise FoldCampaignError(
                        "render-round2 replays nothing; not on disk: " + "; ".join(absent[:6])
                    )
            else:
                plan = round2_plan(out_dir, folds, labels, data_root=args.data_root)
                if _print_outcomes(run_fold_units(plan, workers=args.workers)):
                    raise FoldCampaignError("an H-arm run failed; no decision rendered (re-run)")
            name, service_fmv = "round2-decision.md", l1_grandfathering(args.data_root)
            with service_fmv[0]:
                text = render_round2(
                    plan,
                    baseline_dir,
                    folds,
                    actions,
                    service_fmv[1],
                    baseline_label=args.baseline,
                    trials=args.trials,
                    trial_sharpes=trial_sharpes,
                    nav_dir=out_dir,
                )
    except (FoldCampaignError, CampaignError, WindowError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    report = out_dir / "reports" / name
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    print(f"  report written to {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
