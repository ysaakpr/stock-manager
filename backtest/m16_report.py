"""M16 — render the strategy-exploration report from the saved runs; replays nothing.

Reads the run directories ``backtest.m12_rerun --arm-set m16`` and ``--arm-set m16-fundamentals``
wrote (``manifest.json`` and each run's summary, fill ledger and NAV path) and renders
``ops/gates/M16-strategy-exploration-report.md`` under the rule
``ops/studies/preregistration-m16-2026-10-08.md`` fixed before any M16 run:

* one table per (universe, window, floor), D13 in every one, with each arm's A8 rail refusals;
* **A6**, the static 50/50 blend of D13 and M10.7, built here from the two baselines' saved NAV
  paths and cashflows (:func:`blend`) — it has no engine arm;
* the generated scorecard against D13 (:func:`scorecard`);
* the decision: the walk-forward choice on the selection window at the **₹10 crore** floor
  (:data:`PRIMARY_FLOOR`; the ₹1 crore choice is printed as informational), then Step 2's five
  criteria for that choice and for A4/A5 (:func:`criteria`), the fifth being the deflated Sharpe
  ratio with N = :data:`TRIALS` and V over the trials with a saved run on the cell
  (:func:`trial_sharpes`).

The selection floor and every comparison's direction are module constants and plain code, so the
tests can pin them: a report that chose on ₹1 crore, or let a worse arm pass, fails them.

Everything below the hand-written marker in an existing report is kept on re-render.

What it never does: replay a run, read a wall clock into the report, average across windows, or
re-rank the selection window after reading the verification window.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from backtest.m12_rerun import REGIME_DAILY_SET, RERUN_ARMS, WINDOWS
from backtest.m16_arms import (
    A1_ABSOLUTE_MOMENTUM,
    A2_INDUSTRY_GATE,
    A3_RESIDUAL_MOMENTUM_V2,
    A4_PROFITABILITY,
    A5_EARNINGS_SURPRISE,
    A6_BLEND,
    A7_LOW_VOL,
    M10_7_BASELINE,
    PREREGISTRATION,
    V2_ALL_ON_BASELINE,
    resolvable_m16_arms,
)
from backtest.nav import daily_returns
from backtest.policies.momentum_v2 import MomentumV2Parameters
from backtest.policies.naive_momentum import MomentumParameters
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import UniverseParameters
from backtest.sharpe import deflated_sharpe_ratio, sharpe_stats, sharpe_variance
from backtest.sweep import (
    CAP_TIER_ARMS,
    D13_PAPER_BASELINE,
    HIGH_FLOOR,
    LOW_FLOOR,
    REDEPLOY_ARMS,
    Arm,
    _arm_spec,
)
from backtest.xirr import Cashflow, xirr

__all__ = [
    "DILUTION_THRESHOLD",
    "M16_TRIAL_LABELS",
    "PRIMARY_FLOOR",
    "TRIALS",
    "TRIALS_ON_RECORD",
    "A2Coverage",
    "Criterion",
    "RunFacts",
    "TrialSharpe",
    "blend",
    "collect",
    "config_key",
    "criteria",
    "decision",
    "decisive",
    "load_a2_coverage",
    "main",
    "render",
    "scorecard",
    "select",
    "trial_sharpes",
    "with_blends",
]

REPORT = Path("ops/gates/M16-strategy-exploration-report.md")
MARKER = "<!-- hand-written analysis below: kept verbatim on re-render -->"

D13 = D13_PAPER_BASELINE.label
FLOOR_ONLY = "turnover_floor"
NIFTY500 = "nifty500"
SELECTION = "wf-selection"
VERIFICATION = "wf-verification"

#: Step 1 chooses on this floor (pre-registration §4) — **pending owner confirmation before the
#: campaign launches**. ₹1 crore is printed beside it and decides nothing.
PRIMARY_FLOOR: Final = HIGH_FLOOR
SECONDARY_FLOOR: Final = LOW_FLOOR
#: Step 2 criterion 3: verification XIRR above this at ₹10 crore, floor-only.
BAR: Final = Decimal("0.25")
#: Step 2 criterion 4: max DD no more than this worse than D13's in any covered cell.
DD_TOLERANCE: Final = Decimal("0.03")
#: Step 2 criterion 5: deflated-Sharpe probability at least this.
DSR_THRESHOLD: Final = 0.95
#: Amendment 1 (a): A2 is *diluted* — informational, deciding nothing — in any cell where more than
#: this share of the floor universe is missing from the (2026-snapshot) industry classification.
DILUTION_THRESHOLD: Final = Decimal("0.30")

#: The arms Step 1 ranks (every arm with a selection-window row); A4/A5 have none (§2, §4).
SELECTION_LABELS: Final = (
    D13,
    V2_ALL_ON_BASELINE.label,
    M10_7_BASELINE.label,
    A1_ABSOLUTE_MOMENTUM,
    A2_INDUSTRY_GATE,
    A3_RESIDUAL_MOMENTUM_V2,
    A6_BLEND,
    A7_LOW_VOL,
)
#: Step 3: verification-only evidence; at most "shadow in paper".
SHADOW_ONLY: Final = (A4_PROFITABILITY, A5_EARNINGS_SURPRISE)

#: Pre-registration Appendix A, in its order: every distinct configuration run on this lake before
#: M16. Rows 1-47 carry the sweep's own labels (they are matched to saved runs by it).
TRIALS_ON_RECORD: Final[tuple[str, ...]] = (
    "Momentum v2, all on (M9.5)",
    "Momentum v2, D13 paper config",
    "D13 + daily regime re-entry",
    "D13 + daily regime re-entry and exit",
    "D13 + daily regime re-entry, 2% band",
    "Naive momentum (M4.10)",
    "Naive momentum (M4.10) + redeploy",
    "Swing composite (M10.7)",
    "Reversal: 5-day losers",
    "Trend: 1-month",
    "Delivery acceleration",
    "Turnover expansion",
    "Mean proximity (50d)",
    "Short composite",
    "Short composite + reversal",
    "Breakout",
    "M10.7 + delivery acceleration",
    "M10.7 + 1-month trend",
    "Short composite, 2-week holds",
    "Short composite, 1-month holds",
    "Short composite, 3-month holds",
    "M10.7 + regime gate",
    "Short composite + regime gate",
    "Short composite + low-vol leg",
    "Short composite + regime + low-vol",
    "Short composite, top-10",
    "Short composite, top-10 + regime",
    "Short composite, top-5",
    "Swing composite + residual momentum (H1)",
    "Swing composite + band-hit avoidance (H2)",
    "Swing composite + residual momentum + band-hit avoidance (H3)",
    "M10.7 @ fortnightly / 21-session hold",
    "M10.7 @ fortnightly / 42-session hold",
    "M10.7 @ weekly / 21-session hold",
    "M10.7 @ weekly / 10-session hold",
    "M10.7 @ weekly / 10-session hold, 2-session floor",
    "M10.7 @ monthly / 63-session hold",
    "M10.7 @ monthly / 126-session hold",
    "M10.7 @ quarterly / 126-session hold",
    "M10.7, band 1.5x top_n",
    "M10.7, band 5x top_n",
    "Multi cap: 8 large + 8 mid + 8 small (liquidity tiers)",
    "Focused midcap: top 20 mid (liquidity tier)",
    "Focused smallcap: top 20 small (liquidity tier)",
    "Swing composite (M10.7) + redeploy",
    "M10.7 + regime gate + redeploy",
    "Multi cap + redeploy",
    "v2: + 12-1 momentum (alone)",
    "v2: + turnover banding (alone)",
    "v2: + regime filter (alone)",
    "v2: + vol-scaled weights (alone)",
    "v2: + redeploy proceeds next session (alone)",
    "v2: all four M9.5 toggles, no redeploy",
    "v2: + vol target 15% (alone)",
    "M10.7, cadence weekly (63-session re-underwrite)",
    "M10.7, no trailing stop",
    "M10.7, 12% trailing stop",
    "M10.7, delivery leg only",
    "M10.7, no delivery leg",
    "M10.7, 12-1 momentum leg only",
    "M10.7, no volatility screen",
    "Sector rotation",
    "Plain momentum on the sector-rotation universe",
    "Fundamentals: VALUE",
    "Fundamentals: GROWTH",
    "Fundamentals: QUALITY_VALUE",
    "Fundamentals: MOMENTUM_VALUE",
    "Forecast, daily",
)
M16_TRIAL_LABELS: Final = (
    A1_ABSOLUTE_MOMENTUM,
    A2_INDUSTRY_GATE,
    A3_RESIDUAL_MOMENTUM_V2,
    A4_PROFITABILITY,
    A5_EARNINGS_SURPRISE,
    A6_BLEND,
    A7_LOW_VOL,
)
#: N in the deflated Sharpe ratio (pre-registration §6): 68 + 7.
TRIALS: Final = len(TRIALS_ON_RECORD) + len(M16_TRIAL_LABELS)

_ZERO = Decimal("0")
_CRORE = Decimal("10000000")
_FLOOR_RE = re.compile(r"median_turnover_floor=Decimal\('(\d+)'\)")
_INDEX_RE = re.compile(r"index_slug='([a-z0-9]+)'")
_FIELD_RE = re.compile(r"(\w+)=(Decimal\('[^']*'\)|<[^>]*>|[^,]+)")
_WINDOW_ORDER = ("decade", "six-year", SELECTION, VERIFICATION)
_DEFAULTS: dict[str, object] = {
    "MomentumV2Parameters": MomentumV2Parameters(),
    "SwingCompositeParameters": SwingCompositeParameters(),
    "MomentumParameters": MomentumParameters(),
}


# ── run identity ─────────────────────────────────────────────────────────────────────────────────


def _fields(parameters: str) -> dict[str, str]:
    body = parameters[parameters.index("(") + 1 : parameters.rindex(")")]
    return {m.group(1): m.group(2).strip() for m in _FIELD_RE.finditer(body)}


def config_key(spec: Mapping[str, str]) -> str:
    """A run's strategy configuration, independent of window, floor, universe and engine state.

    The runner, every parameter that differs from today's default, and the band-hit and cap-tier
    settings. Dropping default-valued fields is what keeps one configuration one trial when a
    later field (added at a neutral default) lengthens the parameters' repr.
    """
    parameters = spec["parameters"]
    name = parameters[: parameters.index("(")]
    default = _DEFAULTS.get(name)
    given = _fields(parameters)
    if default is not None:
        base = _fields(repr(default))
        given = {k: v for k, v in given.items() if base.get(k) != v}
    changed = ",".join(f"{k}={v}" for k, v in sorted(given.items()))
    extras = "|".join(spec.get(k, "") for k in ("band_hit_avoidance", "cap_tiers"))
    return f"{spec['runner']}:{name}({changed})|{extras}"


def _arm_key(arm: Arm) -> str:
    universe = UniverseParameters.for_universe(FLOOR_ONLY, median_turnover_floor=PRIMARY_FLOOR)
    spec = _arm_spec(
        arm,
        start=WINDOWS[VERIFICATION][0],
        end=WINDOWS[VERIFICATION][1],
        universe=universe,
        opening_cash=Decimal("1000000"),
        adjusted=True,
    )
    return config_key(spec)


def _known_labels(arms: Iterable[Arm]) -> dict[str, str]:
    out: dict[str, str] = {}
    for arm in arms:
        out.setdefault(_arm_key(arm), arm.label)
    return out


# ── loading ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunFacts:
    """One saved run (or one A6 blend of two), as the report needs it."""

    window: str
    universe: str
    floor: Decimal
    label: str
    key: str
    digest: str
    xirr: Decimal
    max_drawdown: Decimal
    excess: Decimal
    charges: Decimal
    trades: int
    final_nav: Decimal
    opening_cash: Decimal
    terminal: date
    #: Sells A8's minimum-holdings rail refused over the run, on any session (summary rail blocks).
    floor_refusals: int
    #: Every rail block of the run, by rail.
    rail_blocks: Mapping[str, int] = field(default_factory=dict)
    nav: tuple[tuple[date, Decimal], ...] = ()

    @property
    def ratio(self) -> Decimal:
        return self.xirr / self.max_drawdown if self.max_drawdown > _ZERO else _ZERO

    @property
    def cell(self) -> tuple[str, str, Decimal]:
        return (self.universe, self.window, self.floor)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _window_of(start: str, end: str) -> str | None:
    for name, (first, last) in WINDOWS.items():
        if first.isoformat() == start and last.isoformat() == end:
            return name
    return None


def collect(run_dir: Path, labels: Mapping[str, str] | None = None) -> list[RunFacts]:
    """Every run in ``run_dir`` on a mandate window whose configuration ``labels`` names.

    ``labels`` maps :func:`config_key` to a label; by default the M16 arms that resolve today.
    A run of another configuration, or on another window, is skipped.
    """
    if labels is None:
        labels = _known_labels(resolvable_m16_arms()[0])
    facts: list[RunFacts] = []
    for summary_file in sorted((run_dir / "runs").glob("*.json")):
        summary = _load(summary_file)
        spec = summary["spec"]
        window = _window_of(spec["start"], spec["end"])
        label = labels.get(config_key(spec))
        if window is None or label is None:
            continue
        floor_match = _FLOOR_RE.search(spec["universe"])
        if floor_match is None:
            raise ValueError(f"{summary_file.name}: no turnover floor in {spec['universe']}")
        index_match = _INDEX_RE.search(spec["universe"])
        digest = summary["digest"]
        ledger = _load(run_dir / "ledgers" / f"{digest}.json")
        nav = _load(run_dir / "navs" / f"{digest}.json")
        blocks = {str(k): int(v) for k, v in summary.get("rail_blocks", {}).items()}
        facts.append(
            RunFacts(
                window=window,
                universe=index_match.group(1) if index_match else FLOOR_ONLY,
                floor=Decimal(floor_match.group(1)),
                label=label,
                key=config_key(spec),
                digest=digest,
                xirr=Decimal(summary["xirr"]),
                max_drawdown=Decimal(summary["max_drawdown"]),
                excess=Decimal(summary["excess"]),
                charges=Decimal(summary["total_charges"]),
                trades=len(ledger["trades"]),
                final_nav=Decimal(summary["final_nav"]),
                opening_cash=Decimal(spec["opening_cash"]),
                terminal=date.fromisoformat(summary["terminal"]),
                floor_refusals=blocks.get("MIN_HOLDINGS", 0),
                rail_blocks=blocks,
                nav=tuple((date.fromisoformat(d), Decimal(v)) for d, v in nav["points"]),
            )
        )
    return facts


# ── A6: the report-side blend ────────────────────────────────────────────────────────────────────


def _max_drawdown(points: Sequence[tuple[date, Decimal]]) -> Decimal:
    peak = worst = _ZERO
    for _, value in points:
        peak = max(peak, value)
        if peak > _ZERO:
            worst = max(worst, (peak - value) / peak)
    return worst


def _carried(points: Sequence[tuple[date, Decimal]], on: Sequence[date]) -> list[Decimal]:
    """``points`` read on every date of ``on``, each the last value known on or before it."""
    out: list[Decimal] = []
    index, last = 0, points[0][1]
    for when in on:
        while index < len(points) and points[index][0] <= when:
            last = points[index][1]
            index += 1
        out.append(last)
    return out


def blend(a: RunFacts, b: RunFacts, *, label: str = A6_BLEND) -> RunFacts:
    """Half the capital in each of two runs, never rebalanced between the halves (A6, §2).

    Assumes both are full runs of the same cell and opening capital. The NAV is the mean of the two
    paths on the union of their dates from the first date both have, each carrying its last known
    value across a session it lacks; the XIRR is struck on the halved cashflows (half of each
    deposit, half of each terminal NAV); rupee figures are halved, counts summed. Raises
    ``ValueError`` for runs of different cells, capital or terminal dates.
    """
    if a.cell != b.cell or a.opening_cash != b.opening_cash or a.terminal != b.terminal:
        raise ValueError(f"cannot blend {a.label} {a.cell} with {b.label} {b.cell}")
    if not a.nav or not b.nav:
        raise ValueError("a blend needs both runs' NAV paths")
    first = max(a.nav[0][0], b.nav[0][0])
    dates = sorted({d for d, _ in (*a.nav, *b.nav) if d >= first})
    left, right = _carried(a.nav, dates), _carried(b.nav, dates)
    half = Decimal("2")
    path = tuple((d, (x + y) / half) for d, x, y in zip(dates, left, right, strict=True))
    flows = [
        Cashflow(a.nav[0][0], -a.opening_cash / half),
        Cashflow(b.nav[0][0], -b.opening_cash / half),
        Cashflow(a.terminal, (a.final_nav + b.final_nav) / half),
    ]
    rate = xirr(flows)
    benchmark = a.xirr - a.excess  # the window's benchmark XIRR, as a's summary recorded it
    blocks = {
        k: a.rail_blocks.get(k, 0) + b.rail_blocks.get(k, 0)
        for k in {*a.rail_blocks, *b.rail_blocks}
    }
    return RunFacts(
        window=a.window,
        universe=a.universe,
        floor=a.floor,
        label=label,
        key=label,
        digest=f"blend:{a.digest[:12]}+{b.digest[:12]}",
        xirr=rate,
        max_drawdown=_max_drawdown(path),
        excess=rate - benchmark,
        charges=(a.charges + b.charges) / half,
        trades=a.trades + b.trades,
        final_nav=(a.final_nav + b.final_nav) / half,
        opening_cash=a.opening_cash,
        terminal=a.terminal,
        floor_refusals=a.floor_refusals + b.floor_refusals,
        rail_blocks=dict(sorted(blocks.items())),
        nav=path,
    )


def with_blends(facts: Sequence[RunFacts]) -> list[RunFacts]:
    """``facts`` plus an A6 row in every cell where both D13 and M10.7 ran."""
    by_cell: dict[tuple[str, str, Decimal], dict[str, RunFacts]] = {}
    for f in facts:
        by_cell.setdefault(f.cell, {})[f.label] = f
    out = [f for f in facts if f.label != A6_BLEND]
    for rows in by_cell.values():
        if D13 in rows and M10_7_BASELINE.label in rows:
            out.append(blend(rows[D13], rows[M10_7_BASELINE.label]))
    return out


# ── A2's classification coverage (Amendment 1 (a)) ──────────────────────────────────────────────

Cell = tuple[str, str, Decimal]


@dataclass(frozen=True, slots=True)
class A2Coverage:
    """How much of each cell's floor universe the industry classification leaves out.

    The classification is a 2026 snapshot, so names delisted or merged since are absent from it,
    and under Amendment 1 an unclassified name passes the gate. ``share`` is the unclassified
    share of the floor universe (before the gate) over a cell's rebalance sessions, ``by_year`` the
    same per calendar year, and ``first_rankable`` each sector index's first rankable date.
    Produced at campaign time from the lake (M16.4); this module only reads it.
    """

    share: Mapping[Cell, Decimal]
    by_year: Mapping[Cell, Mapping[int, Decimal]]
    first_rankable: Mapping[str, date]

    def diluted(self, cell: Cell) -> bool:
        """Whether A2 decides nothing in ``cell``: over the threshold, or not measured at all."""
        share = self.share.get(cell)
        return share is None or share > DILUTION_THRESHOLD


def load_a2_coverage(path: Path) -> A2Coverage:
    """Read an A2 coverage file.

    The file is ``{"cells": [{"universe", "window", "floor", "unclassified_share",
    "by_year": {"2016": "0.42", ...}}], "first_rankable": {"<index slug>": "YYYY-MM-DD"}}``, with
    shares as decimal strings.
    """
    doc = _load(path)
    share: dict[Cell, Decimal] = {}
    by_year: dict[Cell, dict[int, Decimal]] = {}
    for entry in doc["cells"]:
        cell = (str(entry["universe"]), str(entry["window"]), Decimal(str(entry["floor"])))
        share[cell] = Decimal(str(entry["unclassified_share"]))
        by_year[cell] = {int(y): Decimal(str(v)) for y, v in entry.get("by_year", {}).items()}
    first = {str(k): date.fromisoformat(v) for k, v in doc.get("first_rankable", {}).items()}
    return A2Coverage(share=share, by_year=by_year, first_rankable=first)


def decisive(facts: Sequence[RunFacts], coverage: A2Coverage | None) -> list[RunFacts]:
    """The rows the rule may read: every row except A2's in a diluted (or unmeasured) cell."""
    return [
        f
        for f in facts
        if f.label != A2_INDUSTRY_GATE or (coverage is not None and not coverage.diluted(f.cell))
    ]


