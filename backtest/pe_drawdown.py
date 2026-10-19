"""PE-drawdown study: is "price regime AND P/E below its 63-session mean" a real low-drawdown D13?

``ops/studies/preregistration-pe-drawdown-2026-10-19.md`` fixes the question, the comparators, the
robustness variants and the drawdown-first rule (``backtest.drawdown_rule``) before any run. The
PE-timing study (PR #127) found the arm "D13 + price regime AND PE<63d mean" (A3-63) had the best
XIRR/DD of its 24 arms while sitting in cash most of the time; it was picked *after* that result was
seen, so this study asks whether the drawdown cut is the P/E's or just the cash's.

Runs, in order, every one resumable and persisted under ``--out``:

* **phase 0** — D13 on every window and floor (runs whose digest the parent study already holds on
  disk are copied from it, not replayed: same code and data give the same digest), frozen into
  ``frozen-baseline.json`` and compared with the parent's frozen record;
* **phase 1** — the candidate, the fixed comparators (D13 + the M9.5 15 % vol target, the stricter
  price-only regime, the price-only analogue of the P/E rule) and the robustness variants;
* **calibration** — from phase 1's runs and nothing else: the equal-exposure fraction per window
  and floor, and the tighter vol target (``calibration.json``, written once);
* **phase 2** — equal-exposure D13 and D13 + the tighter vol target.

``python -m backtest.pe_drawdown run --out DIR --workers N [--signal-panel P] [--reuse-from D]
[--reference-frozen F]`` — every phase.

``python -m backtest.pe_drawdown render --out DIR --trials N --trial-sharpes FILE --parent-dir D``
— replays nothing: writes ``DIR/reports/decision.md``.

What it never does: write under the lake, run a comparator before the baseline is frozen, choose a
calibration from a return, tune a parameter, or round a figure before the rule compares it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Final

from backtest.book_actions import corporate_actions_in_force, load_store_book_actions
from backtest.campaign import MAX_WORKERS, CampaignError, _git_commit
from backtest.cash_interest import (
    RepoRateSchedule,
    accrue_cash_interest,
    describe_cash_interest,
    load_repo_rate_schedule,
)
from backtest.drawdown_rule import (
    ArmWindows,
    DrawdownVerdict,
    RatioVariance,
    TrialRatio,
    WindowFigures,
    beats_on_ratio,
    calmar,
    evaluate_drawdown_first,
    ratio_variance,
    robust_pass,
    study_verdict,
)
from backtest.fold_campaign import CONTINUOUS, FoldWindow, continuous_fold, continuous_window
from backtest.folds import FOLDS_PATH, FoldPlan, load_folds
from backtest.ma_timing import Cadence, CostMode, TimingRule, after_tax, load_index, simulate
from backtest.nav import daily_returns, nav_file, read_nav
from backtest.pe_timing import _facts, _RunFacts
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    ValuationGate,
    d13_valuation_gate,
)
from backtest.run import (
    UNIVERSE_TURNOVER_FLOOR,
    _first_session_of_each_month,
    _L1Reader,
    _RegimeSource,
)
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
    PE_TIMING_ARMS,
    Arm,
    l1_grandfathering,
    pe_timing_label,
    run_digests,
    run_sweep,
)
from backtest.tax import GrandfatheringPrices
from backtest.tax_report import read_run_ledger
from backtest.valuation_gate import CLOSE_SERIES, NIFTY50_PE_SERIES, ValuationSource
from backtest.windows import Window
from backtest.xirr import Cashflow, xirr
from dataplatform.logging import get_logger
from execution.broker import Side
from execution.costs import CostModel, load_rate_card

__all__ = [
    "BAND",
    "BASELINE",
    "CANDIDATE",
    "DECIDING_FLOOR",
    "EQUAL_EXPOSURE_LABEL",
    "FLOORS",
    "PREREGISTRATION_PATH",
    "StudyError",
    "equal_exposure_arm",
    "equal_exposure_fraction",
    "invested_fractions",
    "main",
    "mix_path",
    "tighter_vol_target",
    "vol_target_arm",
]

_LOG = get_logger(__name__)

BASELINE: Arm = D13_PAPER_BASELINE
DECIDING_FLOOR: Final = HIGH_FLOOR
FLOORS: tuple[Decimal, ...] = (HIGH_FLOOR, LOW_FLOOR)
PREREGISTRATION_PATH = (
    Path(__file__).resolve().parent.parent / "ops/studies/preregistration-pe-drawdown-2026-10-19.md"
)
FROZEN_NAME = "frozen-baseline.json"
CALIBRATION_NAME = "calibration.json"
_TEST = "test"
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_YEAR: Final = Decimal("365")

# ── arms (pre-registration §3) ─────────────────────────────────────────────────────────────────


def _pe_arm(days: int, *, splice: bool = False) -> Arm:
    label = pe_timing_label(ValuationGate.BOTH, days, splice=splice)
    for arm in PE_TIMING_ARMS:
        if arm.label == label:
            return arm
    return Arm(
        label=label,
        family="pe drawdown (robustness)",
        reference=pe_timing_label(ValuationGate.BOTH, 63, splice=False),
        note=f"the candidate with a {days}-session P/E mean in place of 63",
        v2=d13_valuation_gate(ValuationGate.BOTH, days, splice=splice),
    )


#: The candidate: the PE-timing study's A3 at 63 sessions, raw P/E — the same arm, the same digest.
CANDIDATE: Final = _pe_arm(63)
#: Comparator 3a: the M9.5 overlay already in the code — D13 plus a 15 % volatility target. The
#: same parameters, and label, as ``backtest.sweep``'s "Momentum v2, all on (M9.5)" round-1 arm.
VOL_TARGET_15: Final = Arm(
    label="Momentum v2, all on (M9.5)",
    family="pe drawdown (comparator)",
    reference=D13_PAPER_BASELINE.label,
    note="D13 + the M9.5 15% volatility target (rho 0.3)",
    v2=replace(PAPER_RATIFIED_2026_09_06, vol_target_annual=Decimal("0.15")),
)
#: Comparator 4: a stricter price-only monthly regime — held only at or above 1.02 x the 200d MA.
BAND_RATIO: Final = Decimal("0.02")
BAND: Final = Arm(
    label="D13, price regime only at >= 1.02 x 200d MA",
    family="pe drawdown (comparator)",
    reference=D13_PAPER_BASELINE.label,
    note="D13's monthly regime made stricter: invested only while NIFTY 50 TRI >= 1.02 x its 200d "
    "MA (both exits and re-entries); price only",
    v2=replace(PAPER_RATIFIED_2026_09_06, regime_monthly_band=BAND_RATIO),
)
#: Comparator 5 (added by this study): the candidate's rule with the NIFTY 500 *close* in place of
#: its P/E — the same snapshot, the same 63-session mean, the same AND with the price regime.
ANALOGUE: Final = Arm(
    label="D13 + price regime AND NIFTY 500 < its 63d mean",
    family="pe drawdown (comparator)",
    reference=CANDIDATE.label,
    note="the candidate's rule on the NIFTY 500 close instead of its P/E (price-only analogue)",
    v2=d13_valuation_gate(ValuationGate.BOTH, 63, series=CLOSE_SERIES),
)
NEIGHBOURS: Final = (_pe_arm(42), _pe_arm(84), _pe_arm(126))
NIFTY50: Final = Arm(
    label="D13 + price regime AND NIFTY 50 PE<63d mean",
    family="pe drawdown (robustness)",
    reference=CANDIDATE.label,
    note="the candidate on the NIFTY 50 P/E (raw) instead of the NIFTY 500's",
    v2=d13_valuation_gate(ValuationGate.BOTH, 63, series=NIFTY50_PE_SERIES),
)
SPLICED: Final = _pe_arm(63, splice=True)
ROBUSTNESS: Final = (*NEIGHBOURS, NIFTY50, SPLICED)

EQUAL_EXPOSURE_LABEL: Final = "Equal-exposure D13 (constant mix)"
VOL_TARGET_TIGHT_LABEL: Final = "D13 + tighter vol target"
#: Comparator 3b's rule: 15 % scaled by the candidate's over VT15's average invested fraction.
VOL_TARGET_REFERENCE: Final = Decimal("0.15")
_FRACTION_QUANTUM: Final = Decimal("0.001")
_TARGET_QUANTUM: Final = Decimal("0.005")


def equal_exposure_arm(fraction: Decimal | None) -> Arm:
    """Comparator 2: D13 with its basket capped at ``fraction`` of capital (``None``: D13)."""
    v2 = (
        PAPER_RATIFIED_2026_09_06
        if fraction is None
        else replace(PAPER_RATIFIED_2026_09_06, exposure_fraction=fraction)
    )
    return Arm(
        label=EQUAL_EXPOSURE_LABEL,
        family="pe drawdown (comparator)",
        reference=D13_PAPER_BASELINE.label,
        note="D13 held at a constant share of capital that matches the candidate's average "
        f"invested fraction ({'1' if fraction is None else fraction}), the rest in cash",
        v2=v2,
    )


def vol_target_arm(target: Decimal) -> Arm:
    """Comparator 3b: D13 with the M9.5 volatility target at ``target`` instead of 15 %."""
    return Arm(
        label=VOL_TARGET_TIGHT_LABEL,
        family="pe drawdown (comparator)",
        reference=VOL_TARGET_15.label,
        note=f"D13 + a {target:.1%} volatility target (rho 0.3), calibrated to the candidate's "
        "exposure",
        v2=replace(PAPER_RATIFIED_2026_09_06, vol_target_annual=target),
    )


class StudyError(RuntimeError):
    """The study cannot proceed as asked — fails loud, before any run or render."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── windows (pre-registration §4) ──────────────────────────────────────────────────────────────

