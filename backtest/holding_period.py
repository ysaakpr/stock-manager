"""Holding-period study: how long must D13 be held to expect its return? Fresh-start replays.

``ops/studies/preregistration-holding-period-2026-10-19.md`` fixes the question, the span, the
intervals, the hurdles, the stability rule and every derived figure before any run. The owner
(2026-10-19): find the minimum investment period needed to get the expected return, across very
short and very long horizons, starts at any time of year, and repeated entry and exit.

**Two layers, both exact replays from cash.** Every run below starts from ₹10 lakh of cash on its
first session with D13's real entry behaviour (it rebalances on its first session and fills the
next; a book that opens with the regime off waits in cash for a risk-on month), through the same
engine, cost model, rails, book corporate actions and repo - 50 bp interest as every fold study:

1. **Named intervals** — 40 stated windows chosen by market situation (crashes, bears, bulls,
   sideways stretches, all-time-high and deep-drawdown starts, the latest months), each its own
   run at ₹10 crore/day (deciding) and ₹1 crore/day (information).
2. **The rolling grid** — one fresh-start run from every month's first session in the span to the
   span end (and, as the densification test, from every month's first session on or after the
   15th). A replay never reads past its decision session, so the run from ``S`` to the span end
   *is* the run from ``S`` to any earlier ``T`` up to ``T``; every grid cell ``(S, h)`` is struck
   from that run's own NAV and its own fills truncated at ``T``. That claim is checked, not
   assumed: every named interval and baseline window that opens on a grid start is compared with
   the grid run's prefix (:func:`validation_lines`).

**The outcome of a cell** is the investor's *walk-away* after-tax XIRR: the book is deemed sold at
``T``'s marks, the sale is charged through the shared cost model, and the deemed gains are taxed
(FIFO lots, 30 % slab, FY-end payment — the fold campaigns' profile). The decision rule's
*realised* figure (holdings still valued pre-tax) is reported beside it. The NIFTY 500 TRI is held
the same way (as an index ETF, ``backtest.ma_timing``) and cash earns repo - 50 bp, taxed at slab.

``python -m backtest.holding_period run --phase P --out DIR --workers N --signal-panel PANEL``
    Runs (or resumes) a phase: ``baseline`` (D13 on the fold test windows and the continuous
    window, both floors), ``named``, ``grid-start``, ``grid-mid``. The store's corporate actions are
    loaded once per invocation and handed to every worker.
``python -m backtest.holding_period check-baseline --out DIR --reference-frozen FILE``
    Freezes the baseline phase and compares it with an earlier study's frozen D13 — digests, and
    the run's economics (NAV path, fills, summary figures) — and stops on an economic difference.
``python -m backtest.holding_period render --out DIR``
    Replays nothing: writes ``DIR/reports/tables.md``, ``DIR/reports/cells.json`` and the SVG
    charts.

What it never does: write under the lake, tune a parameter, read a wall clock, or score a cell
that ends inside an incomplete month.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Final

from backtest.book_actions import (
    BookActionCalendar,
    corporate_actions_in_force,
    load_store_book_actions,
)
from backtest.campaign import MAX_WORKERS, _git_commit
from backtest.cash_interest import (
    RepoRateSchedule,
    accrue_cash_interest,
    load_repo_rate_schedule,
)
from backtest.fold_campaign import PROFILE, continuous_window
from backtest.folds import FOLDS_PATH, load_folds
from backtest.holding_period_charts import horizon_box_svg, probability_svg
from backtest.holding_period_stats import (
    anchor_sessions,
    chain_growth,
    growth,
    horizon_end,
    independent_windows,
    longest_under_water,
    max_drawdown,
    min_horizon,
    month_add,
    parked_share,
    permutation_spread_pvalue,
    share_meeting,
    sip_xirr,
    wilson_interval,
)
from backtest.ma_timing import (
    COST_CARD_FROM,
    ETF_ISIN,
    SLIPPAGE,
    Cadence,
    IndexSeries,
    SimResult,
    TimingRule,
    after_tax,
    load_index,
    simulate,
)
from backtest.nav import nav_file, read_nav
from backtest.run import UNIVERSE_TURNOVER_FLOOR, UniverseParameters, _L1Reader, _RegimeSource
from backtest.run_ledger import (
    RunSummary,
    ledger_path,
    load_run,
    persist_run_ledgers,
    refuse_lake_location,
    summary_path,
)
from backtest.signal_panel import SignalPanel, signal_panel_in_force
from backtest.sweep import D13_PAPER_BASELINE, HIGH_FLOOR, LOW_FLOOR, Arm, run_sweep
from backtest.tax import (
    GrandfatheringPrices,
    RunLedger,
    TaxError,
    TaxSchedule,
    compute_after_tax,
    load_tax_schedule,
    match_lots,
)
from backtest.tax_report import L1GrandfatheringPrices
from backtest.xirr import Cashflow, XIRRError, xirr
from dataplatform.logging import get_logger
from execution.broker import Side
from execution.costs import CostModel, Trade, load_rate_card

__all__ = [
    "CONFIDENCES",
    "DENSE_HORIZONS",
    "FIXED_HURDLES",
    "HORIZONS",
    "NAMED_INTERVALS",
    "SPAN_END",
    "SPAN_START",
    "NamedInterval",
    "Regime",
    "StudyError",
    "Unit",
    "baseline_units",
    "grid_units",
    "main",
    "named_units",
]

_LOG = get_logger(__name__)

# ── what the pre-registration fixes ──────────────────────────────────────────────────────────

#: The first decision session with D13's whole look-back in joinable (ISIN-bearing) L1 history:
#: ``backtest/windows.yaml`` ``full.start``. L1 before 2011-06-22 has no ISIN and is never read.
SPAN_START: Final = date(2012, 7, 4)
#: Pinned so every run ends on the same terminal session whatever the scheduler lands meanwhile:
#: the lake's last session at pre-registration is 2026-10-16, reserved as T+1 fill headroom.
SPAN_END: Final = date(2026, 10, 15)
#: The month-start grid opens on the first full month of the span.
GRID_FIRST: Final = (2012, 8)
MONTH_START: Final = 1
MID_MONTH: Final = 15
#: The third densification grid, run only if step 2 leaves a hurdle unstable (pre-registration).
DAY_EIGHT: Final = 8
GRID_NAMES: Final = {MONTH_START: "month-start", MID_MONTH: "mid-month", DAY_EIGHT: "day-8"}
HORIZONS: Final = (1, 3, 6, 9, 12, 18, 24, 36, 48, 60, 84)
DENSE_HORIZONS: Final = (1, 2, 3, 4, 6, 9, 12, 15, 18, 21, 24, 30, 36, 42, 48, 54, 60, 72, 84)
CONFIDENCES: Final = (Decimal("0.75"), Decimal("0.90"), Decimal("1"))
#: The confidence the stability rule and the headline read.
HEADLINE_CONFIDENCE: Final = Decimal("0.90")
FIXED_HURDLES: Final = (Decimal("0"), Decimal("0.12"), Decimal("0.15"))
IN_OUT_PATTERNS: Final = ((3, 3), (6, 6), (12, 12))
IN_OUT_TOTALS: Final = (24, 36, 60)
SIP_HORIZONS: Final = (12, 36, 60)
MONTH_EFFECT_HORIZONS: Final = (12, 36)
GRID_FLOOR: Final = HIGH_FLOOR
FLOORS: Final = (HIGH_FLOOR, LOW_FLOOR)
INDEX_SLUG: Final = "nifty500"
PE_SERIES: Final = "IN.NSE.NIFTY_500.PE"
REGIME_MA_DAYS: Final = 200
FROZEN_NAME: Final = "frozen-baseline.json"
PREREGISTRATION_PATH: Final = (
    Path(__file__).resolve().parent.parent
    / "ops/studies/preregistration-holding-period-2026-10-19.md"
)
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_PAISA: Final = Decimal("0.01")
_CRORE: Final = Decimal("10000000")
_ACCOUNT_STATE: Final = "MH"


class StudyError(RuntimeError):
    """The study cannot proceed as asked — fails loud, before any run or render."""


class Regime(StrEnum):
    """The market situation a named interval was chosen for (stated a priori, from the index)."""

    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    CRASH = "crash"
    RECOVERY = "recovery"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class NamedInterval:
    """One named window: a key, what it is, its dates, and the situation it was chosen for."""

    key: str
    label: str
    start: date
    end: date
    regime: Regime


def _d(text: str) -> date:
    return date.fromisoformat(text)


_B, _R, _S, _C, _V, _M = (
    Regime.BULL,
    Regime.BEAR,
    Regime.SIDEWAYS,
    Regime.CRASH,
    Regime.RECOVERY,
    Regime.MIXED,
)
#: The named intervals. Peak and trough sessions are the NIFTY 500 TRI's own (all-time highs and
#: the lows of each >10 % drawdown since 2011), the 2018 small-cap peak the NIFTY Smallcap 250's.
NAMED_INTERVALS: Final[tuple[NamedInterval, ...]] = tuple(
    NamedInterval(k, label, _d(a), _d(b), regime)
    for k, label, a, b, regime in (
        ("N01", "Full span", "2012-07-04", "2026-10-15", _M),
        ("N02", "D13 decade (F1-F3 continuous)", "2016-09-01", "2026-08-31", _M),
        ("N03", "Six-year window of the old ~23 % figures", "2019-07-01", "2026-10-15", _M),
        ("N04", "First year of the span", "2012-07-04", "2013-07-03", _S),
        ("N05", "Taper tantrum: Jan-2013 high to Aug-2013 low", "2013-01-15", "2013-08-28", _R),
        ("N06", "Aug-2013 low, 12 months", "2013-08-28", "2014-08-27", _V),
        ("N07", "2014 election year", "2014-01-01", "2014-12-31", _B),
        ("N08", "Mar-2015 all-time high to Feb-2016 low", "2015-03-03", "2016-02-25", _R),
        ("N09", "Mar-2015 all-time high, 24 months (flat)", "2015-03-03", "2017-03-02", _S),
        ("N10", "Feb-2016 low, 12 months", "2016-02-25", "2017-02-24", _V),
        ("N11", "Demonetisation, 3 months", "2016-11-09", "2017-02-08", _C),
        ("N12", "Demonetisation, 12 months", "2016-11-09", "2017-11-08", _B),
        ("N13", "2017 bull year", "2017-01-02", "2017-12-29", _B),
        ("N14", "2018 small/mid-cap bear: Jan-2018 small-cap peak", "2018-01-15", "2019-08-30", _R),
        ("N15", "Jan-2018 high, 24 months (flat broad market)", "2018-01-23", "2020-01-17", _S),
        ("N16", "IL&FS sell-off, 6 months", "2018-09-21", "2019-03-20", _C),
        ("N17", "Aug-2018 all-time high, 12 months", "2018-08-31", "2019-08-30", _R),
        ("N18", "COVID: from just before the peak, calendar 2020", "2020-01-01", "2020-12-31", _M),
        ("N19", "COVID: at the Jan-2020 peak, 3 months", "2020-01-17", "2020-04-16", _C),
        ("N20", "COVID: at the Jan-2020 peak, 24 months", "2020-01-17", "2022-01-17", _M),
        ("N21", "COVID: at the Mar-2020 trough, 3 months", "2020-03-23", "2020-06-22", _V),
        ("N22", "COVID: at the Mar-2020 trough, 12 months", "2020-03-23", "2021-03-22", _V),
        ("N23", "COVID: in the recovery, Jun-2020, 12 months", "2020-06-01", "2021-05-31", _V),
        ("N24", "2021 bull run to the Oct-2021 high", "2021-01-01", "2021-10-18", _B),
        ("N25", "Oct-2021 all-time high, 12 months", "2021-10-18", "2022-10-17", _R),
        ("N26", "2022: sideways, rate hikes", "2022-01-03", "2022-12-30", _S),
        ("N27", "Jun-2022 low, 12 months", "2022-06-20", "2023-06-19", _V),
        (
            "N28",
            "2023-24 small-cap melt-up: Mar-2023 low to Sep-2024 peak",
            "2023-03-28",
            "2024-09-26",
            _B,
        ),
        ("N29", "Sep-2024 all-time high to Feb-2025 low", "2024-09-26", "2025-02-28", _R),
        ("N30", "Sep-2024 all-time high, 12 months", "2024-09-26", "2025-09-25", _R),
        ("N31", "Sep-2024 all-time high to span end", "2024-09-26", "2026-10-15", _M),
        ("N32", "Feb-2025 low, 12 months", "2025-02-28", "2026-02-27", _V),
        ("N33", "Latest year", "2025-10-15", "2026-10-15", _M),
        ("N34", "Latest 6 months", "2026-04-15", "2026-10-15", _M),
        ("N35", "Latest 3 months", "2026-07-15", "2026-10-15", _M),
        ("N36", "First 7 years of the span", "2012-07-04", "2019-07-03", _M),
        ("N37", "Mar-2015 all-time high, 5 years", "2015-03-03", "2020-03-02", _M),
        ("N38", "COVID peak, 5 years", "2020-01-17", "2025-01-16", _M),
        ("N39", "COVID trough, 5 years", "2020-03-23", "2025-03-21", _M),
        ("N40", "2017-2021, 5 years", "2017-01-02", "2021-12-31", _M),
    )
)
#: The interval whose walk-away XIRR is the strategy's own long-run "expected return".
FULL_SPAN_KEY: Final = "N01"


# ── plan and runs ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Unit:
    """One replay window, run at every floor in ``floors`` over one shared lake pass."""

    name: str
    start: date
    end: date
    floors: tuple[Decimal, ...]


def baseline_units() -> tuple[Unit, ...]:
    """D13's frozen-check windows: each fold's test window and the continuous one, both floors."""
    folds = load_folds()
    units = [Unit(f"{f.name}-test", f.test.start, f.test.end, FLOORS) for f in folds.folds]
    whole = continuous_window(folds)
    units.append(Unit("F1-F3-continuous", whole.start, whole.end, FLOORS))
    return tuple(units)


def named_units() -> tuple[Unit, ...]:
    """Every named interval at both floors."""
    return tuple(Unit(n.key, n.start, n.end, FLOORS) for n in NAMED_INTERVALS)


def grid_starts(sessions: Sequence[date], anchor_day: int) -> tuple[date, ...]:
    """Every grid start: the anchor session of each month from :data:`GRID_FIRST` whose one-month
    cell is complete by :data:`SPAN_END`."""
    anchors = anchor_sessions([s for s in sessions if s <= SPAN_END], anchor_day)
    first = anchors[GRID_FIRST]
    out = []
    for key in sorted(anchors):
        start = anchors[key]
        if start < first:
            continue
        if horizon_end(sessions, anchors, start, 1, SPAN_END) is not None:
            out.append(start)
    return tuple(out)


def grid_units(sessions: Sequence[date], anchor_day: int) -> tuple[Unit, ...]:
    """One ₹10-crore run per grid start, from the start to :data:`SPAN_END`."""
    tag = "start" if anchor_day == MONTH_START else f"day{anchor_day}"
    return tuple(
        Unit(f"grid-{tag}-{s.isoformat()}", s, SPAN_END, (GRID_FLOOR,))
        for s in grid_starts(sessions, anchor_day)
    )


@dataclass(frozen=True, slots=True)
class Plan:
    """What one ``run`` invocation replays and where it keeps it."""

    out_dir: Path
    arm: Arm
    units: tuple[Unit, ...]
    data_root: Path | None
    signal_panel: Path | None
    universe: str = UNIVERSE_TURNOVER_FLOOR

    @property
    def order(self) -> tuple[int, ...]:
        """Longest window first, so the workers finish close together."""
        spans = [(u.end - u.start).days for u in self.units]
        return tuple(sorted(range(len(self.units)), key=lambda i: (-spans[i], i)))


def run_unit(plan: Plan, index: int, actions: BookActionCalendar) -> tuple[str, int, int]:
    """Run (or resume) one unit; ``(name, resumed, failed)``. Module-level for spawn."""
    unit = plan.units[index]
    panel = SignalPanel(plan.signal_panel) if plan.signal_panel is not None else None
    began = time.perf_counter()
    with ExitStack() as stack:
        stack.enter_context(corporate_actions_in_force(actions, apply_to_book=True))
        stack.enter_context(accrue_cash_interest(load_repo_rate_schedule()))
        stack.enter_context(persist_run_ledgers(plan.out_dir))
        stack.enter_context(signal_panel_in_force(panel))
        result = run_sweep(
            start=unit.start,
            end=unit.end,
            arms=(plan.arm,),
            floors=unit.floors,
            data_root=plan.data_root,
            universe_name=plan.universe,
        )
    failed = 0
    for row in result.rows:
        if not row.ok:
            failed += 1
            _LOG.error("holding_period.run_failed", unit=unit.name, error=row.error)
    _LOG.info(
        "holding_period.unit_done",
        unit=unit.name,
        resumed=result.resumed,
        failed=failed,
        seconds=round(time.perf_counter() - began, 1),
    )
    return unit.name, result.resumed, failed


def run_plan(plan: Plan, *, workers: int, actions: BookActionCalendar) -> int:
    """Every unit of ``plan``; returns the number of failed runs."""
    if not 1 <= workers <= MAX_WORKERS:
        raise StudyError(f"workers must be 1..{MAX_WORKERS} on this box, got {workers}")
    _LOG.info("holding_period.plan", units=len(plan.units), workers=workers, arm=plan.arm.label)
    if workers == 1:
        outcomes = [run_unit(plan, i, actions) for i in plan.order]
    else:
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = [pool.submit(run_unit, plan, i, actions) for i in plan.order]
            outcomes = [f.result() for f in futures]
    return sum(failed for _, _, failed in outcomes)


# ── the persisted runs, indexed by what they are (no digest recomputation, no database) ──────


def universe_key(floor: Decimal, universe: str = UNIVERSE_TURNOVER_FLOOR) -> str:
    """The run spec's ``universe`` field for ``floor``."""
    return repr(UniverseParameters.for_universe(universe, median_turnover_floor=floor))