# ── the decision ─────────────────────────────────────────────────────────────────────────────────


def _row(facts: Sequence[RunFacts], label: str, cell: tuple[str, str, Decimal]) -> RunFacts | None:
    return next((f for f in facts if f.label == label and f.cell == cell), None)


def select(facts: Sequence[RunFacts], floor: Decimal = PRIMARY_FLOOR) -> list[RunFacts]:
    """Step 1: the selection window's floor-only rows at ``floor``, best XIRR/DD first.

    Only :data:`SELECTION_LABELS` take part. Ties go to the smaller drawdown, then the label. The
    choice is the first row; it is made before, and never changed by, a verification figure.
    """
    rows = [
        f for f in facts if f.cell == (FLOOR_ONLY, SELECTION, floor) and f.label in SELECTION_LABELS
    ]
    return sorted(rows, key=lambda f: (-f.ratio, f.max_drawdown, f.label))


@dataclass(frozen=True, slots=True)
class Criterion:
    """One of Step 2's five tests, for one arm."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TrialSharpe:
    """One trial's per-period Sharpe on the deciding cell, and the label it is known by."""

    key: str
    label: str
    sharpe: float


def _ratio_criterion(
    name: str, facts: Sequence[RunFacts], label: str, cell: tuple[str, str, Decimal]
) -> Criterion:
    arm, base = _row(facts, label, cell), _row(facts, D13, cell)
    if arm is None or base is None:
        return Criterion(name, False, "no row" if arm is None else "no D13 row")
    return Criterion(name, arm.ratio >= base.ratio, f"{_r(arm.ratio)} vs D13 {_r(base.ratio)}")