MANDATE: Final = FoldWindow("M6", "mandate", Window("M6", date(2019, 7, 1), date(2026, 8, 31)))
#: The out-of-sample window: the longest span before 2016-09 whose every first-of-month session
#: has a published P/E and a full 63-session mean (2015-02-02 and 2015-12-01 have none).
OUT_OF_SAMPLE: Final = FoldWindow(
    "O1", "out-of-sample", Window("O1", date(2013, 2, 1), date(2015, 1, 30))
)


def study_windows(folds: FoldPlan) -> tuple[FoldWindow, ...]:
    """F1-F3 test windows, their continuous union, the mandate window and the OOS window."""
    return (
        *(FoldWindow(f.name, _TEST, f.test) for f in folds.folds),
        FoldWindow(continuous_fold(folds), CONTINUOUS, continuous_window(folds)),
        MANDATE,
        OUT_OF_SAMPLE,
    )


def _phase1_arms(window: FoldWindow) -> tuple[Arm, ...]:
    fixed = (CANDIDATE, VOL_TARGET_15, BAND, ANALOGUE)
    return fixed if window.fold == OUT_OF_SAMPLE.fold else (*fixed, *ROBUSTNESS)


# ── units, reuse, runs ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Unit:
    """One (window, floor) pair and the arms run on it."""

    window: FoldWindow
    floor: Decimal
    arms: tuple[Arm, ...]

    @property
    def name(self) -> str:
        return f"{self.window.fold}-{self.window.role}@{self.floor}"


@dataclass(frozen=True, slots=True)
class Plan:
    out_dir: Path
    data_root: Path | None
    signal_panel: Path | None
    reuse_from: tuple[Path, ...] = ()
    universe: str = UNIVERSE_TURNOVER_FLOOR


@contextmanager
def _contexts(plan: Plan) -> Iterator[None]:
    """The run conditions every run and digest of the study is made under (as the parent's)."""
    with ExitStack() as stack:
        stack.enter_context(
            corporate_actions_in_force(
                load_store_book_actions(data_root=plan.data_root), apply_to_book=True
            )
        )
        stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))
        yield


def _files(out_dir: Path, digest: str) -> dict[str, Path]:
    return {
        "summary": summary_path(out_dir, digest),
        "ledger": ledger_path(out_dir, digest),
        "nav": nav_file(out_dir, digest),
    }


def unit_digests(plan: Plan, units: Sequence[Unit]) -> dict[tuple[str, str, str], str]:
    """``(arm label, fold, floor) -> digest`` for every run of ``units``."""
    out: dict[tuple[str, str, str], str] = {}
    with _contexts(plan):
        for unit in units:
            digests = run_digests(
                start=unit.window.window.start,
                end=unit.window.window.end,
                arms=unit.arms,
                floors=(unit.floor,),
                universe_name=plan.universe,
            )
            for arm in unit.arms:
                out[(arm.label, unit.window.fold, str(unit.floor))] = digests[
                    (arm.label, unit.floor)
                ]
    return out


def reuse_runs(plan: Plan, digests: Mapping[tuple[str, str, str], str]) -> list[str]:
    """Copy into ``out_dir`` every planned run a ``reuse_from`` directory already finished.

    A run's digest is its spec (code path, parameters, window, floor, the book's corporate-action
    calendar, cash interest): an equal digest is the same run on the same data. Returns one line
    per copied run.
    """
    copied: list[str] = []
    for (label, fold, floor), digest in sorted(digests.items()):
        if load_run(plan.out_dir, digest) is not None:
            continue
        for source in plan.reuse_from:
            theirs = _files(source, digest)
            if all(p.is_file() for p in theirs.values()):
                for kind, path in theirs.items():
                    target = _files(plan.out_dir, digest)[kind]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                copied.append(f"{label} {fold} @{floor}: {digest[:12]} from {source}")
                break
    return copied