@dataclass(frozen=True, slots=True)
class RunKey:
    """A persisted run's identity as the study names it: requested window, arm, floor."""

    start: date
    end: date
    parameters: str
    universe: str


class RunIndex:
    """Every run summary under ``out_dir/runs``, by :class:`RunKey`; fails loud on a duplicate key
    (two book-action states for one window) unless the caller asks for the newest."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self._by_key: dict[RunKey, list[str]] = {}
        self.book_actions: dict[str, int] = {}
        runs = out_dir / "runs"
        for path in sorted(runs.glob("*.json")) if runs.is_dir() else ():
            doc = json.loads(path.read_text(encoding="utf-8"))
            spec = doc["spec"]
            key = RunKey(
                date.fromisoformat(spec["start"]),
                date.fromisoformat(spec["end"]),
                spec["parameters"],
                spec["universe"],
            )
            self._by_key.setdefault(key, []).append(doc["digest"])
            actions = spec.get("book_actions", "none")
            self.book_actions[actions] = self.book_actions.get(actions, 0) + 1

    def digest(self, key: RunKey) -> str | None:
        found = self._by_key.get(key)
        if not found:
            return None
        if len(found) > 1:
            raise StudyError(
                f"{len(found)} runs for {key.start}..{key.end} {key.universe[:60]}: the store's "
                "corporate actions changed between them; re-run on one state"
            )
        return found[0]


@dataclass(frozen=True, slots=True)
class RunData:
    """One persisted run: its summary, its fills, and its NAV path."""

    digest: str
    summary: RunSummary
    ledger: RunLedger
    nav: tuple[tuple[date, Decimal], ...]


def load_run_data(out_dir: Path, digest: str) -> RunData:
    loaded = load_run(out_dir, digest)
    if loaded is None:
        raise StudyError(f"run {digest[:12]} is not complete under {out_dir}")
    summary, ledger = loaded
    nav = read_nav(nav_file(out_dir, digest), digest=digest)
    return RunData(digest, summary, ledger, nav.points)


# ── the frozen baseline and its check against an earlier study ──────────────────────────────


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def economics(out_dir: Path, digest: str) -> dict[str, str]:
    """A run's economic fingerprint: its NAV points, its fills and credits, its summary figures —
    everything but the digest and the run-spec labels it is filed under."""
    run = load_run_data(out_dir, digest)
    nav = json.dumps([[d.isoformat(), str(v)] for d, v in run.nav]).encode()
    ledger_doc = json.loads(ledger_path(out_dir, digest).read_text(encoding="utf-8"))
    ledger_doc.pop("source", None)
    fills = json.dumps(ledger_doc, sort_keys=True).encode()
    s = run.summary
    figures = "|".join(
        str(x)
        for x in (
            s.start,
            s.terminal,
            s.sessions,
            s.xirr,
            s.max_drawdown,
            s.final_nav,
            s.total_charges,
            s.round_trips,
            s.replay_digest,
        )
    )
    return {
        "nav_points_sha256": _sha256_bytes(nav),
        "ledger_content_sha256": _sha256_bytes(fills),
        "figures": figures,
    }


def freeze_baseline(out_dir: Path, arm: Arm, *, commit: str, lake_last: date) -> dict[str, Any]:
    """Write ``frozen-baseline.json`` for the finished baseline phase; never rewrites a different
    record."""
    index = RunIndex(out_dir)
    runs: list[dict[str, str]] = []
    missing: list[str] = []
    for unit in baseline_units():
        for floor in unit.floors:
            key = RunKey(unit.start, unit.end, repr(arm.v2), universe_key(floor))
            digest = index.digest(key)
            if digest is None or load_run(out_dir, digest) is None:
                missing.append(f"{unit.name}@{floor}")
                continue
            files = {
                "summary": summary_path(out_dir, digest),
                "ledger": ledger_path(out_dir, digest),
                "nav": nav_file(out_dir, digest),
            }
            runs.append(
                {
                    "window": unit.name,
                    "floor": str(floor),
                    "digest": digest,
                    **{f"{k}_sha256": _sha256(p) for k, p in files.items()},
                    **economics(out_dir, digest),
                }
            )
    if missing:
        raise StudyError(f"cannot freeze: baseline run(s) missing: {missing}")
    document = {
        "version": 1,
        "baseline": arm.label,
        "commit": commit,
        "lake_last_session": lake_last.isoformat(),
        "span_end": SPAN_END.isoformat(),
        "folds_sha256": _sha256(FOLDS_PATH),
        "preregistration_sha256": _sha256(PREREGISTRATION_PATH),
        "cash_interest": load_repo_rate_schedule().identity(),
        "runs": runs,
    }
    path = out_dir / FROZEN_NAME
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["runs"] != runs:
            raise StudyError(f"{path} records a different frozen baseline; it is never rewritten")
        return dict(existing)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return document


_REFERENCE_WINDOWS: Final = {
    ("F1", "test"): "F1-test",
    ("F2", "test"): "F2-test",
    ("F3", "test"): "F3-test",
    ("F1-F3", "continuous"): "F1-F3-continuous",
}


def compare_reference(frozen: Mapping[str, Any], reference: Path) -> list[str]:
    """Each baseline run against an earlier study's frozen D13 (``fold``/``role`` keyed records).

    Digests are reported; the *economics* decide (NAV points, fills and credits, summary figures,
    recomputed from the reference's own run directory). Pre-registered rule: the run is store
    drift — reported with its first divergence and the credits that moved, and the baseline is
    re-frozen on the current store — when a *credit* (a dividend or a corporate event in the run's
    ledger) moved and the NAV first diverges no earlier than the first moved credit, with every
    fill before that date identical (later fills may differ: a rupee more or less of cash resizes
    a later buy). Anything else — a fill differing first, or a NAV moving with no credit moved —
    is a different D13 and raises ``StudyError``.
    """
    other = json.loads(reference.read_text(encoding="utf-8"))
    ref_dir = reference.parent
    out_dir = Path(frozen["_out_dir"])
    theirs = {
        (_REFERENCE_WINDOWS[(r["fold"], r["role"])], r["floor"]): r
        for r in other["runs"]
        if (r["fold"], r["role"]) in _REFERENCE_WINDOWS
    }
    lines: list[str] = []
    problems: list[str] = []
    for run in frozen["runs"]:
        where = f"{run['window']} @ ₹{Decimal(run['floor']) / _CRORE:.0f} cr"
        match = theirs.get((run["window"], run["floor"]))
        if match is None:
            problems.append(f"{where}: not in {reference.name}")
            continue
        mine_doc = json.loads(summary_path(out_dir, run["digest"]).read_text(encoding="utf-8"))
        ref_doc = json.loads(summary_path(ref_dir, match["digest"]).read_text(encoding="utf-8"))
        moved = sorted(
            k
            for k in set(mine_doc["spec"]) | set(ref_doc["spec"])
            if mine_doc["spec"].get(k) != ref_doc["spec"].get(k)
        )
        digest_note = (
            "digest identical"
            if match["digest"] == run["digest"]
            else f"digest differs (spec fields moved: {', '.join(moved) or 'none'})"
        )
        their_econ = economics(ref_dir, match["digest"])
        if all(their_econ[k] == run[k] for k in their_econ):
            lines.append(f"{where}: economics identical (NAV path, fills, figures); {digest_note}")
            continue
        a = load_run_data(ref_dir, match["digest"])
        b = load_run_data(out_dir, run["digest"])
        theirs_nav = dict(a.nav)
        first = next((d for d, v in b.nav if theirs_nav.get(d) != v), None)
        moved_on: list[date] = []
        credits = []
        for name in ("dividends", "corporate_events"):
            sa = {repr(x): x for x in getattr(a.ledger, name)}
            sb = {repr(x): x for x in getattr(b.ledger, name)}
            gone = [sa[k] for k in sorted(set(sa) - set(sb))]
            came = [sb[k] for k in sorted(set(sb) - set(sa))]
            moved_on += [getattr(x, "received", None) or x.ex_date for x in (*gone, *came)]
            if gone or came:
                credits.append(f"{name}: {len(gone)} only earlier, {len(came)} only now")
                credits += [f"e.g. dropped {gone[0]!r}"] if gone else []
                credits += [f"e.g. added {came[0]!r}"] if came else []
        first_credit = min(moved_on) if moved_on else None
        fill_diff = next(
            (
                ta.trade_date
                for ta, tb in zip(a.ledger.trades, b.ledger.trades, strict=False)
                if ta != tb
            ),
            None,
        )
        if fill_diff is None and len(a.ledger.trades) != len(b.ledger.trades):
            shorter = min(len(a.ledger.trades), len(b.ledger.trades))
            fill_diff = (a.ledger.trades if len(a.ledger.trades) > shorter else b.ledger.trades)[
                shorter
            ].trade_date
        drift = (
            first_credit is not None
            and first is not None
            and first >= first_credit
            and (fill_diff is None or fill_diff >= first)
        )
        if not drift:
            problems.append(f"{where}: fills or NAV moved with no earlier credit moved")
            lines.append(
                f"{where}: DIFFERENT D13 (NAV first differs {first}, first fill difference "
                f"{fill_diff}, first moved credit {first_credit}); {digest_note}"
            )
            continue
        fills_b = b.ledger.trades
        lines.append(
            f"{where}: first moved credit {first_credit}; NAV first differs {first}; first fill "
            f"difference {fill_diff or 'none'} ({len(fills_b)} fills now); terminal "
            f"₹{a.nav[-1][1]:,.2f} then, ₹{b.nav[-1][1]:,.2f} now; pre-tax XIRR "
            f"{a.summary.xirr:.4%} then, {b.summary.xirr:.4%} now; credits moved: "
            f"{'; '.join(credits)}; {digest_note} — store drift, re-frozen on the current store"
        )
    if problems:
        raise StudyError("D13 is not the earlier frozen D13: " + "; ".join(problems[:6]))
    return lines


# ── render: the market context every cell is scored against ────────────────────────────────


@dataclass(frozen=True, slots=True)
class Outcome:
    """One scored window of one run (a grid cell, or a named interval's whole run)."""

    start: date
    end: date
    pre_tax_xirr: Decimal
    walkaway_xirr: Decimal | None
    realised_xirr: Decimal | None
    tax_error: str | None
    max_drawdown: Decimal
    under_water_days: int
    parked: Decimal
    trades: int
    exit_charges: Decimal
    tri_pre_tax: Decimal
    tri_walkaway: Decimal | None
    cash_pre_tax: Decimal
    cash_after_tax: Decimal | None

    @property
    def walkaway_multiple(self) -> Decimal | None:
        """What each rupee became, after exit and tax, over the window (1.10 = +10 %)."""
        if self.walkaway_xirr is None:
            return None
        return growth(self.walkaway_xirr, self.start, self.end)

    def to_document(self) -> dict[str, str | int | None]:
        def text(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "pre_tax_xirr": str(self.pre_tax_xirr),
            "walkaway_xirr": text(self.walkaway_xirr),
            "realised_xirr": text(self.realised_xirr),
            "tax_error": self.tax_error,
            "max_drawdown": str(self.max_drawdown),
            "under_water_days": self.under_water_days,
            "parked": str(self.parked),
            "trades": self.trades,
            "exit_charges": str(self.exit_charges),
            "tri_pre_tax": str(self.tri_pre_tax),
            "tri_walkaway": text(self.tri_walkaway),
            "cash_pre_tax": str(self.cash_pre_tax),
            "cash_after_tax": text(self.cash_after_tax),
        }


@dataclass(slots=True)
class Market:
    """The lake facts every outcome reads: calendar, marks, regime, NIFTY 500 TRI, cash, tax."""

    data_root: Path | None
    sessions: tuple[date, ...]
    reader: _L1Reader
    regime: _RegimeSource
    index: IndexSeries
    repo: RepoRateSchedule
    cost_model: CostModel
    schedule: TaxSchedule
    fmv: GrandfatheringPrices
    pe: dict[date, Decimal]
    _closes: dict[date, Mapping[str, Decimal]] = field(default_factory=dict)
    _risk_on: dict[date, bool] = field(default_factory=dict)
    _bench: dict[tuple[date, date], tuple[Decimal, Decimal | None, Decimal, Decimal | None]] = (
        field(default_factory=dict)
    )

    @classmethod
    def open(cls, data_root: Path | None) -> Market:
        from dataplatform.store.macro_series import series_history

        reader = _L1Reader(data_root=data_root)
        sessions = tuple(s for s in reader.all_sessions() if s <= SPAN_END)
        schedule = load_tax_schedule()
        pe = {
            f.period_end: f.value for f in series_history(PE_SERIES, SPAN_END, data_root=data_root)
        }
        return cls(
            data_root=data_root,
            sessions=sessions,
            reader=reader,
            regime=_RegimeSource.published(
                through=SPAN_END, ma_days=REGIME_MA_DAYS, data_root=data_root
            ),
            index=load_index(INDEX_SLUG, through=SPAN_END, data_root=data_root),
            repo=load_repo_rate_schedule(),
            cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
            schedule=schedule,
            fmv=L1GrandfatheringPrices(fmv_date=schedule.grandfather_fmv_date, data_root=data_root),
            pe=pe,
        )

    def close(self) -> None:
        self.reader.close()

    def closes(self, session: date) -> Mapping[str, Decimal]:
        if session not in self._closes:
            self._closes[session] = self.reader.closes_on(session)
        return self._closes[session]

    def mark(self, isin: str, session: date, floor: date) -> Decimal | None:
        """``isin``'s last close (EQ, else BE/BZ) on or before ``session`` and not before
        ``floor`` — the run's own terminal-price rule (``backtest.run._terminal_prices``)."""
        for position in range(bisect_right(self.sessions, session) - 1, -1, -1):
            day = self.sessions[position]
            if day < floor:
                break
            price = self.closes(day).get(isin)
            if price is None:
                restricted = self.reader.restricted_close(isin, day)
                price = None if restricted is None else restricted[1]
            if price is not None:
                return price
        return None

    def risk_on(self, session: date) -> bool:
        if session not in self._risk_on:
            self._risk_on[session] = self.regime.reading(session).risk_on
        return self._risk_on[session]

    def window(self, start: date, end: date) -> list[date]:
        return [s for s in self.sessions if start <= s <= end]

    def benchmarks(
        self, start: date, end: date
    ) -> tuple[Decimal, Decimal | None, Decimal, Decimal | None]:
        """``(TRI pre-tax, TRI walk-away, cash pre-tax, cash after-tax)`` XIRRs over the window."""
        key = (start, end)
        if key not in self._bench:
            tri = simulate(
                self.index,
                start=start,
                end=end,
                rule=None,
                cost_model=self.cost_model,
                repo=self.repo,
            )
            parked: dict[date, tuple[bool | None, Decimal | None]] = dict.fromkeys(
                self.index.sessions, (False, None)
            )
            cash = simulate(
                self.index,
                start=start,
                end=end,
                rule=TimingRule(ma_days=REGIME_MA_DAYS, cadence=Cadence.MONTHLY),
                cost_model=self.cost_model,
                repo=self.repo,
                decided=parked,
            )
            cash_taxed = after_tax(cash, schedule=self.schedule)
            self._bench[key] = (
                _single_deposit_xirr(tri),
                self._tri_walkaway(tri),
                _single_deposit_xirr(cash),
                cash_taxed.after_tax_xirr_liquidated,
            )
        return self._bench[key]

    def _tri_walkaway(self, run: SimResult) -> Decimal | None:
        """Buy-and-hold TRI, sold at the window's last close (card + the stated slippage), taxed."""
        terminal_cash = run.nav[-1][1] - run.terminal_units * run.terminal_unit_price
        proceeds = _ZERO
        if run.terminal_units > 0:
            price = (run.terminal_unit_price * (_ONE - SLIPPAGE)).quantize(_PAISA)
            when = max(run.end, COST_CARD_FROM)
            charged = self.cost_model.charge(
                Trade(ETF_ISIN, when, Side.SELL, run.terminal_units, price)
            )
            proceeds = charged.net_amount
        taxed = after_tax(
            replace(run, nav=(*run.nav[:-1], (run.end, terminal_cash + proceeds))),
            schedule=self.schedule,
        )
        return taxed.after_tax_xirr_liquidated

    def index_drawdown(self, session: date) -> Decimal:
        """The NIFTY 500 TRI's distance below its running all-time high on ``session``."""
        peak = _ZERO
        level = _ZERO
        for point in self.index.points:
            if point.session > session:
                break
            level = point.level
            peak = max(peak, level)
        return _ZERO if peak == _ZERO else (peak - level) / peak

    def pe_on(self, session: date) -> Decimal | None:
        known = [d for d in self.pe if d <= session]
        if not known:
            return None
        last = max(known)
        return self.pe[last] if (session - last).days <= 7 else None


def _single_deposit_xirr(run: SimResult) -> Decimal:
    return xirr([Cashflow(run.start, -run.opening), Cashflow(run.end, run.nav[-1][1])])


def truncate_ledger(ledger: RunLedger, end: date) -> RunLedger:
    """``ledger`` as it stood at ``end``'s close: every fill, credit and event on or before it."""
    return replace(
        ledger,
        source=f"{ledger.source} truncated at {end.isoformat()}",
        trades=tuple(t for t in ledger.trades if t.trade_date <= end),
        external_flows=tuple(f for f in ledger.external_flows if f.when <= end),
        dividends=tuple(d for d in ledger.dividends if d.received <= end),
        corporate_events=tuple(e for e in ledger.corporate_events if e.ex_date <= end),
        interest=tuple(i for i in ledger.interest if i.received <= end),
        terminal_date=end,
    )


def outcome(run: RunData, end: date, market: Market) -> Outcome:
    """Score ``run`` over ``[its start, end]`` — a grid cell, or (``end`` = its terminal) a run.

    The NAV at ``end`` and the fills through ``end`` are the run's own (a replay never reads past
    its decision session, so they are what a run ending at ``end`` would have held — checked in
    :func:`validation_lines`). The walk-away figure sells every open lot at ``end``'s mark through
    the shared cost model and taxes the deemed sale.
    """
    points = [(d, v) for d, v in run.nav if d <= end]
    if not points or points[-1][0] != end:
        raise StudyError(f"run {run.digest[:12]} has no NAV point on {end}")
    start = points[0][0]
    terminal_nav = points[-1][1]
    trunc = truncate_ledger(run.ledger, end)
    pre_tax = xirr([*trunc.external_flows, Cashflow(end, terminal_nav)])
    walkaway: Decimal | None = None
    realised: Decimal | None = None
    error: str | None = None
    exit_charges = _ZERO
    try:
        _, open_lots = match_lots(trunc, market.schedule, market.fmv)
        prices: dict[str, Decimal] = {}
        sells: list[Trade] = []
        for isin in sorted(open_lots):
            qty = sum(lot.quantity for lot in open_lots[isin])
            if qty <= 0:
                continue
            price = market.mark(isin, end, start)
            if price is None:
                raise TaxError(f"no mark for held {isin} on or before {end}")
            prices[isin] = price
            sells.append(Trade(isin, end, Side.SELL, qty, price))
        exit_charges = sum((c.total for c in market.cost_model.charge_all(sells)), _ZERO)
        realised_run = compute_after_tax(
            replace(trunc, terminal_nav=terminal_nav, terminal_prices=prices),
            PROFILE,
            schedule=market.schedule,
            fmv=market.fmv,
        )
        walk_run = compute_after_tax(
            replace(trunc, terminal_nav=terminal_nav - exit_charges, terminal_prices=prices),
            PROFILE,
            schedule=market.schedule,
            fmv=market.fmv,
        )
        realised = realised_run.after_tax_xirr_realised
        walkaway = walk_run.after_tax_xirr_liquidated
        error = walk_run.liquidation_error or realised_run.realised_xirr_error
    except (TaxError, XIRRError) as failure:
        error = str(failure)
    sessions = [d for d, _ in points]
    rebalances = [start] + [s for s in _month_firsts(sessions) if s > start]
    tri_pre, tri_walk, cash_pre, cash_after = market.benchmarks(start, end)
    return Outcome(
        start=start,
        end=end,
        pre_tax_xirr=pre_tax,
        walkaway_xirr=walkaway,
        realised_xirr=realised,
        tax_error=error,
        max_drawdown=max_drawdown([v for _, v in points]),
        under_water_days=longest_under_water(points),
        parked=parked_share(sessions, rebalances, {r: market.risk_on(r) for r in rebalances}),
        trades=len(trunc.trades),
        exit_charges=exit_charges,
        tri_pre_tax=tri_pre,
        tri_walkaway=tri_walk,
        cash_pre_tax=cash_pre,
        cash_after_tax=cash_after,
    )


def _month_firsts(sessions: Iterable[date]) -> list[date]:
    seen: set[tuple[int, int]] = set()
    out: list[date] = []
    for s in sessions:
        if (s.year, s.month) not in seen:
            seen.add((s.year, s.month))
            out.append(s)
    return out


def approx_pre_tax(nav: Mapping[date, Decimal], start: date, end: date) -> Decimal:
    """The continuous run's NAV slice ``[start, end]`` as if bought at ``start``'s close."""
    return xirr([Cashflow(start, -nav[start]), Cashflow(end, nav[end])])


# ── render: the cells ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Cell:
    """One grid cell: which grid, its start, its horizon (``0`` = to the span end), its outcome."""

    grid: str
    months: int
    outcome: Outcome
    approx_pre_tax: Decimal | None = None


def grid_cells(
    market: Market,
    index: RunIndex,
    arm: Arm,
    anchor_day: int,
    horizons: Sequence[int],
    approx_nav: Mapping[date, Decimal] | None,
) -> tuple[list[Cell], list[str]]:
    """Every scorable cell of one grid; missing runs are listed, never silently skipped."""
    grid = GRID_NAMES[anchor_day]
    anchors = anchor_sessions(market.sessions, anchor_day)
    cells: list[Cell] = []
    missing: list[str] = []
    for start in grid_starts(market.sessions, anchor_day):
        digest = index.digest(RunKey(start, SPAN_END, repr(arm.v2), universe_key(GRID_FLOOR)))
        if digest is None or load_run(index.out_dir, digest) is None:
            missing.append(start.isoformat())
            continue
        run = load_run_data(index.out_dir, digest)
        for months in (*horizons, 0):
            end = (
                run.nav[-1][0]
                if months == 0
                else horizon_end(market.sessions, anchors, start, months, SPAN_END)
            )
            if end is None:
                continue
            scored = outcome(run, end, market)
            approx = (
                approx_pre_tax(approx_nav, start, end)
                if approx_nav is not None and start in approx_nav and end in approx_nav
                else None
            )
            cells.append(Cell(grid, months, scored, approx))
        _LOG.info("holding_period.grid_start_scored", grid=grid, start=start.isoformat())
    return cells, missing


# ── render: tables ───────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _pp(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f} pp"


def _quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise StudyError("quantile of nothing")
    pos = q * Decimal(len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - Decimal(lo)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _median(values: Sequence[Decimal]) -> Decimal:
    return _quantile(values, Decimal("0.5"))


def _horizon_label(months: int) -> str:
    return "to span end" if months == 0 else f"{months} m"


@dataclass(frozen=True, slots=True)
class Hurdle:
    """A bar a cell's figure must reach: its name and how it reads the cell."""

    name: str
    key: str

    def pair(
        self, cell: Cell, expected: Decimal, *, pre_tax: bool = False
    ) -> tuple[Decimal | None, Decimal | None]:
        o = cell.outcome
        value = o.pre_tax_xirr if pre_tax else o.walkaway_xirr
        if self.key == "expected":
            return value, expected
        if self.key == "cash":
            return value, o.cash_pre_tax if pre_tax else o.cash_after_tax
        if self.key == "tri":
            return value, o.tri_pre_tax if pre_tax else o.tri_walkaway
        return value, Decimal(self.key)

    def pair_approx(self, cell: Cell, expected: Decimal) -> tuple[Decimal | None, Decimal | None]:
        """The continuous-run slice against the pre-tax version of this hurdle."""
        value = cell.approx_pre_tax
        o = cell.outcome
        if self.key == "expected":
            return value, expected
        if self.key == "cash":
            return value, o.cash_pre_tax
        if self.key == "tri":
            return value, o.tri_pre_tax
        return value, Decimal(self.key)


HURDLES: Final = (
    Hurdle("D13's own full-span return (E)", "expected"),
    Hurdle("Cash (repo - 50 bp, after slab tax)", "cash"),
    Hurdle("NIFTY 500 TRI, same window", "tri"),
    Hurdle("No loss (≥ 0 %)", "0"),
    Hurdle("12 % a year", "0.12"),
    Hurdle("15 % a year", "0.15"),
)


def shares_by_horizon(
    cells: Sequence[Cell],
    hurdle: Hurdle,
    expected: Decimal,
    horizons: Sequence[int],
    *,
    pre_tax: bool = False,
    approx: bool = False,
) -> dict[int, tuple[int, int]]:
    """``horizon -> (met, scored)`` for one hurdle over ``cells``."""
    out: dict[int, tuple[int, int]] = {}
    for h in horizons:
        pairs = [
            hurdle.pair_approx(c, expected) if approx else hurdle.pair(c, expected, pre_tax=pre_tax)
            for c in cells
            if c.months == h
        ]
        out[h] = share_meeting(pairs)
    return out


def _span_months() -> int:
    return (SPAN_END.year - GRID_FIRST[0]) * 12 + SPAN_END.month - GRID_FIRST[1]


def probability_lines(
    cells: Sequence[Cell], expected: Decimal, horizons: Sequence[int]
) -> list[str]:
    """Per hurdle: horizon x P(meet), its 90 % Wilson interval on the non-overlapping count."""
    lines: list[str] = []
    span = _span_months()
    for hurdle in HURDLES:
        shares = shares_by_horizon(cells, hurdle, expected, horizons)
        lines += [
            f"**{hurdle.name}**" + (f" — E = {expected:.2%}" if hurdle.key == "expected" else ""),
            "",
            "| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval "
            "(on non-overlapping) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for h in horizons:
            met, scored = shares[h]
            if scored == 0:
                continue
            eff = independent_windows(span, h)
            lo, hi = wilson_interval(met, scored, eff)
            lines.append(
                f"| {h} m | {scored} | {met} | {Decimal(met) / Decimal(scored):.0%} | {eff} | "
                f"{lo:.0%} to {hi:.0%} |"
            )
        lines.append("")
    return lines


def min_horizon_table(
    grids: Mapping[str, Sequence[Cell]],
    expected: Decimal,
    expected_pre: Decimal,
) -> tuple[list[str], dict[str, bool]]:
    """The headline table — minimum horizon per hurdle and confidence — and the stability rule."""
    lines = [
        "| Hurdle | 75 % | 90 % | 100 % (worst case) | 90 %, dense horizons | 90 %, mid-month "
        "grid | 90 %, all grids pooled | 90 % pre-tax, exact | 90 % pre-tax, continuous-run "
        "slice | "
        "Stable? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    stable: dict[str, bool] = {}
    start = grids["month-start"]
    mid = grids.get("mid-month", [])
    others = [cells for name, cells in grids.items() if name != "month-start" and cells]
    both = [c for cells in grids.values() for c in cells]

    def mh(
        cells: Sequence[Cell], hurdle: Hurdle, conf: Decimal, horizons: Sequence[int], **kw: bool
    ) -> int | None:
        exp = expected_pre if kw.get("pre_tax") or kw.get("approx") else expected
        return min_horizon(shares_by_horizon(cells, hurdle, exp, horizons, **kw), conf)

    def cell(h: int | None) -> str:
        return "not within 84 m" if h is None else f"{h} m"

    for hurdle in HURDLES:
        coarse = {c: mh(start, hurdle, c, HORIZONS) for c in CONFIDENCES}
        dense = mh(start, hurdle, HEADLINE_CONFIDENCE, DENSE_HORIZONS)
        mid_h = mh(mid, hurdle, HEADLINE_CONFIDENCE, HORIZONS) if mid else None
        both_h = mh(both, hurdle, HEADLINE_CONFIDENCE, HORIZONS)
        exact_pre = mh(start, hurdle, HEADLINE_CONFIDENCE, HORIZONS, pre_tax=True)
        approx_pre = mh(start, hurdle, HEADLINE_CONFIDENCE, HORIZONS, approx=True)
        headline = coarse[HEADLINE_CONFIDENCE]
        s1 = (
            bool(others)
            and all(mh(o, hurdle, HEADLINE_CONFIDENCE, HORIZONS) == headline for o in others)
            and both_h == headline
        )
        s3 = exact_pre == approx_pre
        s4 = _dense_consistent(headline, dense)
        stable[hurdle.name] = s1 and s3 and s4
        verdict = (
            "yes"
            if stable[hurdle.name]
            else "NO ("
            + ", ".join(
                n
                for n, ok in (("S1 grids", s1), ("S3 approximation", s3), ("S4 dense", s4))
                if not ok
            )
            + ")"
        )
        lines.append(
            f"| {hurdle.name} | {cell(coarse[CONFIDENCES[0]])} | {cell(headline)} | "
            f"{cell(coarse[CONFIDENCES[2]])} | {cell(dense)} | "
            f"{cell(mid_h) if mid else 'not run'} | {cell(both_h)} | {cell(exact_pre)} | "
            f"{cell(approx_pre)} | {verdict} |"
        )
    return lines, stable


def _dense_consistent(coarse: int | None, dense: int | None) -> bool:
    """S4: the dense-grid answer lies in (the coarse horizon below the coarse answer, the coarse
    answer]; both ``None`` agree."""
    if coarse is None or dense is None:
        return coarse is None and dense is None
    below = [h for h in HORIZONS if h < coarse]
    floor = below[-1] if below else 0
    return floor < dense <= coarse


def distribution_lines(cells: Sequence[Cell], horizons: Sequence[int]) -> list[str]:
    """Walk-away after-tax XIRR by horizon: spread, worst and best case, drawdown, water, TRI."""
    lines = [
        "| Horizon | Windows | Worst | 10th pct | Median | 90th pct | Best | Worst cumulative "
        "(start) | Best cumulative (start) | P(loss) | Worst max DD | Longest under water | "
        "Median TRI walk-away | Median excess vs TRI | Median parked share |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
        "--- |",
    ]
    for h in (*horizons, 0):
        group = [c.outcome for c in cells if c.months == h and c.outcome.walkaway_xirr is not None]
        if not group:
            continue
        values = [o.walkaway_xirr for o in group if o.walkaway_xirr is not None]
        multiples = [(o.walkaway_multiple, o.start) for o in group]
        scored = [(m, s) for m, s in multiples if m is not None]
        worst = min(scored)
        best = max(scored)
        losses = sum(1 for m, _ in scored if m < _ONE)
        tri = [o.tri_walkaway for o in group if o.tri_walkaway is not None]
        excess = [
            o.walkaway_xirr - o.tri_walkaway
            for o in group
            if o.walkaway_xirr is not None and o.tri_walkaway is not None
        ]
        lines.append(
            f"| {_horizon_label(h)} | {len(group)} | {min(values):.1%} | "
            f"{_quantile(values, Decimal('0.1')):.1%} | {_median(values):.1%} | "
            f"{_quantile(values, Decimal('0.9')):.1%} | {max(values):.1%} | "
            f"{worst[0] - _ONE:+.1%} ({worst[1]}) | {best[0] - _ONE:+.1%} ({best[1]}) | "
            f"{Decimal(losses) / Decimal(len(scored)):.0%} | "
            f"{max(o.max_drawdown for o in group):.1%} | "
            f"{max(o.under_water_days for o in group)} d | "
            f"{_pct(_median(tri) if tri else None)} | {_pp(_median(excess) if excess else None)} | "
            f"{_median([o.parked for o in group]):.0%} |"
        )
    return lines


def named_lines(
    market: Market, index: RunIndex, arm: Arm
) -> tuple[list[str], dict[str, Outcome], list[str]]:
    """The named-interval table, both floors; the ₹10-crore outcomes by key; what is missing."""
    lines = [
        "| Key | Interval | Regime | Start → end | NIFTY 500 P/E at start | NIFTY 500 below its "
        "high at start | D13 regime at start | Floor | Walk-away after-tax XIRR | Realised "
        "after-tax XIRR (decision rule) | Pre-tax XIRR | Max DD | Longest under water | "
        "NIFTY 500 TRI walk-away | Excess vs TRI | Cash after tax | Excess vs cash | Trades | "
        "Charges | Parked share |",
        "| " + " | ".join(["---"] * 20) + " |",
    ]
    outcomes: dict[str, Outcome] = {}
    missing: list[str] = []
    for n in NAMED_INTERVALS:
        first = next(s for s in market.sessions if s >= n.start)
        pe = market.pe_on(first)
        for floor in FLOORS:
            digest = index.digest(RunKey(n.start, n.end, repr(arm.v2), universe_key(floor)))
            if digest is None or load_run(index.out_dir, digest) is None:
                missing.append(f"{n.key}@{floor}")
                continue
            run = load_run_data(index.out_dir, digest)
            o = outcome(run, run.nav[-1][0], market)
            if floor == GRID_FLOOR:
                outcomes[n.key] = o
            excess_tri = (
                None
                if o.walkaway_xirr is None or o.tri_walkaway is None
                else o.walkaway_xirr - o.tri_walkaway
            )
            excess_cash = (
                None
                if o.walkaway_xirr is None or o.cash_after_tax is None
                else o.walkaway_xirr - o.cash_after_tax
            )
            lines.append(
                f"| {n.key} | {n.label} | {n.regime.value} | {o.start} → {o.end} | "
                f"{'n/a (series opens 2012-10-01)' if pe is None else f'{pe:.1f}'} | "
                f"{market.index_drawdown(first):.1%} | "
                f"{'on' if market.risk_on(first) else 'OFF (parked)'} | "
                f"₹{floor / _CRORE:.0f} cr{'' if floor == GRID_FLOOR else ' (info)'} | "
                f"**{_pct(o.walkaway_xirr)}** | {_pct(o.realised_xirr)} | {o.pre_tax_xirr:.1%} | "
                f"{o.max_drawdown:.1%} | {o.under_water_days} d | {_pct(o.tri_walkaway)} | "
                f"{_pp(excess_tri)} | {_pct(o.cash_after_tax)} | {_pp(excess_cash)} | "
                f"{o.trades} | ₹{run.summary.total_charges:,.0f} | {o.parked:.0%} |"
            )
    return lines, outcomes, missing


def validation_lines(market: Market, index: RunIndex, arm: Arm, cells: Sequence[Cell]) -> list[str]:
    """Every exact run that opens on a month-start grid session, against the grid run's prefix.

    The claim checked: a fresh start from ``S`` run to ``T`` holds exactly the grid run's NAV path
    and fills up to ``T`` — so the grid's truncated cells are exact replays, not approximations.
    """
    starts = set(grid_starts(market.sessions, MONTH_START))
    windows = [(n.key, n.start, n.end) for n in NAMED_INTERVALS]
    windows += [(u.name, u.start, u.end) for u in baseline_units()]
    lines = [
        "| Window | Start → end | NAV points compared | NAV points differing | Fills through end "
        "(exact run / grid prefix) | Walk-away XIRR, exact run | from the grid run truncated | "
        "Gap |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    seen: set[tuple[date, date]] = set()
    for name, start, end in windows:
        first = next(s for s in market.sessions if s >= start)
        if first not in starts or (start, end) in seen:
            continue
        seen.add((start, end))
        own = index.digest(RunKey(start, end, repr(arm.v2), universe_key(GRID_FLOOR)))
        grid = index.digest(RunKey(first, SPAN_END, repr(arm.v2), universe_key(GRID_FLOOR)))
        if own is None or grid is None or own == grid:
            continue
        a = load_run_data(index.out_dir, own)
        b = load_run_data(index.out_dir, grid)
        terminal = a.nav[-1][0]
        bmap = dict(b.nav)
        differing = sum(1 for d, v in a.nav if bmap.get(d) != v)
        fills_a = len(a.ledger.trades)
        fills_b = len([t for t in b.ledger.trades if t.trade_date <= terminal])
        oa = outcome(a, terminal, market)
        ob = outcome(b, terminal, market)
        gap = (
            None
            if oa.walkaway_xirr is None or ob.walkaway_xirr is None
            else oa.walkaway_xirr - ob.walkaway_xirr
        )
        lines.append(
            f"| {name} | {first} → {terminal} | {len(a.nav)} | {differing} | {fills_a} / "
            f"{fills_b} | {_pct(oa.walkaway_xirr)} | {_pct(ob.walkaway_xirr)} | "
            f"{'n/a' if gap is None else f'{gap * 10000:.2f} bp'} |"
        )
    return lines


def approximation_lines(cells: Sequence[Cell], horizons: Sequence[int]) -> list[str]:
    """Exact fresh-start pre-tax XIRR against the continuous run's slice, per horizon."""
    lines = [
        "| Horizon | Cells | Median gap (exact - slice) | Mean absolute gap | Largest absolute gap "
        "(start) | Share where the slice is higher |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for h in (*horizons, 0):
        pairs = [
            (c.outcome.pre_tax_xirr - c.approx_pre_tax, c.outcome.start)
            for c in cells
            if c.months == h and c.approx_pre_tax is not None
        ]
        if not pairs:
            continue
        gaps = [g for g, _ in pairs]
        worst = max(pairs, key=lambda p: abs(p[0]))
        mean_abs = sum((abs(g) for g in gaps), _ZERO) / Decimal(len(gaps))
        higher = sum(1 for g in gaps if g < _ZERO)
        lines.append(
            f"| {_horizon_label(h)} | {len(gaps)} | {_pp(_median(gaps))} | {_pp(mean_abs)} | "
            f"{_pp(worst[0])} ({worst[1]}) | {Decimal(higher) / Decimal(len(gaps)):.0%} |"
        )
    return lines


def month_effect_lines(cells: Sequence[Cell], expected: Decimal) -> list[str]:
    """Does the calendar month of the start matter? Per start month, and a permutation test."""
    lines: list[str] = []
    names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    for h in MONTH_EFFECT_HORIZONS:
        group = [c.outcome for c in cells if c.months == h and c.outcome.walkaway_xirr is not None]
        by_month: dict[int, list[Outcome]] = {}
        for o in group:
            by_month.setdefault(o.start.month, []).append(o)
        lines += [
            f"**{h}-month horizon** (month-start grid, walk-away after-tax XIRR)",
            "",
            "| Start month | Windows | Median | Worst | P(≥ E) | P(≥ NIFTY 500 TRI) |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for m in sorted(by_month):
            os_ = by_month[m]
            vals = [o.walkaway_xirr for o in os_ if o.walkaway_xirr is not None]
            meet_e = sum(1 for v in vals if v >= expected)
            meet_t = sum(
                1
                for o in os_
                if o.walkaway_xirr is not None
                and o.tri_walkaway is not None
                and o.walkaway_xirr >= o.tri_walkaway
            )
            lines.append(
                f"| {names[m - 1]} | {len(os_)} | {_median(vals):.1%} | {min(vals):.1%} | "
                f"{Decimal(meet_e) / Decimal(len(vals)):.0%} | "
                f"{Decimal(meet_t) / Decimal(len(os_)):.0%} |"
            )
        spread, p = permutation_spread_pvalue(
            {
                m: [float(o.walkaway_xirr) for o in v if o.walkaway_xirr is not None]
                for m, v in by_month.items()
            }
        )
        lines += [
            "",
            f"Spread of the twelve monthly medians: {spread:.1%}; permutation p-value (2,000 "
            f"relabellings, seed fixed): **{p:.2f}** — the chance that start months with no "
            "effect at all would produce a spread this wide.",
            "",
        ]
    return lines


def grid_comparison_lines(
    start_cells: Sequence[Cell], mid_cells: Sequence[Cell], expected: Decimal
) -> list[str]:
    """Month-start against mid-month starts, per horizon: same distribution or not?"""
    lines = [
        "| Horizon | Month-start: windows / median / P(≥ E) / P(≥ TRI) | Mid-month: windows / "
        "median / P(≥ E) / P(≥ TRI) | Median paired difference (mid - start, same month) |",
        "| --- | --- | --- | --- |",
    ]
    tri = HURDLES[2]
    for h in HORIZONS:
        a = [c for c in start_cells if c.months == h and c.outcome.walkaway_xirr is not None]
        b = [c for c in mid_cells if c.months == h and c.outcome.walkaway_xirr is not None]
        if not a or not b:
            continue

        def summary(cells: Sequence[Cell]) -> str:
            vals = [c.outcome.walkaway_xirr for c in cells if c.outcome.walkaway_xirr is not None]
            me, ns = share_meeting([(v, expected) for v in vals])
            mt, nt = share_meeting([tri.pair(c, expected) for c in cells])
            return (
                f"{len(vals)} / {_median(vals):.1%} / {Decimal(me) / Decimal(ns):.0%} / "
                f"{Decimal(mt) / Decimal(max(nt, 1)):.0%}"
            )

        by_month_a = {(c.outcome.start.year, c.outcome.start.month): c for c in a}
        diffs: list[Decimal] = []
        for c in b:
            pair = by_month_a.get((c.outcome.start.year, c.outcome.start.month))
            mine, theirs = (
                c.outcome.walkaway_xirr,
                None if pair is None else pair.outcome.walkaway_xirr,
            )
            if mine is not None and theirs is not None:
                diffs.append(mine - theirs)
        lines.append(
            f"| {h} m | {summary(a)} | {summary(b)} | {_pp(_median(diffs) if diffs else None)} |"
        )
    return lines


def in_out_lines(cells: Sequence[Cell], market: Market) -> list[str]:
    """Repeated entry and exit against staying invested, from every month start, both phases."""
    by = {(c.outcome.start, c.months): c.outcome for c in cells}
    anchors = anchor_sessions(market.sessions, MONTH_START)
    starts = grid_starts(market.sessions, MONTH_START)
    lines = [
        "| Pattern | Phase | Total horizon | Windows | In/out beats staying | Median XIRR, in/out "
        "| Median XIRR, staying | Median difference | Worst in/out | Worst staying |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for months_in, months_out in IN_OUT_PATTERNS:
        for phase in ("in first", "out first"):
            for total in IN_OUT_TOTALS:
                rows: list[tuple[Decimal, Decimal]] = []
                for start in starts:
                    stay = by.get((start, total))
                    if stay is None or stay.walkaway_xirr is None:
                        continue
                    legs = _in_out_legs(
                        start, total, months_in, months_out, phase, by, anchors, market
                    )
                    if legs is None:
                        continue
                    end = stay.end
                    factor = chain_growth(legs)
                    days = (end - start).days
                    rate = (factor.ln() * Decimal(365) / Decimal(days)).exp() - _ONE
                    rows.append((rate, stay.walkaway_xirr))
                if not rows:
                    continue
                beats = sum(1 for a, b in rows if a >= b)
                lines.append(
                    f"| {months_in} m in / {months_out} m out | {phase} | {total} m | {len(rows)} "
                    f"| {Decimal(beats) / Decimal(len(rows)):.0%} | "
                    f"{_median([a for a, _ in rows]):.1%} | {_median([b for _, b in rows]):.1%} | "
                    f"{_pp(_median([a - b for a, b in rows]))} | {min(a for a, _ in rows):.1%} | "
                    f"{min(b for _, b in rows):.1%} |"
                )
    return lines


def _in_out_legs(
    start: date,
    total: int,
    months_in: int,
    months_out: int,
    phase: str,
    by: Mapping[tuple[date, int], Outcome],
    anchors: Mapping[tuple[int, int], date],
    market: Market,
) -> list[tuple[Decimal, date, date]] | None:
    """The legs of one in/out path: invested legs are fresh-start cells, cash legs earn after-tax
    cash. ``None`` when any leg is not scorable."""
    legs: list[tuple[Decimal, date, date]] = []
    offset = 0
    invested = phase == "in first"
    while offset < total:
        length = min(months_in if invested else months_out, total - offset)
        key = month_add(start.year, start.month, offset)
        leg_start = anchors.get(key)
        if leg_start is None:
            return None
        if invested:
            cell = by.get((leg_start, length))
            if cell is None or cell.walkaway_xirr is None:
                return None
            legs.append((cell.walkaway_xirr, cell.start, cell.end))
        else:
            end = horizon_end(market.sessions, anchors, leg_start, length, SPAN_END)
            if end is None:
                return None
            cash = market.benchmarks(leg_start, end)[3]
            if cash is None:
                return None
            legs.append((cash, leg_start, end))
        offset += length
        invested = not invested
    return legs


def sip_lines(cells: Sequence[Cell], index: RunIndex, arm: Arm, market: Market) -> list[str]:
    """Monthly SIP against a lump sum over the same window — D13 (as units of the fresh-start
    run's NAV) and the NIFTY 500 TRI (as units of the index), pre-tax."""
    anchors = anchor_sessions(market.sessions, MONTH_START)
    levels = {p.session: p.level for p in market.index.points}
    lines = [
        "| Horizon | Windows | D13 lump sum: median / worst / P(loss) | D13 monthly SIP: median "
        "/ worst / P(loss) | SIP beats lump sum (D13) | TRI lump sum: median / worst | TRI SIP: "
        "median / worst |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    navs: dict[date, dict[date, Decimal]] = {}
    for h in SIP_HORIZONS:
        rows: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
        for c in cells:
            if c.months != h or c.grid != "month-start":
                continue
            start, end = c.outcome.start, c.outcome.end
            if start not in navs:
                digest = index.digest(
                    RunKey(start, SPAN_END, repr(arm.v2), universe_key(GRID_FLOOR))
                )
                if digest is None:
                    continue
                navs[start] = dict(load_run_data(index.out_dir, digest).nav)
            nav = navs[start]
            dates = [anchors[month_add(start.year, start.month, k)] for k in range(h)]
            amount = Decimal("100000")
            instal = [(d, amount) for d in dates]
            sip = sip_xirr(instal, nav, (end, nav[end]))
            tri_sip = sip_xirr(instal, levels, (end, levels[end]))
            lump = c.outcome.pre_tax_xirr
            tri_lump = xirr([Cashflow(start, -levels[start]), Cashflow(end, levels[end])])
            if sip is None or tri_sip is None:
                continue
            rows.append((lump, sip, tri_lump, tri_sip))
        if not rows:
            continue

        def stats(values: Sequence[Decimal], with_loss: bool = True) -> str:
            base = f"{_median(values):.1%} / {min(values):.1%}"
            if not with_loss:
                return base
            return base + f" / {Decimal(sum(1 for v in values if v < 0)) / len(values):.0%}"

        beats = sum(1 for r in rows if r[1] > r[0])
        lines.append(
            f"| {h} m | {len(rows)} | {stats([r[0] for r in rows])} | "
            f"{stats([r[1] for r in rows])} | {Decimal(beats) / Decimal(len(rows)):.0%} | "
            f"{stats([r[2] for r in rows], False)} | {stats([r[3] for r in rows], False)} |"
        )
    return lines


def span_lines(market: Market) -> list[str]:
    """The data-span facts the report's caveat rests on, read from the lake."""
    from dataplatform.ingest.index_history import read_membership_history

    history = read_membership_history("nifty500", data_root=market.data_root)
    coverage = None if history is None else history.coverage_start
    months = _span_months()
    return [
        f"- L1 calendar on disk: {market.reader.all_sessions()[0]} → "
        f"{market.reader.all_sessions()[-1]}; joinable (ISIN-bearing) history from 2011-06-22.",
        f"- Study span: **{SPAN_START} → {SPAN_END}** ({len(market.window(SPAN_START, SPAN_END))} "
        f"sessions); month-start grid {GRID_FIRST[0]}-{GRID_FIRST[1]:02d} onwards, "
        f"{months} months.",
        f"- NIFTY 500 point-in-time membership opens {coverage}: the `nifty500` universe the paper "
        "book screens cannot run before it, so the study runs the `turnover_floor` universe "
        "(liquidity floor only) — close to, not identical with, the paper book.",
        f"- NIFTY 500 P/E (`{PE_SERIES}`) opens {min(market.pe) if market.pe else 'n/a'}.",
    ]


def write_charts(out: Path, cells: Sequence[Cell], expected: Decimal, title: str) -> list[Path]:
    """The horizon distribution and the probability curves as SVG; returns the paths."""
    series: dict[int, list[float]] = {}
    tri_median: dict[int, float] = {}
    for h in HORIZONS:
        vals = [
            float(c.outcome.walkaway_xirr)
            for c in cells
            if c.months == h and c.outcome.walkaway_xirr is not None
        ]
        tris = [
            c.outcome.tri_walkaway
            for c in cells
            if c.months == h and c.outcome.tri_walkaway is not None
        ]
        if vals:
            series[h] = vals
        if tris:
            tri_median[h] = float(_median(tris))
    box = out / "holding-period-distribution.svg"
    box.write_text(
        horizon_box_svg(series, tri_median, float(expected), title=title), encoding="utf-8"
    )
    curves: dict[str, list[tuple[int, float]]] = {}
    for hurdle in HURDLES:
        shares = shares_by_horizon(cells, hurdle, expected, HORIZONS)
        curves[hurdle.name] = [(h, met / n) for h, (met, n) in shares.items() if n > 0]
    prob = out / "holding-period-probability.svg"
    prob.write_text(probability_svg(curves, title=title), encoding="utf-8")
    return [box, prob]


def render(out_dir: Path, *, data_root: Path | None, arm: Arm, reference: Path | None) -> Path:
    """Write the study's tables, cells and charts for ``arm``; returns the tables path."""
    index = RunIndex(out_dir)
    market = Market.open(data_root)
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    try:
        named, named_outcomes, named_missing = named_lines(market, index, arm)
        full = named_outcomes.get(FULL_SPAN_KEY)
        if full is None or full.walkaway_xirr is None:
            raise StudyError("the full-span run (N01) is missing or untaxable: no expected return")
        expected = full.walkaway_xirr
        expected_pre = full.pre_tax_xirr
        full_digest = index.digest(
            RunKey(SPAN_START, SPAN_END, repr(arm.v2), universe_key(GRID_FLOOR))
        )
        assert full_digest is not None
        approx_nav = dict(load_run_data(out_dir, full_digest).nav)
        start_cells, start_missing = grid_cells(
            market, index, arm, MONTH_START, DENSE_HORIZONS, approx_nav
        )
        mid_cells, mid_missing = grid_cells(market, index, arm, MID_MONTH, HORIZONS, None)
        day8_cells, _ = grid_cells(market, index, arm, DAY_EIGHT, HORIZONS, None)
        grids = {"month-start": start_cells}
        if mid_cells:
            grids["mid-month"] = mid_cells
        if day8_cells:
            grids["day-8"] = day8_cells
        headline, stable = min_horizon_table(grids, expected, expected_pre)
        withheld = sum(1 for c in [*start_cells, *mid_cells] if c.outcome.walkaway_xirr is None)
        sections: list[tuple[str, list[str]]] = [
            ("Arm", [f"{arm.label} — `{arm.v2!r}`"]),
            ("Data span", span_lines(market)),
            (
                "Run inventory",
                [
                    f"- Book-action states across the runs: {len(index.book_actions)} "
                    + (
                        "(one — every run on the same store)"
                        if len(index.book_actions) == 1
                        else "(MORE THAN ONE — see below)"
                    ),
                    *[f"  - `{k[:90]}…`: {v} runs" for k, v in index.book_actions.items()],
                    f"- Named-interval runs missing: {named_missing or 'none'}",
                    f"- Month-start grid starts missing: {start_missing or 'none'}",
                    f"- Mid-month grid starts missing: {mid_missing or 'none'}",
                    f"- Cells scored: month-start {len(start_cells)}, mid-month {len(mid_cells)}; "
                    f"cells whose walk-away figure the tax layer withheld: {withheld}",
                ],
            ),
            (
                "Expected return (E)",
                [
                    f"- Full span {full.start} → {full.end} at ₹10 crore/day: walk-away after-tax "
                    f"XIRR **{expected:.2%}**, realised after-tax {_pct(full.realised_xirr)}, "
                    f"pre-tax {expected_pre:.2%}; NIFTY 500 TRI walk-away "
                    f"{_pct(full.tri_walkaway)}; cash after tax {_pct(full.cash_after_tax)}.",
                ],
            ),
            ("Minimum holding period (month-start grid, walk-away after-tax)", headline),
            (
                "Probability by horizon (month-start grid, dense horizons)",
                probability_lines(start_cells, expected, DENSE_HORIZONS),
            ),
            (
                "Distribution by horizon (month-start grid)",
                distribution_lines(start_cells, DENSE_HORIZONS),
            ),
            (
                "Distribution by horizon (mid-month grid)",
                distribution_lines(mid_cells, HORIZONS) if mid_cells else ["not run"],
            ),
            ("Named intervals", named),
            (
                "Validation: exact runs against the grid run's prefix",
                validation_lines(market, index, arm, start_cells),
            ),
            (
                "Exact fresh starts against the continuous-run slice (pre-tax)",
                approximation_lines(start_cells, DENSE_HORIZONS),
            ),
            ("Start month", month_effect_lines(start_cells, expected)),
            (
                "Month-start against mid-month starts",
                grid_comparison_lines(start_cells, mid_cells, expected)
                if mid_cells
                else ["not run"],
            ),
            ("In and out against staying invested", in_out_lines(start_cells, market)),
            ("Monthly SIP against a lump sum", sip_lines(start_cells, index, arm, market)),
        ]
        if reference is not None and (out_dir / FROZEN_NAME).is_file():
            frozen = json.loads((out_dir / FROZEN_NAME).read_text(encoding="utf-8"))
            frozen["_out_dir"] = str(out_dir)
            try:
                sections.insert(1, ("Frozen D13 check", compare_reference(frozen, reference)))
            except StudyError as failure:
                sections.insert(1, ("Frozen D13 check", [f"**FAILED**: {failure}"]))
        body = [f"# Holding-period study tables — {arm.label}", ""]
        for title, lines in sections:
            body += [f"## {title}", "", *lines, ""]
        body += [
            "## Stability verdicts",
            "",
            *[f"- {k}: {'stable' if v else 'NOT stable'}" for k, v in stable.items()],
            "",
        ]
        slug = _slug(arm.label)
        path = reports / f"tables-{slug}.md"
        path.write_text("\n".join(body), encoding="utf-8")
        cells_doc = {
            "arm": arm.label,
            "expected": str(expected),
            "named": {k: o.to_document() for k, o in named_outcomes.items()},
            "cells": [
                {
                    "grid": c.grid,
                    "months": c.months,
                    **c.outcome.to_document(),
                    "approx_pre_tax": None if c.approx_pre_tax is None else str(c.approx_pre_tax),
                }
                for c in [*start_cells, *mid_cells]
            ],
        }
        (reports / f"cells-{slug}.json").write_text(
            json.dumps(cells_doc, indent=1) + "\n", encoding="utf-8"
        )
        charts = reports / slug
        charts.mkdir(exist_ok=True)
        write_charts(charts, start_cells, expected, arm.label)
        return path
    finally:
        market.close()


def _slug(label: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in label).strip("-")[:48]


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def resolve_arm(label: str) -> Arm:
    """An arm by label from any ``backtest.sweep`` arm list (D13 by default)."""
    import backtest.sweep as sweep

    if label == D13_PAPER_BASELINE.label:
        return D13_PAPER_BASELINE
    for name in dir(sweep):
        value = getattr(sweep, name)
        candidates: Iterable[object] = (
            value if isinstance(value, tuple) else (value,) if isinstance(value, Arm) else ()
        )
        for arm in candidates:
            if isinstance(arm, Arm) and arm.label == label:
                return arm
    raise StudyError(f"no arm labelled {label!r} in backtest.sweep")


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.holding_period")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "check-baseline", "render"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--data-root", type=Path, default=None)
        p.add_argument("--arm", default=D13_PAPER_BASELINE.label)
        if name == "run":
            p.add_argument(
                "--phase",
                required=True,
                choices=("baseline", "named", "grid-start", "grid-mid", "grid-day8"),
            )
            p.add_argument("--workers", type=int, default=2)
            p.add_argument("--signal-panel", type=Path, default=None)
        if name in ("check-baseline", "render"):
            p.add_argument("--reference-frozen", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns a process exit code."""
    args = _parse(argv)
    out: Path = args.out
    try:
        refuse_lake_location(out, args.data_root)
        out.mkdir(parents=True, exist_ok=True)
        arm = resolve_arm(args.arm)
        if args.command == "run":
            reader = _L1Reader(data_root=args.data_root)
            try:
                sessions = reader.all_sessions()
            finally:
                reader.close()
            if sessions[-1] <= SPAN_END:
                raise StudyError(
                    f"the lake ends {sessions[-1]}; the span end {SPAN_END} needs a session after "
                    "it for T+1 fills"
                )
            if args.phase == "baseline" and arm is not D13_PAPER_BASELINE:
                raise StudyError("the baseline phase is D13's alone")
            units: tuple[Unit, ...]
            if args.phase == "baseline":
                units = baseline_units()
            elif args.phase == "named":
                units = named_units()
            else:
                units = grid_units(
                    sessions,
                    {"grid-start": MONTH_START, "grid-mid": MID_MONTH}.get(args.phase, DAY_EIGHT),
                )
            actions = load_store_book_actions(data_root=args.data_root)
            plan = Plan(out, arm, units, args.data_root, args.signal_panel)
            failed = run_plan(plan, workers=args.workers, actions=actions)
            print(f"{args.phase}: {len(units)} units, {failed} failed runs")
            return 1 if failed else 0
        if args.command == "check-baseline":
            reader = _L1Reader(data_root=args.data_root)
            try:
                lake_last = reader.all_sessions()[-1]
            finally:
                reader.close()
            frozen = freeze_baseline(out, arm, commit=_git_commit(), lake_last=lake_last)
            print(f"frozen: {out / FROZEN_NAME} ({len(frozen['runs'])} runs)")
            if args.reference_frozen is not None:
                frozen["_out_dir"] = str(out)
                for line in compare_reference(frozen, args.reference_frozen):
                    print(f"  {line}")
            return 0
        path = render(out, data_root=args.data_root, arm=arm, reference=args.reference_frozen)
        print(f"wrote {path}")
        return 0
    except StudyError as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