def _dsr(
    facts: Sequence[RunFacts], label: str, sharpes: Sequence[TrialSharpe], trials: int
) -> Criterion:
    name = "5. deflated Sharpe ≥ 0.95 vs D13"
    cell = (FLOOR_ONLY, VERIFICATION, PRIMARY_FLOOR)
    arm, base = _row(facts, label, cell), _row(facts, D13, cell)
    if arm is None or base is None:
        return Criterion(name, False, "no row" if arm is None else "no D13 row")
    if len(sharpes) < 2:
        return Criterion(name, False, f"V needs two trial Sharpes, has {len(sharpes)}")
    stats = sharpe_stats(daily_returns(arm.nav))
    benchmark = sharpe_stats(daily_returns(base.nav)).sharpe
    variance = sharpe_variance([t.sharpe for t in sharpes])
    probability = deflated_sharpe_ratio(
        stats, trials=trials, sharpe_variance=variance, benchmark_sharpe=benchmark
    )
    return Criterion(
        name,
        probability >= DSR_THRESHOLD,
        f"p = {probability:.4f}; SR {stats.sharpe:.5f} vs D13 {benchmark:.5f} per session; "
        f"N = {trials}; V = {variance:.3e} over n = {len(sharpes)}",
    )


def criteria(
    facts: Sequence[RunFacts],
    label: str,
    sharpes: Sequence[TrialSharpe],
    *,
    trials: int = TRIALS,
) -> list[Criterion]:
    """Step 2's five tests for ``label`` against D13 (pre-registration §4). All must pass.

    Ties pass criteria 1 and 2 (≥); criterion 4 compares exact drawdowns over every cell both ran.
    """
    out = [
        _ratio_criterion(
            "1a. verification XIRR/DD ≥ D13, floor-only ₹1 cr",
            facts,
            label,
            (FLOOR_ONLY, VERIFICATION, LOW_FLOOR),
        ),
        _ratio_criterion(
            "1b. verification XIRR/DD ≥ D13, floor-only ₹10 cr",
            facts,
            label,
            (FLOOR_ONLY, VERIFICATION, HIGH_FLOOR),
        ),
        _ratio_criterion(
            "2. verification XIRR/DD ≥ D13, NIFTY 500 ₹10 cr",
            facts,
            label,
            (NIFTY500, VERIFICATION, HIGH_FLOOR),
        ),
    ]
    row = _row(facts, label, (FLOOR_ONLY, VERIFICATION, HIGH_FLOOR))
    out.append(
        Criterion(
            "3. verification XIRR > 25%, floor-only ₹10 cr",
            row is not None and row.xirr > BAR,
            "no row" if row is None else _p(row.xirr),
        )
    )
    pairs = [
        (f, base)
        for f in facts
        if f.label == label and (base := _row(facts, D13, f.cell)) is not None
    ]
    worst = max((f.max_drawdown - b.max_drawdown for f, b in pairs), default=None)
    out.append(
        Criterion(
            "4. max DD ≤ D13 + 3.0pp in every covered cell",
            worst is not None and worst <= DD_TOLERANCE,
            "no covered cell" if worst is None else f"worst {_pp(worst)} over {len(pairs)} cells",
        )
    )
    out.append(_dsr(facts, label, sharpes, trials))
    return out


