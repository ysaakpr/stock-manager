"""PE timing study: the market P/E against its own trailing mean, on D13 and as index timing.

``ops/studies/preregistration-pe-timing-2026-10-19.md`` fixes the question, the arms and the rule
before any run. Two parts:

* **A — overlay on D13.** D13 with the valuation gate (``backtest.policies.momentum_v2``
  ``ValuationGate``) in three modes for three trailing-mean lengths, on the raw P/E and on the P/E
  spliced across NSE's 2021-03-31 earnings-basis change (``backtest.sweep.PE_TIMING_ARMS``): 18
  arms, each judged by the round-2 four-criterion rule (``backtest.decision_rule``) against D13
  itself on the fold test windows F1-F3 at ₹10 crore/day, after tax; ₹1 crore/day beside it for
  information. The driver is the per-stock rebalance study's: D13 runs first and is frozen
  (``frozen-baseline.json``) before any arm, and the frozen record is checked against the earlier
  study's D13 record when one is named (``--reference-frozen``).
* **B — standalone index timing.** Hold the NIFTY 500 TRI (as a liquid index ETF, through
  ``backtest.ma_timing.simulate`` — the shared cost model, repo-50bp cash, FIFO tax) while the P/E
  reads cheap at a month's last close, else cash; against buy-and-hold and the same timing on the
  index's own 200-session average. No replay: it runs inside ``render``.

``python -m backtest.pe_timing run --out DIR --workers N [--signal-panel P] [--reference-frozen F]``
    Runs D13 on every fold test window and the continuous F1-F3 window at both floors, freezes it,
    checks it against ``F``, then runs the 18 arms the same way. Resumable run by run.

``python -m backtest.pe_timing render --out DIR --trials N --trial-sharpes FILE``
    Replays nothing: writes ``DIR/reports/decision.md`` — the methodology-break finding, the
    agreement of the P/E gate with the price regime, part A's decision and figures, and part B.

What it never does: write under the lake, run an arm before the baseline is frozen, tune a
parameter, or round a figure before the rule compares it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
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
from backtest.decision_rule import (
    PREREGISTERED_MIN_TRIALS,
    ArmFolds,
    ArmVerdict,
    FoldResult,
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
from backtest.ma_timing import (
    Cadence,
    CostMode,
    IndexSeries,
    SimResult,
    TimingRule,
    after_tax,
    load_index,
    metrics,
    simulate,
    states,
)
from backtest.nav import NavSeries, after_tax_nav, daily_returns, nav_file
from backtest.policies.momentum_v2 import PE_GATE_MEAN_DAYS, ValuationGate
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
    run_digests,
    run_sweep,
)
from backtest.tax import GrandfatheringPrices, TaxError, compute_after_tax, load_tax_schedule
from backtest.valuation_gate import (
    CLOSE_SERIES,
    PE_METHODOLOGY_BREAK,
    PE_SERIES,
    ValuationSource,
    load_pe_points,
    splice_factor,
)
from dataplatform.logging import get_logger
from execution.broker import Side
from execution.costs import CostModel, load_rate_card

__all__ = [
    "BASELINE",
    "DECIDING_FLOOR",
    "FLOORS",
    "FROZEN_NAME",
    "PREREGISTRATION_PATH",
    "StudyError",
    "StudyPlan",
    "main",
    "pe_states",
    "study_plan",
]

_LOG = get_logger(__name__)

BASELINE: Arm = D13_PAPER_BASELINE
DECIDING_FLOOR = HIGH_FLOOR
FLOORS: tuple[Decimal, ...] = (HIGH_FLOOR, LOW_FLOOR)
PREREGISTRATION_PATH = (
    Path(__file__).resolve().parent.parent / "ops/studies/preregistration-pe-timing-2026-10-19.md"
)
FROZEN_NAME = "frozen-baseline.json"
_TEST = "test"
_INTEREST_CONVENTION = "repo-50bp:"
#: Part B's index: the NIFTY 500 TRI, the index whose P/E the gate reads.
INDEX_SLUG: Final = "nifty500"
#: Part B's price reference: the same timing on the index's own 200-session simple average.
PRICE_RULE: Final = TimingRule(ma_days=200, cadence=Cadence.MONTHLY)
#: D13's price regime: NIFTY 50 TRI against its 200-session mean (the agreement statistics).
_D13_REGIME_DAYS: Final = 200
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


class StudyError(RuntimeError):
    """The study cannot proceed as asked — fails loud, before any run or render."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── part A: plan, runs, freeze ───────────────────────────────────────────────────────────────


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
            _LOG.error("pe_timing.run_failed", arm=row.arm.label, error=row.error)
    unit = f"{target.fold}-{target.role}@{floor}"
    _LOG.info("pe_timing.unit_done", unit=unit, failed=failed)
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

    Refuses a partial phase, and a record already on disk that differs in any run.
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
    _LOG.info("pe_timing.frozen", path=str(path), runs=len(runs))
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