def run_unit(plan: Plan, unit: Unit) -> tuple[str, int]:
    """Run (or resume) every arm of ``unit``; ``(unit name, failed runs)``."""
    panel = SignalPanel(plan.signal_panel) if plan.signal_panel is not None else None
    with ExitStack() as stack:
        stack.enter_context(_contexts(plan))
        stack.enter_context(persist_run_ledgers(plan.out_dir))
        stack.enter_context(signal_panel_in_force(panel))
        result = run_sweep(
            start=unit.window.window.start,
            end=unit.window.window.end,
            arms=unit.arms,
            floors=(unit.floor,),
            data_root=plan.data_root,
            universe_name=plan.universe,
        )
    failed = sum(1 for row in result.rows if not row.ok)
    for row in result.rows:
        if not row.ok:
            _LOG.error("pe_drawdown.run_failed", arm=row.arm.label, unit=unit.name, error=row.error)
    _LOG.info("pe_drawdown.unit_done", unit=unit.name, failed=failed)
    return unit.name, failed


def run_units(plan: Plan, units: Sequence[Unit], *, workers: int) -> int:
    """Every unit, longest window first; returns the number of failed runs."""
    if not 1 <= workers <= MAX_WORKERS:
        raise StudyError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    for line in reuse_runs(plan, unit_digests(plan, units)):
        print(f"  reused {line}")
    ordered = sorted(
        units, key=lambda u: -(u.window.window.end - u.window.window.start).days * len(u.arms)
    )
    if workers == 1:
        outcomes = [run_unit(plan, u) for u in ordered]
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = [pool.submit(run_unit, plan, u) for u in ordered]
            outcomes = [future.result() for future in futures]
    for name, failed in outcomes:
        print(f"  {name:<36} failed {failed}")
    return sum(failed for _, failed in outcomes)


def baseline_units(windows: Sequence[FoldWindow]) -> list[Unit]:
    return [Unit(w, floor, (BASELINE,)) for w in windows for floor in FLOORS]


def phase1_units(windows: Sequence[FoldWindow]) -> list[Unit]:
    return [Unit(w, floor, _phase1_arms(w)) for w in windows for floor in FLOORS]


def phase2_units(windows: Sequence[FoldWindow], calibration: Mapping[str, Any]) -> list[Unit]:
    target = Decimal(calibration["vol_target"])
    out: list[Unit] = []
    for w in windows:
        for floor in FLOORS:
            raw = calibration["fractions"][f"{w.fold}@{floor}"]
            fraction = None if raw is None else Decimal(raw)
            out.append(Unit(w, floor, (equal_exposure_arm(fraction), vol_target_arm(target))))
    return out


# ── freeze ─────────────────────────────────────────────────────────────────────────────────────