def trial_sharpes(facts: Sequence[RunFacts], extra: Sequence[RunFacts] = ()) -> list[TrialSharpe]:
    """V's inputs: one Sharpe per distinct configuration with a run on the deciding cell.

    The deciding cell is the verification window, floor-only, ₹10 crore. ``facts`` (the M16
    campaign's runs, blends included) take precedence over ``extra`` (earlier saved runs: M12.R,
    M14.5); among ``extra``, a later run of the same configuration replaces an earlier one.
    """
    cell = (FLOOR_ONLY, VERIFICATION, PRIMARY_FLOOR)
    chosen: dict[str, RunFacts] = {}
    for f in extra:
        if f.cell == cell and f.nav:
            chosen[f.key] = f
    for f in facts:
        if f.cell == cell and f.nav:
            chosen[f.key] = f
    return [
        TrialSharpe(key, f.label, sharpe_stats(daily_returns(f.nav)).sharpe)
        for key, f in sorted(chosen.items())
    ]


# ── rendering ────────────────────────────────────────────────────────────────────────────────────


def _p(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.01'))}%"


def _pp(value: Decimal) -> str:
    return f"{'+' if value >= 0 else ''}{(value * 100).quantize(Decimal('0.01'))}pp"


def _r(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _lakh(value: Decimal) -> str:
    return f"₹{(value / Decimal('100000')).quantize(Decimal('0.01'))}L"


def _floor_label(floor: Decimal) -> str:
    crore = floor / _CRORE
    shown = crore.to_integral_value() if crore == crore.to_integral_value() else crore
    return f"₹{shown} cr/day floor"


def _blocks(blocks: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(blocks.items())) or "—"


def _table(rows: Sequence[RunFacts], coverage: A2Coverage | None = None) -> list[str]:
    base = next((r for r in rows if r.label == D13), None)
    lines = [
        "| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs "
        "NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks "
        "| >25%? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(sorted(rows, key=lambda r: (-r.ratio, r.label)), start=1):
        name = f"**{row.label}**" if row is base else row.label
        if row.label == A2_INDUSTRY_GATE and (coverage is None or coverage.diluted(row.cell)):
            name += " *(diluted: informational, decides nothing)*"
        dx = "—" if base is None or row is base else _pp(row.xirr - base.xirr)
        dd = "—" if base is None or row is base else _pp(row.max_drawdown - base.max_drawdown)
        lines.append(
            f"| {position} | {name} | {_p(row.xirr)} | {_p(row.max_drawdown)} "
            f"| **{_r(row.ratio)}** | {dx} | {dd} | {_p(row.excess)} | {row.trades} "
            f"| {_lakh(row.charges)} | {row.floor_refusals} | {_blocks(row.rail_blocks)} "
            f"| {'yes' if row.xirr > BAR else 'no'} |"
        )
    return lines


def _cell_name(cell: tuple[str, str, Decimal]) -> str:
    universe = "floor-only" if cell[0] == FLOOR_ONLY else cell[0].upper()
    return f"{universe} {cell[1]} {_floor_label(cell[2]).removesuffix('/day floor')}"


def scorecard(facts: Sequence[RunFacts]) -> list[str]:
    """Each arm against D13, cell by cell, counted in code from the same rows as the tables.

    A cell is one (universe, window, floor); an arm counts only where D13 also ran. Comparisons
    are exact (no rounding), so a tie is a tie.
    """
    base = {f.cell: f for f in facts if f.label == D13}
    out = [
        "## Scorecard against D13 (generated)",
        "",
        "Counted in code (`backtest.m16_report.scorecard`) over every cell (universe, window, "
        "floor) that has both the arm and D13. Comparisons are exact.",
        "",
        "| Strategy | Cells | XIRR/DD better / tie / worse | Cells where XIRR/DD is worse | "
        "Max DD worse / tie / better | Worst DD increase | XIRR > 25% | A8 min-holdings refusals |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    labels = list(dict.fromkeys(f.label for f in facts))
    for label in sorted(labels, key=lambda name: (name != D13, name)):
        rows = [f for f in facts if f.label == label and f.cell in base]
        if not rows:
            continue
        cleared = sum(1 for f in rows if f.xirr > BAR)
        refusals = sum(f.floor_refusals for f in rows)
        if label == D13:
            out.append(
                f"| **{label}** | {len(rows)} | — | — | — | — | {cleared} / {len(rows)} "
                f"| {refusals} |"
            )
            continue
        pairs = [(f, base[f.cell]) for f in rows]
        better = sum(1 for f, b in pairs if f.ratio > b.ratio)
        tie = sum(1 for f, b in pairs if f.ratio == b.ratio)
        worse = [_cell_name(f.cell) for f, b in pairs if f.ratio < b.ratio]
        dd_worse = sum(1 for f, b in pairs if f.max_drawdown > b.max_drawdown)
        dd_tie = sum(1 for f, b in pairs if f.max_drawdown == b.max_drawdown)
        worst = max(f.max_drawdown - b.max_drawdown for f, b in pairs)
        worse_text = (
            "—"
            if not worse
            else f"all {len(pairs)}"
            if len(worse) == len(pairs)
            else "; ".join(worse)
        )
        out.append(
            f"| {label} | {len(pairs)} | {better} / {tie} / {len(worse)} | {worse_text} "
            f"| {dd_worse} / {dd_tie} / {len(pairs) - dd_worse - dd_tie} "
            f"| {_pp(worst) if worst > _ZERO else '—'} | {cleared} / {len(rows)} | {refusals} |"
        )
    out.append("")
    return out


def a2_coverage_lines(coverage: A2Coverage | None) -> list[str]:
    """Amendment 1 (a): A2's unclassified share per cell and year, and the indices' first dates."""
    out = ["## A2 — industry-classification coverage (Amendment 1)", ""]
    if coverage is None:
        return [*out, "No coverage file was supplied; every A2 row is diluted.", ""]
    out += [
        "Unclassified share of the floor universe (names absent from the 2026 classification, "
        f"which pass the gate). Over {_p(DILUTION_THRESHOLD)} in a cell, A2 is **diluted** "
        "there: shown, but informational, and read by no step of the rule.",
        "",
        "| Cell | Unclassified | A2 | By year |",
        "| --- | --- | --- | --- |",
    ]
    for cell in sorted(
        coverage.share,
        key=lambda c: (
            c[0] != FLOOR_ONLY,
            c[0],
            _WINDOW_ORDER.index(c[1]) if c[1] in _WINDOW_ORDER else 9,
            c[2],
        ),
    ):
        years = ", ".join(f"{y} {_p(v)}" for y, v in sorted(coverage.by_year.get(cell, {}).items()))
        verdict = "diluted" if coverage.diluted(cell) else "decides"
        out.append(
            f"| {_cell_name(cell)} | {_p(coverage.share[cell])} | {verdict} | {years or '—'} |"
        )
    out += ["", "| Sector index | First rankable date |", "| --- | --- |"]
    for slug, first in sorted(coverage.first_rankable.items()):
        out.append(f"| {slug} | {first.isoformat()} |")
    out.append("")
    return out


def _criteria_lines(label: str, tests: Sequence[Criterion], outcome: str) -> list[str]:
    lines = [f"**{label}**", "", "| Criterion | Result | Figures |", "| --- | --- | --- |"]
    for test in tests:
        lines.append(f"| {test.name} | {'PASS' if test.passed else 'FAIL'} | {test.detail} |")
    passed = all(t.passed for t in tests)
    lines += ["", f"→ {outcome if passed else 'does not pass; nothing changes'}.", ""]
    return lines


def decision(
    facts: Sequence[RunFacts],
    sharpes: Sequence[TrialSharpe],
    *,
    missing: Sequence[str] = (),
    coverage: A2Coverage | None = None,
) -> list[str]:
    """Steps 1-4 of the pre-registration's selection rule, as report lines.

    Reads only :func:`decisive` rows: A2 in a diluted cell takes part in no step (Amendment 1).
    """
    facts = decisive(facts, coverage)
    primary, secondary = select(facts, PRIMARY_FLOOR), select(facts, SECONDARY_FLOOR)
    out = [
        "## Decision (pre-registered rule)",
        "",
        f"Rule: `{PREREGISTRATION}` §4. Trial count **N = {TRIALS}** "
        f"({len(TRIALS_ON_RECORD)} configurations on record before M16 + {len(M16_TRIAL_LABELS)} "
        "M16 arms; Appendix A lists every one).",
        "",
        "**Step 1 — walk-forward choice** on the selection window, floor-only, at the "
        f"**{_floor_label(PRIMARY_FLOOR)}** (primary; pending owner confirmation before the "
        "campaign launched), by XIRR/DD:",
        "",
    ]
    for position, row in enumerate(primary, start=1):
        out.append(f"{position}. {row.label} — {_r(row.ratio)}")
    choice = primary[0].label if primary else ""
    out += [
        "",
        f"Choice: **{choice or 'none (no selection-window rows)'}**.",
        "",
        "Secondary, *informational only* — the same ranking at the "
        f"{_floor_label(SECONDARY_FLOOR)}: "
        + (", ".join(f"{r.label} ({_r(r.ratio)})" for r in secondary) or "no rows")
        + ". It decides nothing.",
        "",
        "**Step 2 — replacement test** for the choice (all five must pass):",
        "",
    ]
    if not choice:
        out += ["No choice was made; D13 stays.", ""]
    elif choice == D13:
        out += ["The choice is D13 itself; nothing changes.", ""]
    else:
        out += _criteria_lines(choice, criteria(facts, choice, sharpes), "replaces D13 for paper")
    out += ["**Step 3 — A4 and A5** (verification-only evidence; at most shadow in paper):", ""]
    for label in SHADOW_ONLY:
        if label in missing or not any(f.label == label for f in facts):
            out += [f"**{label}**: no runs.", ""]
            continue
        out += _criteria_lines(label, criteria(facts, label, sharpes), "shadow in paper")
    out += [
        f"**V** (criterion 5) is the sample variance of the per-session Sharpe on the "
        f"verification window, floor-only, {_floor_label(PRIMARY_FLOOR)}, over the n = "
        f"{len(sharpes)} trials with a saved run there: "
        + ", ".join(t.label for t in sharpes)
        + ".",
        "",
        f"Counted toward N = {TRIALS} only, with no saved run on that cell: "
        + (", ".join(_n_only(sharpes)) or "none")
        + ".",
        "",
    ]
    return out


def _n_only(sharpes: Sequence[TrialSharpe]) -> list[str]:
    """The pre-registered trials (Appendix A and the M16 arms) that did not contribute to V."""
    seen = {t.label for t in sharpes}
    return [label for label in (*TRIALS_ON_RECORD, *M16_TRIAL_LABELS) if label not in seen]


def render(
    facts: Sequence[RunFacts],
    *,
    manifests: Mapping[str, Mapping[str, Any]],
    sharpes: Sequence[TrialSharpe],
    missing: Sequence[str] = (),
    hand_written: str = "",
    a2_coverage: A2Coverage | None = None,
) -> str:
    """The whole report: provenance, the tables, the scorecard, the decision.

    Raises ``ValueError`` when A2 has rows but no coverage was supplied: its dilution label
    (Amendment 1) cannot be struck without it.
    """
    if a2_coverage is None and any(f.label == A2_INDUSTRY_GATE for f in facts):
        raise ValueError("A2 rows need an A2 coverage file (--a2-coverage; Amendment 1)")
    out = [
        "# M16 — strategy exploration (gate report)",
        "",
        "> **Generated** by `uv run python -m backtest.m16_report` from the saved runs listed "
        "below; nothing in the tables is typed by hand. The section after the marker at the end "
        "is hand-written analysis and is labelled as such.",
        "",
        f"Pre-registration: `{PREREGISTRATION}` (arms, grid, rule and N fixed before any run).",
        "",
        "| Run directory | Universe | Commit | Lake last session | Units | Arms |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, manifest in manifests.items():
        out.append(
            f"| `{name}` | {manifest['universe']} | `{manifest['commit'][:10]}` | "
            f"{manifest['lake_last_session']} | {', '.join(manifest['units'])} | "
            f"{len(manifest['arms'])} |"
        )
    out += [
        "",
        f"**A6** ({A6_BLEND}) is built here from D13's and M10.7's saved runs: half of each "
        "full ₹10 lakh run, never rebalanced between the halves; NAV the mean of the two paths, "
        "XIRR on the halved cashflows, max DD from the blended path, charges halved, trades and "
        "rail refusals summed. A8's floors would bind a little differently on two ₹5 lakh books.",
        "",
        "**A8 min-holdings refusals** are all sells A8's 8-name floor refused over the run, on "
        "any session; **all rail blocks** list every rail that refused an order, with counts.",
        "",
    ]
    if missing:
        out += [f"Arms with no option in the code at render time: {', '.join(missing)}.", ""]
    universes = sorted({f.universe for f in facts}, key=lambda u: (u != FLOOR_ONLY, u))
    for universe in universes:
        title = "floor-only universe" if universe == FLOOR_ONLY else f"{universe} universe"
        out += [f"## Results — {title}", ""]
        for window in _WINDOW_ORDER:
            if not any(f.universe == universe and f.window == window for f in facts):
                continue
            first, last = WINDOWS[window]
            out += [f"### {window} ({first.isoformat()} → {last.isoformat()})", ""]
            for floor in (LOW_FLOOR, HIGH_FLOOR):
                rows = [f for f in facts if f.cell == (universe, window, floor)]
                if rows:
                    out += [f"**{_floor_label(floor)}**", "", *_table(rows, a2_coverage), ""]
    out += a2_coverage_lines(a2_coverage)
    out += scorecard(facts)
    out += decision(facts, sharpes, missing=missing, coverage=a2_coverage)
    out += [MARKER, hand_written.strip("\n"), ""]
    return "\n".join(out)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtest.m16_report")
    parser.add_argument("run_dirs", nargs="+", type=Path, help="m16 / m16-fundamentals run dirs")
    parser.add_argument(
        "--trial-dirs",
        nargs="*",
        type=Path,
        default=[],
        help="earlier saved runs (M12.R, M14.5) read for V only; later directories win a tie",
    )
    parser.add_argument(
        "--a2-coverage",
        type=Path,
        default=None,
        help="A2's classification coverage per cell (Amendment 1); required when A2 has runs",
    )
    parser.add_argument("--out", type=Path, default=REPORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the report from ``run_dirs``; keep any hand-written analysis already in ``--out``."""
    args = _parse_args(argv)
    arms, missing = resolvable_m16_arms()
    m16_labels = _known_labels(arms)
    facts: list[RunFacts] = []
    manifests: dict[str, dict[str, Any]] = {}
    for run_dir in args.run_dirs:
        manifests[run_dir.name] = _load(run_dir / "manifest.json")
        facts.extend(collect(run_dir, m16_labels))
    facts = with_blends(facts)
    prior = _known_labels((*RERUN_ARMS, *REGIME_DAILY_SET, *CAP_TIER_ARMS, *REDEPLOY_ARMS))
    extra: list[RunFacts] = []
    for trial_dir in args.trial_dirs:
        extra.extend(collect(trial_dir, prior))
    hand_written = ""
    if args.out.is_file():
        existing = args.out.read_text(encoding="utf-8")
        if MARKER in existing:
            hand_written = existing.split(MARKER, 1)[1]
    args.out.write_text(
        render(
            facts,
            manifests=manifests,
            sharpes=trial_sharpes(facts, extra),
            missing=missing,
            hand_written=hand_written,
            a2_coverage=load_a2_coverage(args.a2_coverage) if args.a2_coverage else None,
        ),
        encoding="utf-8",
    )
    print(f"  {len(facts)} rows rendered to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