def compare_reference(frozen: Mapping[str, Any], reference: Path) -> list[str]:
    """Each D13 run against an earlier study's frozen D13 record: same digest and file hashes.

    Returns one line per run (``identical`` or what differs); raises ``StudyError`` on any
    difference — a D13 that moved is a different baseline, not this study's to explain away.
    """
    other = json.loads(reference.read_text(encoding="utf-8"))
    theirs = {(r["fold"], r["role"], r["floor"]): r for r in other["runs"]}
    lines: list[str] = []
    problems: list[str] = []
    keys = ("digest", "summary_sha256", "ledger_sha256", "nav_sha256")
    for run in frozen["runs"]:
        key = (run["fold"], run["role"], run["floor"])
        match = theirs.get(key)
        where = f"{run['fold']} {run['role']} @{run['floor']}"
        if match is None:
            problems.append(f"{where}: not in {reference.name}")
            continue
        differ = [k for k in keys if match[k] != run[k]]
        if differ:
            problems.append(f"{where}: {', '.join(differ)} differ")
        lines.append(f"{where}: {'identical' if not differ else 'DIFFERS: ' + ', '.join(differ)}")
    if problems:
        raise StudyError("D13 is not the earlier frozen D13: " + "; ".join(problems[:6]))
    return lines


# ── signals: the P/E gate, the price regime, the methodology break ───────────────────────────


def pe_states(
    series: IndexSeries, source: ValuationSource
) -> dict[date, tuple[bool | None, Decimal | None]]:
    """Part B's decision per session: cheap/rich struck at each month's last close, carried between.

    The shape ``backtest.ma_timing.simulate`` takes (``decided``). ``None`` until the P/E source can
    strike a reading (before the series starts or before it has ``L`` points); after that a month's
    last session with no published P/E fails loud — a stale figure is never carried.
    """
    sessions = series.sessions
    last_of_month: dict[tuple[int, int], date] = {}
    for session in sessions:
        last_of_month[(session.year, session.month)] = session
    checks = set(last_of_month.values())
    first = source.first_full_session
    state: bool | None = None
    mean: Decimal | None = None
    out: dict[date, tuple[bool | None, Decimal | None]] = {}
    for session in sessions:
        if session in checks and first is not None and session >= first:
            reading = source.reading(session)
            state, mean = reading.cheap, reading.trailing_mean
        out[session] = (state, mean)
    return out


@dataclass(frozen=True, slots=True)
class _Signals:
    """Every reading the agreement and switch statistics need, struck once."""

    rebalances: tuple[date, ...]
    sessions: tuple[date, ...]
    price_on: dict[date, bool]
    cheap: dict[tuple[int, bool], dict[date, bool]]
    tri: dict[date, Decimal]