def freeze_baseline(
    plan: Plan, units: Sequence[Unit], *, commit: str, lake_last_session: date
) -> dict[str, Any]:
    """Write ``frozen-baseline.json`` for the finished D13 runs; never rewrite a different one."""
    runs: list[dict[str, str]] = []
    missing: list[str] = []
    for (label, fold, floor), digest in sorted(unit_digests(plan, units).items()):
        files = _files(plan.out_dir, digest)
        absent = [kind for kind, path in files.items() if not path.is_file()]
        if absent:
            missing.append(f"{label} {fold} @{floor} ({', '.join(absent)})")
            continue
        role = next(u.window.role for u in units if u.window.fold == fold)
        runs.append(
            {
                "arm": label,
                "fold": fold,
                "role": role,
                "floor": floor,
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
    _LOG.info("pe_drawdown.frozen", path=str(path), runs=len(runs))
    return document


def verify_frozen(plan: Plan, units: Sequence[Unit]) -> dict[str, Any]:
    """The frozen record, or ``StudyError`` if the code, the data or the files moved since."""
    path = plan.out_dir / FROZEN_NAME
    if not path.is_file():
        raise StudyError(f"no frozen baseline at {path}: phase 0 has not finished")
    frozen = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if frozen.get("preregistration_sha256") != _sha256(PREREGISTRATION_PATH):
        problems.append("the pre-registration changed since the freeze")
    if frozen.get("folds_sha256") != _sha256(FOLDS_PATH):
        problems.append("the folds changed since the freeze")
    recorded = {(r["arm"], r["fold"], r["floor"]): r for r in frozen["runs"]}
    for key, digest in unit_digests(plan, units).items():
        record = recorded.get(key)
        if record is None:
            problems.append(f"{key}: not in the frozen record")
        elif record["digest"] != digest:
            problems.append(f"{key}: digest moved (the lake or the code changed)")
        else:
            for kind, file in _files(plan.out_dir, digest).items():
                if not file.is_file() or _sha256(file) != record[f"{kind}_sha256"]:
                    problems.append(f"{key}: {kind} file changed")
    if problems:
        raise StudyError("the frozen baseline does not match: " + "; ".join(problems[:6]))
    return dict(frozen)


def compare_reference(frozen: Mapping[str, Any], reference: Path) -> list[str]:
    """Each D13 run against the parent study's frozen record: identical, differs, or new window."""
    other = json.loads(reference.read_text(encoding="utf-8"))
    theirs = {(r["fold"], r["floor"]): r for r in other["runs"]}
    keys = ("digest", "summary_sha256", "ledger_sha256", "nav_sha256")
    lines: list[str] = []
    for run in frozen["runs"]:
        where = f"{run['fold']} {run['role']} @{run['floor']}"
        match = theirs.get((run["fold"], run["floor"]))
        if match is None:
            lines.append(f"{where}: new window (not in {reference.name})")
            continue
        differ = [k for k in keys if match[k] != run[k]]
        lines.append(
            f"{where}: " + ("identical" if not differ else f"DIFFERS: {', '.join(differ)}")
        )
    return lines


def _lake_last_session(data_root: Path | None) -> date:
    reader = _L1Reader(data_root=data_root)
    try:
        return reader.all_sessions()[-1]
    finally:
        reader.close()


# ── exposure and calibration (pre-registration §3.2, §3.3) ─────────────────────────────────────


def invested_fractions(out_dir: Path, digest: str) -> list[tuple[date, Decimal]]:
    """The run's invested fraction per NAV session: ``1 - cash / NAV``.

    Cash is rebuilt from the ledger alone — the deposit, every fill's net amount on its trade date,
    every dividend and interest credit — so a sale's proceeds count as cash from the fill (the
    engine's NAV carries them as the account's too). Assumes one deposit and no withdrawal, as
    every run here has. Never reads a price.
    """
    ledger = read_run_ledger(ledger_path(out_dir, digest))
    nav = read_nav(nav_file(out_dir, digest), digest=digest).points
    flows: dict[date, Decimal] = {}

    def add(day: date, amount: Decimal) -> None:
        flows[day] = flows.get(day, _ZERO) + amount

    for flow in ledger.external_flows:
        add(flow.when, -flow.amount)
    for trade in ledger.trades:
        add(trade.trade_date, -trade.net_amount if trade.side is Side.BUY else trade.net_amount)
    for dividend in ledger.dividends:
        add(dividend.received, dividend.amount)
    for credit in ledger.interest:
        add(credit.received, credit.amount)
    nav_on = dict(nav)
    cash = _ZERO
    out: list[tuple[date, Decimal]] = []
    for day in sorted(set(flows) | set(nav_on)):
        cash += flows.get(day, _ZERO)
        value = nav_on.get(day)
        if value is not None and value > _ZERO:
            out.append((day, _ONE - cash / value))
    return out


def mean_invested(out_dir: Path, digest: str) -> Decimal:
    """The time-average (over NAV sessions) of :func:`invested_fractions`."""
    points = invested_fractions(out_dir, digest)
    if not points:
        raise StudyError(f"run {digest[:12]} has no NAV to average an exposure over")
    return sum((f for _, f in points), _ZERO) / Decimal(len(points))


def equal_exposure_fraction(candidate: Decimal, baseline: Decimal) -> Decimal | None:
    """§3.2: the cap ``s = e_candidate / e_D13`` to 0.001 (half-even); ``None`` (D13) at >= 1."""
    if baseline <= _ZERO:
        raise StudyError("D13's average invested fraction is not positive")
    fraction = (candidate / baseline).quantize(_FRACTION_QUANTUM, rounding=ROUND_HALF_EVEN)
    if fraction <= _ZERO:
        raise StudyError(f"the equal-exposure fraction is not positive: {fraction}")
    return None if fraction >= _ONE else fraction


def tighter_vol_target(candidate: Decimal, vt15: Decimal) -> Decimal:
    """§3.3: ``0.15 * e_candidate / e_VT15`` to 0.5 pp (half-even); below 15 % or it fails."""
    if vt15 <= _ZERO:
        raise StudyError("VT15's average invested fraction is not positive")
    steps = (VOL_TARGET_REFERENCE * candidate / vt15 / _TARGET_QUANTUM).quantize(
        _ONE, rounding=ROUND_HALF_EVEN
    )
    target = steps * _TARGET_QUANTUM
    if not _ZERO < target < VOL_TARGET_REFERENCE:
        raise StudyError(
            f"the tighter vol target came out at {target}, not below 15%: VT15 already holds no "
            "more than the candidate — comparator 3b is then VT15 itself (report it, do not run)"
        )
    return target


def calibrate(plan: Plan, windows: Sequence[FoldWindow], folds: FoldPlan) -> dict[str, Any]:
    """Strike ``calibration.json`` from phase 0 and 1's runs; never rewrite a different one."""
    digests = unit_digests(plan, [*baseline_units(windows), *phase1_units(windows)])
    exposures: dict[str, str] = {}
    fractions: dict[str, str | None] = {}
    for w in windows:
        for floor in FLOORS:
            e_c = mean_invested(plan.out_dir, digests[(CANDIDATE.label, w.fold, str(floor))])
            e_b = mean_invested(plan.out_dir, digests[(BASELINE.label, w.fold, str(floor))])
            key = f"{w.fold}@{floor}"
            exposures[f"candidate {key}"] = str(e_c)
            exposures[f"D13 {key}"] = str(e_b)
            fraction = equal_exposure_fraction(e_c, e_b)
            fractions[key] = None if fraction is None else str(fraction)
    cont = continuous_fold(folds)
    e_c = mean_invested(plan.out_dir, digests[(CANDIDATE.label, cont, str(DECIDING_FLOOR))])
    e_v = mean_invested(plan.out_dir, digests[(VOL_TARGET_15.label, cont, str(DECIDING_FLOOR))])
    exposures[f"VT15 {cont}@{DECIDING_FLOOR}"] = str(e_v)
    document = {
        "version": 1,
        "fractions": fractions,
        "vol_target": str(tighter_vol_target(e_c, e_v)),
        "exposures": exposures,
    }
    path = plan.out_dir / CALIBRATION_NAME
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != document:
            raise StudyError(f"{path} records a different calibration; it is never rewritten")
        return dict(existing)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _LOG.info("pe_drawdown.calibrated", path=str(path), vol_target=document["vol_target"])
    return document


# ── the analytic equal-exposure mix (robustness rows) ──────────────────────────────────────────


def mix_path(
    nav: Sequence[tuple[date, Decimal]], fraction: Decimal, repo: RepoRateSchedule
) -> list[tuple[date, Decimal]]:
    """D13's NAV path held at ``fraction``, the rest in cash, rebalanced every session.

    Each step earns ``fraction`` x D13's session return plus ``1 - fraction`` x the cash rate
    (repo - 50 bp, Actual/365 over the calendar days of the step). No cost of the daily rebalance,
    no tax: a pre-tax yardstick for the robustness rows, cross-checked against the engine's
    equal-exposure run in the report.
    """
    if not nav:
        return []
    out = [(nav[0][0], nav[0][1])]
    level = nav[0][1]
    for (before, prior), (day, value) in pairwise(nav):
        if prior <= _ZERO:
            raise StudyError(f"non-positive NAV before {day}")
        equity = value / prior - _ONE
        cash = repo.earning_rate(before) * Decimal((day - before).days) / _YEAR
        level = level * (_ONE + fraction * equity + (_ONE - fraction) * cash)
        out.append((day, level))
    return out


def _path_xirr(points: Sequence[tuple[date, Decimal]]) -> Decimal:
    first, last = points[0], points[-1]
    return xirr([Cashflow(first[0], -first[1]), Cashflow(last[0], last[1])])


def _path_drawdown(points: Sequence[tuple[date, Decimal]]) -> Decimal:
    peak = points[0][1]
    worst = _ZERO
    for _, value in points:
        if value > peak:
            peak = value
        elif peak > _ZERO:
            worst = max(worst, _ONE - value / peak)
    return worst


# ── render ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Run:
    """One finished run as the report reads it."""

    label: str
    fold: str
    floor: Decimal
    digest: str
    facts: _RunFacts
    exposure: Decimal
    nav: tuple[tuple[date, Decimal], ...]

    @property
    def ratio(self) -> Decimal | None:
        if self.facts.max_drawdown <= _ZERO:
            return None
        return self.facts.pre_tax_xirr / self.facts.max_drawdown

    def figures(self) -> WindowFigures:
        return WindowFigures(
            window=self.fold,
            pre_tax_xirr=self.facts.pre_tax_xirr,
            max_drawdown=self.facts.max_drawdown,
            after_tax_xirr=self.facts.after_tax_xirr,
            error=self.facts.after_tax_error,
        )


def _years(nav: Sequence[tuple[date, Decimal]]) -> float:
    return (nav[-1][0] - nav[0][0]).days / 365.0


def arm_windows(label: str, runs: Sequence[Run]) -> ArmWindows:
    """``runs`` (one arm, decision windows in order) as the rule reads them."""
    returns: list[float] = []
    years = 0.0
    for run in runs:
        returns += daily_returns(run.nav)
        years += _years(run.nav)
    return ArmWindows(label, tuple(r.figures() for r in runs), tuple(returns), years)


def _scan_trials(
    directory: Path, labels: Mapping[str, str], fold_windows: Sequence[Window]
) -> list[TrialRatio]:
    """Trial ratios of runs under ``directory`` whose parameters ``repr`` is in ``labels``."""
    by_arm: dict[str, dict[date, Path]] = {}
    starts = {w.start for w in fold_windows}
    for summary in sorted((directory / "runs").glob("*.json")):
        doc = json.loads(summary.read_text(encoding="utf-8"))
        spec = doc.get("spec", {})
        label = labels.get(spec.get("parameters", ""))
        if label is None or str(HIGH_FLOOR) not in spec.get("universe", ""):
            continue
        start = date.fromisoformat(spec["start"])
        if start in starts and any(date.fromisoformat(spec["end"]) == w.end for w in fold_windows):
            by_arm.setdefault(label, {})[start] = directory / "navs" / f"{doc['digest']}.json"
    out: list[TrialRatio] = []
    for label, files in sorted(by_arm.items()):
        if len(files) != len(fold_windows):
            continue
        returns: list[float] = []
        years = 0.0
        for w in fold_windows:
            points = read_nav(files[w.start]).points
            returns += daily_returns(points)
            years += _years(points)
        out.append(TrialRatio(f"{label} (PE timing)", calmar(returns, years=years)))
    return out


def _supplied_trials(path: Path) -> tuple[list[TrialRatio], str]:
    """Round-1 trial ratios from the NAVs named by a ``trial-sharpes.json`` (pre-tax, F1-F3)."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("floor") != str(DECIDING_FLOOR):
        raise StudyError(f"{path} was struck at another floor")
    out: list[TrialRatio] = []
    for trial in document["trials"]:
        returns: list[float] = []
        years = 0.0
        for fold in document["folds"]:
            points = read_nav(path.parent / "navs" / f"{trial['digests'][fold]}.json").points
            returns += daily_returns(points)
            years += _years(points)
        out.append(TrialRatio(str(trial["arm"]), calmar(returns, years=years)))
    source = f"{path.parent.name}/{path.name} @ {str(document['commit'])[:12]} (round-1 arms)"
    return out, source


def cash_hurdles(
    windows: Sequence[FoldWindow], *, data_root: Path | None, lake_last: date
) -> dict[str, Decimal]:
    """§5 criterion 3's bar per window: the cash sleeve's after-tax return there.

    A whole window in cash (repo - 50 bp, credited monthly, slab-taxed), struck by
    ``backtest.ma_timing.simulate`` with every session decided *out* — the same cash and tax
    conventions as the standalone timing runs.
    """
    series = load_index("nifty500", through=lake_last, data_root=data_root)
    model = CostModel(load_rate_card(), account_state="MH")
    repo = load_repo_rate_schedule()
    rule = TimingRule(ma_days=200, cadence=Cadence.MONTHLY)
    decided: dict[date, tuple[bool | None, Decimal | None]] = dict.fromkeys(
        series.sessions, (False, None)
    )
    out: dict[str, Decimal] = {}
    for w in windows:
        run = simulate(
            series,
            start=w.window.start,
            end=w.window.end,
            rule=rule,
            cost_model=model,
            repo=repo,
            costs=CostMode.CARD,
            decided=decided,
        )
        value = after_tax(run).after_tax_xirr_liquidated
        if value is None:
            raise StudyError(f"the cash hurdle on {w.fold} could not be struck")
        out[w.fold] = value
    return out


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _r(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _inr(value: Decimal | None) -> str:
    return "n/a" if value is None else f"₹{value:,.0f}"


def _floor_label(floor: Decimal) -> str:
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


@dataclass(frozen=True, slots=True)
class _Book:
    """Every run of the study, by (label, fold, floor)."""

    runs: dict[tuple[str, str, Decimal], Run]

    def get(self, label: str, fold: str, floor: Decimal) -> Run:
        try:
            return self.runs[(label, fold, floor)]
        except KeyError as error:
            raise StudyError(f"missing run: {label} {fold} @{floor}") from error


def _load_book(plan: Plan, units: Sequence[Unit], fmv: GrandfatheringPrices) -> _Book:
    runs: dict[tuple[str, str, Decimal], Run] = {}
    for (label, fold, floor), digest in unit_digests(plan, units).items():
        if load_run(plan.out_dir, digest) is None:
            raise StudyError(f"run not on disk: {label} {fold} @{floor} ({digest[:12]})")
        runs[(label, fold, Decimal(floor))] = Run(
            label=label,
            fold=fold,
            floor=Decimal(floor),
            digest=digest,
            facts=_facts(plan.out_dir, digest, fmv),
            exposure=mean_invested(plan.out_dir, digest),
            nav=read_nav(nav_file(plan.out_dir, digest), digest=digest).points,
        )
    return _Book(runs)


def _analytic(book: _Book, label: str, fold: str, floor: Decimal, repo: RepoRateSchedule) -> Run:
    """The analytic equal-exposure mix matched to ``label``'s own exposure on (fold, floor)."""
    base = book.get(BASELINE.label, fold, floor)
    target = book.get(label, fold, floor)
    fraction = min(_ONE, target.exposure / base.exposure)
    path = mix_path(base.nav, fraction, repo)
    facts = _RunFacts(
        pre_tax_xirr=_path_xirr(path),
        after_tax_xirr=None,
        after_tax_error="analytic mix: pre-tax only",
        max_drawdown=_path_drawdown(path),
        sells=0,
        buys=0,
        charges=_ZERO,
        tax=None,
        rail_blocks=None,
    )
    return Run(
        label=f"Equal-exposure D13, analytic at {fraction:.3f}",
        fold=fold,
        floor=floor,
        digest="analytic",
        facts=facts,
        exposure=fraction * base.exposure,
        nav=tuple(path),
    )


DECISION_COMPARATORS: Final = (
    EQUAL_EXPOSURE_LABEL,
    VOL_TARGET_15.label,
    VOL_TARGET_TIGHT_LABEL,
    BAND.label,
    ANALOGUE.label,
)


def _figures_table(
    book: _Book, labels: Sequence[str], folds: Sequence[str], floor: Decimal
) -> list[str]:
    lines = [
        "| Arm | Window | Pre-tax XIRR | Max DD | **XIRR/DD** | After-tax XIRR | % invested | "
        "Charges | Tax | Buys / sells |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fold in folds:
        for label in labels:
            run = book.runs.get((label, fold, floor))
            if run is None:
                continue
            f = run.facts
            lines.append(
                f"| {label} | {fold} | {f.pre_tax_xirr:.2%} | {f.max_drawdown:.2%} | "
                f"**{_r(run.ratio)}** | {_pct(f.after_tax_xirr)} | {run.exposure:.0%} | "
                f"{_inr(f.charges)} | {_inr(f.tax)} | {f.buys} / {f.sells} |"
            )
    return lines


def _criteria_lines(verdict: DrawdownVerdict) -> list[str]:
    lines = [
        f"**{verdict.candidate}** against **{verdict.baseline}** and "
        f"{', '.join(verdict.comparators)}:",
        "",
    ]
    for c in verdict.criteria:
        lines.append(f"- **C{c.number} {c.outcome.value}** — {c.rule}: {c.detail}")
    return lines


def _single_window_check(
    book: _Book, fold: str, floor: Decimal, rivals: Sequence[Run]
) -> tuple[bool, str]:
    """R5/R6: on one window the candidate's ratio beats every rival's and its DD is 5 pp lower."""
    cand = book.get(CANDIDATE.label, fold, floor)
    base = book.get(BASELINE.label, fold, floor)
    beats = [(r.label, beats_on_ratio(cand.figures(), r.figures())) for r in (base, *rivals)]
    cut = base.facts.max_drawdown - cand.facts.max_drawdown
    ok = all(b is True for _, b in beats) and cut >= Decimal("0.050")
    detail = (
        f"ratio {_r(cand.ratio)}; beats "
        + ", ".join(f"{label} ({'yes' if b else 'no'})" for label, b in beats)
        + f"; DD {cand.facts.max_drawdown:.2%} vs D13 {base.facts.max_drawdown:.2%} "
        f"({cut * 100:+.2f} pp lower)"
    )
    return ok, detail


def _month_lines(book: _Book, plan: Plan, folds: FoldPlan, *, lake_last: date) -> list[str]:
    """§7: what the candidate's cash months were, and what D13 earned in them."""
    fold = continuous_fold(folds)
    window = continuous_window(folds)
    d13 = book.get(BASELINE.label, fold, DECIDING_FLOOR)
    cand = book.get(CANDIDATE.label, fold, DECIDING_FLOOR)
    reader = _L1Reader(data_root=plan.data_root)
    try:
        sessions = tuple(reader.trading_sessions(window.start, window.end))
    finally:
        reader.close()
    regime = _RegimeSource.published(through=window.end, ma_days=200, data_root=plan.data_root)
    pe = ValuationSource.published(
        through=window.end, mean_days=63, splice=False, data_root=plan.data_root
    )
    tri = {
        p.session: p.level
        for p in load_index("nifty500", through=lake_last, data_root=plan.data_root).points
    }
    rebalances = _first_session_of_each_month(sessions)
    d13_nav = dict(d13.nav)
    cand_nav = dict(cand.nav)
    exposure = dict(invested_fractions(plan.out_dir, cand.digest))

    def value_on(series: Mapping[date, Decimal], day: date) -> Decimal:
        return series[max(d for d in series if d <= day)]

    rows: list[tuple[date, str, Decimal, Decimal, Decimal, Decimal]] = []
    ends = [*rebalances[1:], sessions[-1]]
    for start, end in zip(rebalances, ends, strict=True):
        on = regime.reading(start).risk_on
        cheap = pe.reading(start).cheap
        state = "both invested" if on and cheap else ("P/E-only cash" if on else "price cash")
        d13_r = value_on(d13_nav, end) / value_on(d13_nav, start) - _ONE
        cand_r = value_on(cand_nav, end) / value_on(cand_nav, start) - _ONE
        tri_r = value_on(tri, end) / value_on(tri, start) - _ONE
        span = [f for d, f in exposure.items() if start <= d < end] or [_ZERO]
        rows.append((start, state, d13_r, cand_r, tri_r, sum(span, _ZERO) / len(span)))

    def compound(values: Sequence[Decimal]) -> Decimal:
        level = _ONE
        for v in values:
            level *= _ONE + v
        return level - _ONE

    lines = [
        f"Continuous window {window.start} → {window.end}, ₹10 crore/day. Each month runs from a "
        "D13 rebalance session to the next; its state is read on the opening session (D13's "
        "price regime: NIFTY 50 TRI ≥ its 200-session mean; P/E gate: NIFTY 500 P/E < its "
        "63-session mean). The candidate is invested only in *both invested* months; *P/E-only "
        "cash* months are the ones the P/E alone took it out of (D13 was invested); in *price "
        "cash* months D13 itself was parked. The candidate's actual exposure is its ledger's "
        "(A8's minimum-holdings rail keeps a few names in a parked book).",
        "",
        "| Month state | Months | D13 compounded | D13 mean / month | D13 up / down months | "
        "Candidate compounded | Candidate mean exposure | NIFTY 500 TRI compounded |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for state in ("both invested", "P/E-only cash", "price cash"):
        part = [r for r in rows if r[1] == state]
        if not part:
            lines.append(f"| {state} | 0 | — | — | — | — | — | — |")
            continue
        d = [r[2] for r in part]
        lines.append(
            f"| {state} | {len(part)} | {compound(d):+.2%} | "
            f"{sum(d, _ZERO) / len(d):+.2%} | {sum(1 for v in d if v > 0)} / "
            f"{sum(1 for v in d if v <= 0)} | {compound([r[3] for r in part]):+.2%} | "
            f"{sum((r[5] for r in part), _ZERO) / len(part):.0%} | "
            f"{compound([r[4] for r in part]):+.2%} |"
        )
    worst = sorted(rows, key=lambda r: r[2])[:10]
    best = sorted(rows, key=lambda r: r[2], reverse=True)[:10]
    lines += [
        "",
        "D13's ten **worst** and ten **best** months, and what state the candidate was in:",
        "",
        "| Rank | Worst month | D13 | Candidate | State | Best month | D13 | Candidate | State |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, (w, b) in enumerate(zip(worst, best, strict=True), start=1):
        lines.append(
            f"| {i} | {w[0]:%Y-%m} | {w[2]:+.2%} | {w[3]:+.2%} | {w[1]} | {b[0]:%Y-%m} | "
            f"{b[2]:+.2%} | {b[3]:+.2%} | {b[1]} |"
        )
    tally = {
        side: {
            s: sum(1 for r in rs if r[1] == s)
            for s in ("both invested", "P/E-only cash", "price cash")
        }
        for side, rs in (("worst", worst), ("best", best))
    }
    lines += [
        "",
        f"Of D13's ten worst months the candidate was in P/E-only cash in "
        f"{tally['worst']['P/E-only cash']}, price cash in {tally['worst']['price cash']} and "
        f"invested in {tally['worst']['both invested']}; of its ten best, P/E-only cash in "
        f"{tally['best']['P/E-only cash']}, price cash in {tally['best']['price cash']} and "
        f"invested in {tally['best']['both invested']}.",
        "",
        "Every P/E-only cash month (D13 invested, the P/E gate rich):",
        "",
        "| Month | D13 | Candidate | Candidate exposure | NIFTY 500 TRI |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        if r[1] == "P/E-only cash":
            lines.append(f"| {r[0]:%Y-%m} | {r[2]:+.2%} | {r[3]:+.2%} | {r[5]:.0%} | {r[4]:+.2%} |")
    peak_day, trough_day = _drawdown_span(d13.nav)
    in_span = [f for d, f in exposure.items() if peak_day <= d <= trough_day]
    lines += [
        "",
        f"D13's maximum drawdown ran from {peak_day} to {trough_day} "
        f"({d13.facts.max_drawdown:.2%}); over that span the candidate's mean exposure was "
        f"{sum(in_span, _ZERO) / max(1, len(in_span)):.0%} and its NAV moved "
        f"{value_on(cand_nav, trough_day) / value_on(cand_nav, peak_day) - _ONE:+.2%} "
        f"(D13 {value_on(d13_nav, trough_day) / value_on(d13_nav, peak_day) - _ONE:+.2%}).",
    ]
    c_peak, c_trough = _drawdown_span(cand.nav)
    lines.append(
        f"The candidate's own maximum drawdown ran from {c_peak} to {c_trough} "
        f"({cand.facts.max_drawdown:.2%}); D13 moved "
        f"{value_on(d13_nav, c_trough) / value_on(d13_nav, c_peak) - _ONE:+.2%} over it."
    )
    return lines


def _drawdown_span(nav: Sequence[tuple[date, Decimal]]) -> tuple[date, date]:
    peak_day, peak = nav[0]
    worst = _ZERO
    span = (nav[0][0], nav[0][0])
    for day, value in nav:
        if value > peak:
            peak_day, peak = day, value
        elif peak > _ZERO and _ONE - value / peak > worst:
            worst = _ONE - value / peak
            span = (peak_day, day)
    return span


def render(
    plan: Plan,
    folds: FoldPlan,
    *,
    trials: int,
    trial_sharpes: Path,
    parent_dir: Path,
    reference_lines: Sequence[str],
    frozen: Mapping[str, Any],
    calibration: Mapping[str, Any],
) -> str:
    """The generated decision report (``reports/decision.md``)."""
    windows = study_windows(folds)
    units = [*baseline_units(windows), *phase1_units(windows), *phase2_units(windows, calibration)]
    lake_last = _lake_last_session(plan.data_root)
    service, fmv = l1_grandfathering(plan.data_root)
    with service:
        book = _load_book(plan, units, fmv)
    repo = load_repo_rate_schedule()
    decision_folds = [f.name for f in folds.folds]
    hurdle = cash_hurdles(windows, data_root=plan.data_root, lake_last=lake_last)

    def arm(label: str, floor: Decimal = DECIDING_FLOOR) -> ArmWindows:
        return arm_windows(label, [book.get(label, f, floor) for f in decision_folds])

    # Trial ratios: this study's arms, the parent's part-A arms, the round-1 arms (§6).
    labels = [
        BASELINE.label,
        CANDIDATE.label,
        *DECISION_COMPARATORS,
        *(a.label for a in ROBUSTNESS),
    ]
    own: list[TrialRatio] = []
    for label in labels:
        figures = arm(label)
        own.append(TrialRatio(label, calmar(figures.pre_tax_returns, years=figures.years)))
    parent_labels = {repr(a.v2): a.label for a in PE_TIMING_ARMS}
    fold_windows = [f.test for f in folds.folds]
    parent = _scan_trials(parent_dir, parent_labels, fold_windows)
    supplied, supplied_source = _supplied_trials(trial_sharpes)
    variance: RatioVariance = ratio_variance(
        [*own, *parent, *supplied],
        source=f"{len(own)} arms of this study + {len(parent)} PE-timing arms "
        f"({parent_dir.name}) + {len(supplied)} from {supplied_source}",
    )
    baseline = arm(BASELINE.label)
    comparators = [arm(label) for label in DECISION_COMPARATORS]
    verdict = evaluate_drawdown_first(
        arm(CANDIDATE.label),
        baseline,
        comparators,
        cash_hurdle=hurdle,
        trials=trials,
        variance=variance,
    )

    # Robustness (§6): criteria 1 and 2 against the same rivals, equal exposure analytic.
    fixed_rivals = [VOL_TARGET_15.label, VOL_TARGET_TIGHT_LABEL, BAND.label, ANALOGUE.label]

    def robust(label: str, floor: Decimal) -> tuple[bool, str, ArmWindows]:
        mix = arm_windows(
            "Equal-exposure D13 (analytic)",
            [_analytic(book, label, f, floor, repo) for f in decision_folds],
        )
        rivals = [mix, *(arm(r, floor) for r in fixed_rivals)]
        ok, detail = robust_pass(arm(label, floor), arm(BASELINE.label, floor), rivals)
        return ok, detail, mix

    neighbour_rows = [(a.label, *robust(a.label, DECIDING_FLOOR)) for a in NEIGHBOURS]
    n_neighbours = sum(1 for _, ok, _, _ in neighbour_rows if ok)
    n50_ok, n50_detail, _ = robust(NIFTY50.label, DECIDING_FLOOR)
    spl_ok, spl_detail, _ = robust(SPLICED.label, DECIDING_FLOOR)
    low_ok, low_detail, _ = robust(CANDIDATE.label, LOW_FLOOR)
    m6_rivals = [book.get(label, MANDATE.fold, DECIDING_FLOOR) for label in DECISION_COMPARATORS]
    m6_ok, m6_detail = _single_window_check(book, MANDATE.fold, DECIDING_FLOOR, m6_rivals)
    oos_rivals = [
        book.get(label, OUT_OF_SAMPLE.fold, DECIDING_FLOOR) for label in DECISION_COMPARATORS
    ]
    oos_ok, oos_detail = _single_window_check(book, OUT_OF_SAMPLE.fold, DECIDING_FLOOR, oos_rivals)
    checks = [
        ("R1 neighbouring lookbacks (≥ 2 of 42/84/126 pass C1+C2)", n_neighbours >= 2),
        ("R2 NIFTY 50 P/E", n50_ok),
        ("R3 spliced P/E", spl_ok),
        ("R4 ₹1 crore/day floor", low_ok),
        ("R5 the 6-year mandate window", m6_ok),
        ("R6 out of sample (O1)", oos_ok),
    ]
    word = study_verdict(verdict, checks)

    all_folds = [w.fold for w in windows]
    main_labels = [BASELINE.label, CANDIDATE.label, *DECISION_COMPARATORS]
    lines = [
        "# PE drawdown — the drawdown-first decision (generated)",
        "",
        "*Rule: `backtest/drawdown_rule.py`, as pre-registered in "
        "`ops/studies/preregistration-pe-drawdown-2026-10-19.md` before any run. Generated by "
        "`python -m backtest.pe_drawdown render`.*",
        "",
        f"## Verdict: **{word.value}**",
        "",
        *_criteria_lines(verdict),
        "",
        "Robustness (decides NOT ROBUST only when C1-C5 all pass; reported regardless):",
        "",
        *(f"- {name}: **{'PASS' if ok else 'FAIL'}**" for name, ok in checks),
        "",
        f"- Trial count N = **{trials}**; ratio variance {variance.describe()}.",
        "- Left out of the variance: " + ("; ".join(variance.excluded) or "none"),
        "- Cash hurdle (after-tax, the cash sleeve over the whole window): "
        + ", ".join(f"{k} {v:.2%}" for k, v in hurdle.items()),
        f"- Calibration (`{CALIBRATION_NAME}`): tighter vol target "
        f"**{calibration['vol_target']}**; "
        "equal-exposure fractions "
        + ", ".join(f"{k} {v or '1 (D13)'}" for k, v in sorted(calibration["fractions"].items())),
        f"- Baseline frozen at commit `{str(frozen['commit'])[:12]}` on the lake through "
        f"{frozen['lake_last_session']} before any comparator ran.",
        "- " + describe_cash_interest(True),
        "",
        "### D13 against the parent study's frozen D13",
        "",
        *(f"- {line}" for line in reference_lines),
        "",
    ]
    for floor in FLOORS:
        role = "decides" if floor == DECIDING_FLOOR else "information only — decides nothing"
        lines += [
            f"## Candidate and comparators at {_floor_label(floor)} ({role})",
            "",
            *_figures_table(book, main_labels, all_folds, floor),
            "",
        ]
    lines += [
        "## Robustness grid (generated)",
        "",
        "Each variant against D13, VT15, the tighter vol target, the stricter price regime, the "
        "price-only analogue and an **analytic** equal-exposure D13 matched to the variant's own "
        "exposure (pre-tax, daily-rebalanced, no costs). Criteria 1 and 2 only.",
        "",
        "| Variant | Floor | Result | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for label, ok, detail, _ in neighbour_rows:
        lines.append(f"| {label} | ₹10 cr | {'PASS' if ok else 'FAIL'} | {detail} |")
    lines += [
        f"| {NIFTY50.label} | ₹10 cr | {'PASS' if n50_ok else 'FAIL'} | {n50_detail} |",
        f"| {SPLICED.label} | ₹10 cr | {'PASS' if spl_ok else 'FAIL'} | {spl_detail} |",
        f"| {CANDIDATE.label} | ₹1 cr | {'PASS' if low_ok else 'FAIL'} | {low_detail} |",
        f"| {CANDIDATE.label}, M6 2019-07 → 2026-08 | ₹10 cr | {'PASS' if m6_ok else 'FAIL'} | "
        f"{m6_detail} |",
        f"| {CANDIDATE.label}, O1 2013-02 → 2015-01 | ₹10 cr | {'PASS' if oos_ok else 'FAIL'} | "
        f"{oos_detail} |",
        "",
    ]
    robust_labels = [CANDIDATE.label, *(a.label for a in ROBUSTNESS)]
    for floor in FLOORS:
        lines += [
            f"### Robustness variants at {_floor_label(floor)}",
            "",
            *_figures_table(book, robust_labels, all_folds, floor),
            "",
        ]
    lines += [
        "### Equal exposure: engine run against the analytic mix (candidate's exposure)",
        "",
        "| Window | Floor | Engine: XIRR / DD / ratio / exposure | Analytic: XIRR / DD / ratio / "
        "exposure |",
        "| --- | --- | --- | --- |",
    ]
    for floor in FLOORS:
        for fold in all_folds:
            eng = book.get(EQUAL_EXPOSURE_LABEL, fold, floor)
            ana = _analytic(book, CANDIDATE.label, fold, floor, repo)
            lines.append(
                f"| {fold} | {_floor_label(floor)} | {eng.facts.pre_tax_xirr:.2%} / "
                f"{eng.facts.max_drawdown:.2%} / {_r(eng.ratio)} / {eng.exposure:.0%} | "
                f"{ana.facts.pre_tax_xirr:.2%} / {ana.facts.max_drawdown:.2%} / {_r(ana.ratio)} / "
                f"{ana.exposure:.0%} |"
            )
    lines += [
        "",
        "## Cash months (generated)",
        "",
        *_month_lines(book, plan, folds, lake_last=lake_last),
        "",
        "## Trial ratios counted in the variance",
        "",
        ", ".join(f"{t.label} {t.ratio:.3f}" for t in variance.ratios),
        "",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.pe_drawdown")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="phase 0 (freeze D13), phase 1, calibration, phase 2")
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--signal-panel", type=Path, default=None)
    run.add_argument("--reuse-from", type=Path, action="append", default=[])
    run.add_argument("--reference-frozen", type=Path, default=None)
    rend = sub.add_parser("render", help="render the study from runs on disk")
    rend.add_argument("--trials", type=int, required=True)
    rend.add_argument("--trial-sharpes", type=Path, required=True)
    rend.add_argument("--parent-dir", type=Path, required=True)
    rend.add_argument("--reference-frozen", type=Path, default=None)
    rend.add_argument("--signal-panel", type=Path, default=None)
    for p in (run, rend):
        p.add_argument("--out", type=Path, required=True, help="study directory (not data/)")
        p.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.pe_drawdown``. Returns a process exit code."""
    args = _parse_args(argv)
    folds = load_folds()
    windows = study_windows(folds)
    try:
        out_dir = refuse_lake_location(args.out, args.data_root)
        plan = Plan(
            out_dir=out_dir,
            data_root=args.data_root,
            signal_panel=args.signal_panel.resolve() if args.signal_panel else None,
            reuse_from=tuple(p.resolve() for p in getattr(args, "reuse_from", [])),
        )
        base = baseline_units(windows)
        if args.command == "run":
            out_dir.mkdir(parents=True, exist_ok=True)
            commit = _git_commit()
            if run_units(plan, base, workers=args.workers):
                raise StudyError("a baseline run failed; nothing frozen (re-run to retry)")
            frozen = freeze_baseline(
                plan, base, commit=commit, lake_last_session=_lake_last_session(args.data_root)
            )
            verify_frozen(plan, base)
            print(f"  baseline frozen: {out_dir / FROZEN_NAME}")
            if args.reference_frozen is not None:
                for line in compare_reference(frozen, args.reference_frozen):
                    print(f"  D13 {line}")
            if run_units(plan, phase1_units(windows), workers=args.workers):
                raise StudyError("a phase-1 run failed; re-run to retry")
            calibration = calibrate(plan, windows, folds)
            print(
                f"  calibrated: {json.dumps(calibration['fractions'])} "
                f"vt {calibration['vol_target']}"
            )
            verify_frozen(plan, base)
            if run_units(plan, phase2_units(windows, calibration), workers=args.workers):
                raise StudyError("a phase-2 run failed; re-run to retry")
            return 0
        frozen = verify_frozen(plan, base)
        calibration_path = out_dir / CALIBRATION_NAME
        if not calibration_path.is_file():
            raise StudyError("no calibration on disk: phase 1 has not finished")
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        reference_lines = (
            compare_reference(frozen, args.reference_frozen)
            if args.reference_frozen is not None
            else ["(no earlier record named)"]
        )
        text = render(
            plan,
            folds,
            trials=args.trials,
            trial_sharpes=args.trial_sharpes,
            parent_dir=args.parent_dir,
            reference_lines=reference_lines,
            frozen=frozen,
            calibration=calibration,
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