def _signals(start: date, end: date, *, data_root: Path | None, lake_last: date) -> _Signals:
    reader = _L1Reader(data_root=data_root)
    try:
        sessions = tuple(reader.trading_sessions(start, end))
    finally:
        reader.close()
    regime = _RegimeSource.published(through=end, ma_days=_D13_REGIME_DAYS, data_root=data_root)
    price_on = {s: regime.reading(s).risk_on for s in sessions}
    cheap: dict[tuple[int, bool], dict[date, bool]] = {}
    for days in PE_GATE_MEAN_DAYS:
        for splice in (False, True):
            source = ValuationSource.published(
                through=end, mean_days=days, splice=splice, data_root=data_root
            )
            cheap[(days, splice)] = {s: source.reading(s).cheap for s in sessions}
    tri_series = load_index(INDEX_SLUG, through=lake_last, data_root=data_root)
    tri = {p.session: p.level for p in tri_series.points}
    return _Signals(
        rebalances=tuple(_first_session_of_each_month(sessions)),
        sessions=sessions,
        price_on=price_on,
        cheap=cheap,
        tri=tri,
    )


def _invested(gate: ValuationGate, price_on: bool, cheap: bool) -> bool:
    if gate is ValuationGate.REPLACE:
        return cheap
    if gate is ValuationGate.BOTH:
        return price_on and cheap
    return price_on


@dataclass(frozen=True, slots=True)
class _Switches:
    parks: int
    reentries: int
    parked_months: int
    blocked_months: int


def _switches(signals: _Signals, gate: ValuationGate | None, key: tuple[int, bool]) -> _Switches:
    """Recount, from the readings alone, what the gate decides on each rebalance session."""
    parked = False
    parks = reentries = parked_months = blocked = 0
    for session in signals.rebalances:
        on = signals.price_on[session]
        cheap = signals.cheap[key][session] if gate is not None else True
        invested = on if gate is None else _invested(gate, on, cheap)
        if not invested:
            parked_months += 1
            parks += 0 if parked else 1
            parked = True
            continue
        if parked:
            reentries += 1
        parked = False
        if gate is ValuationGate.ENTRY and not cheap:
            blocked += 1
    return _Switches(parks, reentries, parked_months, blocked)


def _trailing(
    tri: Mapping[date, Decimal], sessions: Sequence[date], i: int, n: int
) -> Decimal | None:
    if i < n:
        return None
    return tri[sessions[i]] / tri[sessions[i - n]] - _ONE


def agreement_lines(signals: _Signals) -> list[str]:
    """How often the P/E gate agrees with D13's price regime, and when it fires."""
    lines = [
        "| P/E mean | Series | Rebalance sessions | Price on & P/E cheap | Price on & P/E rich "
        "| Price off & P/E cheap | Price off & P/E rich | Agreement (on=cheap) | "
        "P(cheap given price on) | P(cheap given price off) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (days, splice), cheap in signals.cheap.items():
        cells = {(a, b): 0 for a in (True, False) for b in (True, False)}
        for s in signals.rebalances:
            cells[(signals.price_on[s], cheap[s])] += 1
        n = len(signals.rebalances)
        on = cells[(True, True)] + cells[(True, False)]
        off = n - on
        agree = cells[(True, True)] + cells[(False, False)]
        lines.append(
            f"| {days} | {'spliced' if splice else 'raw'} | {n} | {cells[(True, True)]} | "
            f"{cells[(True, False)]} | {cells[(False, True)]} | {cells[(False, False)]} | "
            f"{agree / n:.0%} | {_ratio(cells[(True, True)], on)} | "
            f"{_ratio(cells[(False, True)], off)} |"
        )
    lines += [
        "",
        "**Every session** (not only rebalances): share of sessions cheap, and the NIFTY 500 TRI's "
        "trailing 21-session return on cheap vs rich sessions — a dip-buying signal fires after "
        "falls, a trend filter after rises.",
        "",
        "| P/E mean | Series | Sessions | Cheap share | Agreement with price regime | "
        "Mean trailing 21-session return when cheap | when rich | Cheap share after a 21-session "
        "fall | after a 21-session rise |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    sessions = list(signals.sessions)
    for (days, splice), cheap in signals.cheap.items():
        n = len(sessions)
        agree = sum(1 for s in sessions if signals.price_on[s] == cheap[s])
        when: dict[bool, list[Decimal]] = {True: [], False: []}
        after: dict[bool, list[bool]] = {True: [], False: []}
        for i, s in enumerate(sessions):
            if s not in signals.tri or i < 21 or sessions[i - 21] not in signals.tri:
                continue
            back = signals.tri[s] / signals.tri[sessions[i - 21]] - _ONE
            when[cheap[s]].append(back)
            after[back < _ZERO].append(cheap[s])
        lines.append(
            f"| {days} | {'spliced' if splice else 'raw'} | {n} | "
            f"{sum(1 for s in sessions if cheap[s]) / n:.0%} | {agree / n:.0%} | "
            f"{_mean_pct(when[True])} | {_mean_pct(when[False])} | "
            f"{_share(after[True])} | {_share(after[False])} |"
        )
    return lines


def _ratio(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{part / whole:.0%} ({part}/{whole})"


def _share(flags: Sequence[bool]) -> str:
    return "n/a" if not flags else f"{sum(flags) / len(flags):.0%}"


def _mean_pct(values: Sequence[Decimal]) -> str:
    return "n/a" if not values else f"{sum(values, _ZERO) / len(values):+.2%}"


def break_lines(*, data_root: Path | None, through: date, signals: _Signals) -> list[str]:
    """The methodology-break finding: the largest implied-earnings jumps, the splice, the effect."""
    lines: list[str] = []
    for index in ("NIFTY_500", "NIFTY_50"):
        pe = {
            p.session: p.value
            for p in load_pe_points(f"IN.NSE.{index}.PE", through=through, data_root=data_root)
        }
        close = {
            p.session: p.value
            for p in load_pe_points(f"IN.NSE.{index}.CLOSE", through=through, data_root=data_root)
        }
        common = sorted(d for d in pe if d in close)
        jumps: list[tuple[float, date, date]] = []
        for prior, day in pairwise(common):
            earnings = (close[day] / pe[day]) / (close[prior] / pe[prior])
            jumps.append((abs(math.log(float(earnings))), prior, day))
        jumps.sort(reverse=True)
        lines += [
            f"**{index.replace('_', ' ')}** — the five largest one-session moves in implied "
            "earnings (close ÷ P/E) over the whole series:",
            "",
            "| Session | P/E before → after | P/E change | Index close change | Implied-earnings "
            "log change |",
            "| --- | --- | --- | --- | --- |",
        ]
        for size, prior, day in jumps[:5]:
            lines.append(
                f"| {day} | {pe[prior]} → {pe[day]} | {pe[day] / pe[prior] - _ONE:+.2%} | "
                f"{close[day] / close[prior] - _ONE:+.2%} | {size:.3f} |"
            )
        lines.append("")
    pe500 = {
        p.session: p.value for p in load_pe_points(PE_SERIES, through=through, data_root=data_root)
    }
    close500 = {
        p.session: p.value
        for p in load_pe_points(CLOSE_SERIES, through=through, data_root=data_root)
    }
    factor = splice_factor(pe500, close500, break_session=PE_METHODOLOGY_BREAK)
    lines += [
        f"Splice factor k at {PE_METHODOLOGY_BREAK} (NIFTY 500): **{factor:.6f}** — each earlier "
        "P/E is restated at k times its published value in a trailing window read on or after the "
        "break.",
        "",
        "| P/E mean | D13 rebalance sessions in F1-F3 where raw and spliced disagree | Which "
        "(raw reads) |",
        "| --- | --- | --- |",
    ]
    for days in PE_GATE_MEAN_DAYS:
        raw, spliced = signals.cheap[(days, False)], signals.cheap[(days, True)]
        differ = [s for s in signals.rebalances if raw[s] != spliced[s]]
        which = ", ".join(f"{s} ({'cheap' if raw[s] else 'rich'})" for s in differ) or "—"
        lines.append(f"| {days} | {len(differ)} | {which} |")
    return lines


# ── part A: render ───────────────────────────────────────────────────────────────────────────


def load_supplied_sharpes(path: Path) -> tuple[tuple[TrialSharpe, ...], str]:
    """The trial Sharpes of a ``backtest.fold_campaign trial-sharpes`` file, with provenance."""
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
    charges: Decimal
    tax: Decimal | None
    rail_blocks: tuple[tuple[str, int], ...] | None


def _facts(out_dir: Path, digest: str, fmv: GrandfatheringPrices) -> _RunFacts:
    loaded = load_run(out_dir, digest)
    if loaded is None:
        raise StudyError(f"run {digest} not on disk under {out_dir}")
    summary, ledger = loaded
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


def _gate_of(arm: Arm) -> tuple[ValuationGate | None, tuple[int, bool]]:
    params = arm.v2
    assert params is not None
    if params.valuation_gate is None:
        return None, (PE_GATE_MEAN_DAYS[0], False)
    assert params.valuation_mean_days is not None
    return params.valuation_gate, (params.valuation_mean_days, params.valuation_splice)


def _figures_table(
    out_dir: Path,
    arms: Sequence[Arm],
    digests: dict[tuple[str, str, str], str],
    windows: Sequence[FoldWindow],
    fmv: GrandfatheringPrices,
    switches: Mapping[tuple[str, str], _Switches],
) -> list[str]:
    lines = [
        "| Arm | Window | Pre-tax XIRR | After-tax XIRR (realised) | Max drawdown | XIRR/DD "
        "(pre-tax) | Parks / re-entries | Months parked | Months buys blocked | Buys | Sells | "
        "Charges | Tax paid | Rail blocks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in arms:
        for window in windows:
            facts = _facts(out_dir, digests[(arm.label, window.fold, window.role)], fmv)
            sw = switches[(arm.label, window.fold)]
            ratio = (
                f"{facts.pre_tax_xirr / facts.max_drawdown:.2f}"
                if facts.max_drawdown > _ZERO
                else "n/a"
            )
            lines.append(
                f"| {arm.label} | {window.fold} ({window.window.start} → {window.window.end}) | "
                f"{facts.pre_tax_xirr:.2%} | {_pct(facts.after_tax_xirr, facts.after_tax_error)} | "
                f"{facts.max_drawdown:.2%} | {ratio} | {sw.parks} / {sw.reentries} | "
                f"{sw.parked_months} | {sw.blocked_months} | {facts.buys} | {facts.sells} | "
                f"{_inr(facts.charges)} | {_inr(facts.tax)} | "
                f"{rail_blocks_cell(facts.rail_blocks)} |"
            )
    return lines


def _verdict_word(verdict: ArmVerdict) -> str:
    return {"KEPT": "ADOPT"}.get(
        verdict.label, "REJECT" if verdict.label == "NOT KEPT" else verdict.label
    )


def verdict_table(verdicts: Sequence[ArmVerdict]) -> list[str]:
    """One row per arm: each criterion's outcome and the ADOPT/REJECT word."""
    lines = [
        "| Arm | C1 mean after-tax XIRR +1.0 pp | C2 ≥2 of 3 windows | C3 DD ≤ +3.0 pp | C4 DSR ≥ "
        "0.95 | Verdict |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for v in verdicts:
        outcomes = " | ".join(c.outcome.value for c in v.criteria)
        lines.append(f"| {v.candidate} | {outcomes} | **{_verdict_word(v)}** |")
    return lines


def render_part_a(
    plan: StudyPlan,
    folds: FoldPlan,
    fmv: GrandfatheringPrices,
    *,
    trials: int,
    supplied: Sequence[TrialSharpe],
    supplied_source: str,
    frozen: Mapping[str, Any],
    switch_signals: Mapping[str, _Signals],
    reference_lines: Sequence[str],
) -> str:
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
            f"{frozen['lake_last_session']} before any arm ran (`{FROZEN_NAME}`).",
            "Momentum-v2 candidate sets served from the signal panel "
            + (f"`{panel['path']}` (cut {panel['first']} … {panel['last']})" if panel else "(none)")
            + "; baseline and arms read the same one.",
        ],
        command="render",
        headline=headline,
        title="Part A — D13 with the P/E gate: the pre-registered decision rule",
        provenance=(
            "*Rule: `backtest/decision_rule.py` (round 2's four criteria), as pre-registered in "
            "`ops/studies/preregistration-pe-timing-2026-10-19.md` before any run. Generated by "
            "`python -m backtest.pe_timing render`.*"
        ),
    )
    switches: dict[tuple[str, str], _Switches] = {}
    for window in plan.windows:
        signals = switch_signals[window.fold]
        for arm in plan.arms:
            gate, key = _gate_of(arm)
            switches[(arm.label, window.fold)] = _switches(signals, gate, key)
    lines = [
        "### Verdicts at a glance (₹10 crore/day; ADOPT = the rule's KEPT)",
        "",
        *verdict_table(verdicts),
        "",
        "### D13 re-verified on this commit",
        "",
        "Every D13 run of this study against the per-stock rebalance study's frozen D13 record "
        "(digest and the sha256 of its summary, ledger and NAV files):",
        "",
        *(f"- {line}" for line in reference_lines),
        "",
        text.rstrip("\n"),
        "",
    ]
    for floor in plan.floors:
        role = "decides" if floor == DECIDING_FLOOR else "information only — decides nothing"
        lines += [
            f"## Every run at {_floor_label(floor)} ({role})",
            "",
            "*Parks, re-entries, months parked and months whose buys the entry gate blocked are "
            "recounted from the readings on each rebalance session (D13's own rule for the "
            "baseline); buys and sells are fills from the run's ledger. Charges are every rupee "
            "of the shared cost model; tax paid is the realised-gains tax over the run.*",
            "",
            *_figures_table(
                plan.out_dir, plan.arms, _digests(plan, floor), plan.windows, fmv, switches
            ),
            "",
        ]
    return "\n".join(lines) + "\n"


# ── part B: standalone index timing ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Window:
    name: str
    start: date
    end: date


def _fold_result(name: str, run: SimResult) -> FoldResult:
    """One standalone run as the decision rule reads it — on the *liquidated* after-tax basis.

    Buy-and-hold never sells, so a realised-gains figure would leave its deferred LTCG untaxed and
    hand it a tax advantage no investor keeps; every standalone run is therefore taxed as if
    liquidated at the window's last close (pre-registration §5).
    """
    taxed = after_tax(run)
    series = NavSeries(digest=run.label, basis="pre-tax", points=run.nav)
    net = after_tax_nav(series, taxed.fy_taxes_liquidated, PROFILE)
    return FoldResult(
        fold=name,
        after_tax_xirr=taxed.after_tax_xirr_liquidated,
        max_drawdown=metrics(run).max_drawdown,
        after_tax_returns=tuple(daily_returns(net.points)),
        error=taxed.liquidation_error,
    )


def render_part_b(
    *,
    folds: FoldPlan,
    data_root: Path | None,
    lake_last: date,
    trials: int,
    supplied: Sequence[TrialSharpe],
    supplied_source: str,
) -> str:
    series = load_index(INDEX_SLUG, through=lake_last, data_root=data_root)
    end = min(lake_last, series.sessions[-1])
    model = CostModel(load_rate_card(), account_state="MH")
    repo: RepoRateSchedule = load_repo_rate_schedule()
    decided: dict[str, Mapping[date, tuple[bool | None, Decimal | None]]] = {}
    rules: dict[str, TimingRule | None] = {"Buy-and-hold NIFTY 500 TRI": None}
    for splice in (False, True):
        for days in PE_GATE_MEAN_DAYS:
            source = ValuationSource.published(
                through=end, mean_days=days, splice=splice, data_root=data_root
            )
            label = f"PE<{days}d mean timing" + (" (spliced P/E)" if splice else "")
            decided[label] = pe_states(series, source)
            # ``simulate`` reads ``decided`` when given; the rule only marks the run as timed.
            rules[label] = PRICE_RULE
    price_label = "Price: NIFTY 500 TRI ≥ its 200-session mean"
    decided[price_label] = states(series.points, PRICE_RULE)
    rules[price_label] = PRICE_RULE
    long_source = ValuationSource.published(
        through=end, mean_days=max(PE_GATE_MEAN_DAYS), splice=False, data_root=data_root
    )
    first_defined = long_source.first_full_session
    assert first_defined is not None
    full_start = next(s for s in series.sessions if s >= first_defined)
    windows = [
        _Window("full", full_start, end),
        *(_Window(f.name, f.test.start, f.test.end) for f in folds.folds),
        _Window(
            continuous_fold(folds), continuous_window(folds).start, continuous_window(folds).end
        ),
    ]

    def run(label: str, w: _Window, mode: CostMode) -> SimResult:
        return simulate(
            series,
            start=w.start,
            end=w.end,
            rule=rules[label],
            cost_model=model,
            repo=repo,
            costs=mode,
            decided=decided.get(label),
        )

    lines = [
        "## Part B — standalone index timing on the NIFTY 500 TRI (generated)",
        "",
        "Hold the NIFTY 500 TRI (as a liquid index ETF) while the NIFTY 500 P/E is below its "
        "trailing L-session mean at a month's last close, else cash at repo - 50 bp; the switch "
        "is traded at the next session's close. **Costs:** the shared cost model's delivery-equity "
        "card plus 0.1 % slippage a side (`backtest.ma_timing`, cost key NIFTYBEES); *ex-STT* "
        "leaves the card's STT line out — the card has no ETF instrument type and charges equity "
        "delivery STT, which an ETF does not fully pay, so the true cost lies between the two. "
        "**Tax:** FIFO lots, the open holding deemed sold at the window's last close (liquidated) "
        "— buy-and-hold's deferred LTCG is charged too. References (not trials): buy-and-hold, and "
        "the same timing on the index's own 200-session average.",
        "",
        f"The **full** window opens at {full_start}, the first session the 200-session P/E mean "
        f"is defined (P/E history starts 2012-10-01), and ends at {end}. The other windows are "
        "D13's fold test windows and their continuous union.",
        "",
    ]
    fold_names = {f.name for f in folds.folds}
    per_label_folds: dict[str, list[FoldResult]] = {label: [] for label in rules}
    for w in windows:
        lines += [
            f"### {w.name} ({w.start} → {w.end})",
            "",
            "| Strategy | CAGR after costs | CAGR ex-STT | After-tax (liquidated) | Max DD | "
            "**CAGR/DD** | % invested | Switches | Switches/yr | Charges (of which STT) | "
            "Slippage | Excess vs B&H |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        bh_cagr: Decimal | None = None
        for label in rules:
            card = run(label, w, CostMode.CARD)
            ex = run(label, w, CostMode.CARD_EX_STT)
            m, mx = metrics(card), metrics(ex)
            taxed = after_tax(card).after_tax_xirr_liquidated
            if bh_cagr is None:
                bh_cagr = m.cagr
            if w.name in fold_names:
                per_label_folds[label].append(_fold_result(w.name, card))
            lines.append(
                f"| {label} | {m.cagr:.2%} | {mx.cagr:.2%} | {_pct(taxed)} | "
                f"{m.max_drawdown:.2%} | **{m.ratio:.2f}** | {m.invested_share:.0%} | "
                f"{m.switches} | {m.switches_per_year:.2f} | {_inr(card.charges)} "
                f"({_inr(card.stt)}) | {_inr(card.slippage_cost)} | "
                f"{(m.cagr - bh_cagr) * 100:+.2f} pp |"
            )
        lines.append("")
    arm_folds = [ArmFolds(label, tuple(results)) for label, results in per_label_folds.items()]
    baseline = arm_folds[0]
    candidates = [a for a in arm_folds[1:] if a.label.startswith("PE<")]
    variance = trial_sharpe_variance(arm_folds, supplied=supplied, supplied_source=supplied_source)
    verdicts = [
        evaluate(candidate, baseline, trials=trials, sharpe_variance=variance)
        for candidate in candidates
    ]
    lines += [
        "### Part B verdicts — the same four criteria against buy-and-hold on F1-F3",
        "",
        "Each P/E timing arm against buy-and-hold NIFTY 500 TRI, after costs (card), on the "
        "liquidated after-tax basis (pre-registration §5). Trials N = "
        f"{trials}; Sharpe variance {variance.describe()}.",
        "",
        *verdict_table(verdicts),
        "",
    ]
    for verdict in verdicts:
        lines.append(f"- **{verdict.candidate}**:")
        for c in verdict.criteria:
            lines.append(f"  - C{c.number} {c.outcome.value}: {c.detail}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.pe_timing")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run and freeze D13, then run the 18 arms")
    run.add_argument("--workers", type=int, required=True)
    run.add_argument("--signal-panel", type=Path, default=None)
    run.add_argument("--reference-frozen", type=Path, default=None)
    rend = sub.add_parser("render", help="render the study from runs on disk")
    rend.add_argument("--trials", type=int, required=True)
    rend.add_argument("--trial-sharpes", type=Path, required=True)
    rend.add_argument("--reference-frozen", type=Path, default=None)
    for p in (run, rend):
        p.add_argument("--out", type=Path, required=True, help="study directory (not data/)")
        p.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.pe_timing``. Returns a process exit code."""
    args = _parse_args(argv)
    folds = load_folds()
    try:
        out_dir = refuse_lake_location(args.out, args.data_root)
        panel = args.signal_panel.resolve() if getattr(args, "signal_panel", None) else None
        base = study_plan(out_dir, folds, (BASELINE,), data_root=args.data_root, signal_panel=panel)
        arms = study_plan(
            out_dir, folds, PE_TIMING_ARMS, data_root=args.data_root, signal_panel=panel
        )
        if args.command == "run":
            out_dir.mkdir(parents=True, exist_ok=True)
            commit = _git_commit()
            if run_units(base, workers=args.workers):
                raise StudyError("a baseline run failed; nothing frozen (re-run to retry)")
            frozen = freeze_baseline(
                base, commit=commit, lake_last_session=_lake_last_session(args.data_root)
            )
            verify_frozen(base)
            print(f"  baseline frozen: {out_dir / FROZEN_NAME}")
            if args.reference_frozen is not None:
                for line in compare_reference(frozen, args.reference_frozen):
                    print(f"  D13 {line}")
            if run_units(arms, workers=args.workers):
                raise StudyError("an arm run failed; re-run to retry")
            return 0
        if args.trials < PREREGISTERED_MIN_TRIALS:
            raise StudyError(f"--trials is at least {PREREGISTERED_MIN_TRIALS}")
        frozen = verify_frozen(base)
        reference_lines = (
            compare_reference(frozen, args.reference_frozen)
            if args.reference_frozen is not None
            else ["(no earlier record named)"]
        )
        supplied, source = load_supplied_sharpes(args.trial_sharpes)
        everything = study_plan(
            out_dir,
            folds,
            (BASELINE, *PE_TIMING_ARMS),
            data_root=args.data_root,
            signal_panel=panel,
        )
        lake_last = _lake_last_session(args.data_root)
        switch_signals = {
            w.fold: _signals(
                w.window.start, w.window.end, data_root=args.data_root, lake_last=lake_last
            )
            for w in everything.windows
        }
        continuous = switch_signals[continuous_fold(folds)]
        parts = [
            "## The P/E series and its methodology break (generated)",
            "",
            *break_lines(data_root=args.data_root, through=lake_last, signals=continuous),
            "",
            "## Does the P/E gate agree with D13's price regime? (generated)",
            "",
            "D13's price regime: NIFTY 50 TRI at or above its 200-session mean. P/E gate: NIFTY "
            "500 P/E strictly below its trailing L-session mean. Read on the D13 rebalance "
            f"sessions (first session of each month) of {continuous.rebalances[0]} → "
            f"{continuous.rebalances[-1]}.",
            "",
            *agreement_lines(continuous),
            "",
        ]
        service, fmv = l1_grandfathering(args.data_root)
        with service:
            part_a = render_part_a(
                everything,
                folds,
                fmv,
                trials=args.trials,
                supplied=supplied,
                supplied_source=source,
                frozen=frozen,
                switch_signals=switch_signals,
                reference_lines=reference_lines,
            )
        part_b = render_part_b(
            folds=folds,
            data_root=args.data_root,
            lake_last=lake_last,
            trials=args.trials,
            supplied=supplied,
            supplied_source=source,
        )
        text = "\n".join(parts) + "\n" + part_a + "\n" + part_b
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
