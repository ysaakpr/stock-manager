"""M12.2 — a many-arm strategy sweep over one pass of the lake, ranked on return per drawdown (X2).

Every comparison in this repo so far re-read the lake once per arm. That is why no report carries
more than fourteen rows: the windowed feature query, the trading calendar, the liquidity screen's
per-date turnover medians and the regime index's per-session levels are identical for every arm on a
window, and all four are the expensive part of a run. A twenty-arm sweep priced that way is hours of
repeated I/O for a query whose answer does not change. This module builds that state once
(:func:`~backtest.run.open_swing_lake`), loads the union of every arm's decision dates into it, and
replays each arm against it.

**The arms are strategy families, not parameter noise.** Each one differs from a *named* reference
by a stated change, so a row is readable as the price of that change rather than as a point in a
grid. The families are the M10.7 composite it references, the short-horizon signals M12.1 made
expressible — five-day reversal, one-month trend, delivery acceleration, turnover expansion, a
50-session mean — the composites built from them, the holding-period axis, the risk overlays
(regime gate, low-volatility leg) and concentration. Two momentum baselines run on the identical
window, universe, cost model and benchmark, because a sweep that cannot beat the policy the repo
already had has found nothing.

**Ranking is on XIRR / max drawdown**, an owner decision (2026-09-07) and not a default. Ranked on
return alone the winner of this sweep — and of every sweep run on this lake — is whichever arm
carried the most risk, which is how a 45 %-drawdown arm comes first. Return, drawdown, round trips,
cost and benchmark excess are all reported beside the ratio, so a reader who wants a different
objective can re-rank the table by eye.

**Two liquidity floors, every arm.** M10.7 measured the composite's edge as concentrated in the
thinner half of the investable set — 5.16 % excess at the inherited Rs 1 crore median-turnover floor
against 2.78 % at Rs 10 crore — and at the low floor the median name a basket picks trades about
Rs 4.3 crore a day, where the fill model's slippage is a claim rather than a measurement. So the
low floor is the discovery number and the high floor is the reachable one, and an arm that only
works on the low floor has said something about itself.

**A failed arm is reported, never dropped.** An arm that raises out of the replay keeps its row with
the error in it. Silently shrinking the table is how a sweep reports a survivor bias it created.

What this module never does: fit a parameter to the window it reports (every arm is a stated
configuration, chosen before the run), average a figure across windows, or read a wall clock — the
replay drives a ``FrozenClock`` exactly as every other backtest here does.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from backtest.policies.momentum_v2 import MomentumV2Parameters
from backtest.policies.naive_momentum import MomentumParameters
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import (
    BacktestError,
    BacktestResult,
    UniverseParameters,
    _holding_periods,
    open_swing_lake,
    run_momentum_v2,
    run_naive_momentum,
    run_swing_composite,
)
from dataplatform.logging import get_logger

__all__ = [
    "ARMS",
    "DURATION_ARMS",
    "Arm",
    "MultiWindowSweep",
    "SweepResult",
    "SweepRow",
    "Window",
    "WindowRole",
    "WindowSweep",
    "rank_of",
    "render_sweep_report",
    "row_of",
    "run_multi_window_sweep",
    "run_sweep",
]

_LOG = get_logger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: The inherited median-turnover floor (M9.3) — the discovery number, and the optimistic one.
LOW_FLOOR = Decimal("10000000")
#: Ten times it: the floor at which the fill model's slippage is defensible for a real book.
HIGH_FLOOR = Decimal("100000000")

_REPORT_PATH = Path("ops/gates/M12-strategy-sweep-report.md")
_DEFAULT_OPENING_CASH = Decimal("1000000")


# ── the arms ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Arm:
    """One strategy in the sweep: what it is, what it differs from, and by what one change.

    ``reference`` names another arm's ``label`` (or ``"—"`` for a family head), and ``note`` states
    the single change against it. Both are printed, because a twenty-row table whose rows differ in
    unstated ways is a list of numbers rather than a comparison.

    Exactly one of ``swing``, ``naive`` or ``v2`` is set: the sweep's own arms all drive the M10.7
    swing engine, and the two momentum baselines drive their own policies so the comparison is
    against the real thing rather than against a re-expression of it.
    """

    label: str
    family: str
    reference: str
    note: str
    swing: SwingCompositeParameters | None = None
    naive: MomentumParameters | None = None
    v2: MomentumV2Parameters | None = None

    def __post_init__(self) -> None:
        driving = [p for p in (self.swing, self.naive, self.v2) if p is not None]
        if len(driving) != 1:
            raise ValueError(f"{self.label}: an arm drives exactly one policy, got {len(driving)}")


def _swing(**overrides: object) -> SwingCompositeParameters:
    """The M10.7 default with ``overrides`` applied — so every arm states its own difference."""
    return SwingCompositeParameters(**overrides)  # type: ignore[arg-type]


#: The three M10.7 legs turned off, for an arm that scores on one new leg alone.
_LEGS_OFF: dict[str, object] = {
    "weight_high": _ZERO,
    "weight_delivery": _ZERO,
    "weight_momentum": _ZERO,
}

#: The short-horizon rebuild of the composite: where the price sits in its own year, whether
#: delivery is *accelerating* (not merely high), and the one-month trend 12-1 deliberately drops.
#: Three legs, equal weights, exactly as M10.7 argued — the change is which three.
_SHORT_COMPOSITE: dict[str, object] = {
    "weight_high": _ONE,
    "weight_delivery": _ZERO,
    "weight_momentum": _ZERO,
    "weight_delivery_trend": _ONE,
    "weight_momentum_1m": _ONE,
}

_M10_7 = "Swing composite (M10.7)"
_SHORT = "Short composite"

#: The two momentum baselines, named once and shared by every arm set here. A sweep that cannot
#: beat the policy the repo already had has found nothing, so no table is printed without them.
_NAIVE_BASELINE = Arm(
    label="Naive momentum (M4.10)",
    family="baseline",
    reference="—",
    note="monthly, top-20 by trailing 12-month return",
    naive=MomentumParameters(top_n=20),
)
_V2_BASELINE = Arm(
    label="Momentum v2, all on (M9.5)",
    family="baseline",
    reference="Naive momentum (M4.10)",
    note="12-1 + band + regime + vol-scaled + redeploy + 15% vol target",
    v2=MomentumV2Parameters(
        top_n=20,
        use_12_1=True,
        sell_band=30,
        regime_filter=True,
        vol_scaled=True,
        redeploy_next_session=True,
        vol_target_annual=Decimal("0.15"),
    ),
)

ARMS: tuple[Arm, ...] = (
    # ── the reference ────────────────────────────────────────────────────────────────────────────
    Arm(
        label=_M10_7,
        family="reference",
        reference="—",
        note="fortnightly, 3x band, 25% trail, 63-session re-underwrite",
        swing=_swing(),
    ),
    # ── one new leg at a time: the short-horizon signal families ─────────────────────────────────
    Arm(
        label="Reversal: 5-day losers",
        family="single leg",
        reference=_M10_7,
        note="scores on -1 x the 5-session return alone; weekly, 10-session re-underwrite",
        swing=_swing(
            **_LEGS_OFF,
            weight_return_5=-_ONE,
            rebalance_interval_sessions=5,
            max_hold_sessions=10,
            min_hold_sessions=2,
        ),
    ),
    Arm(
        label="Trend: 1-month",
        family="single leg",
        reference=_M10_7,
        note="scores on the 21-session return alone; 21-session re-underwrite",
        swing=_swing(**_LEGS_OFF, weight_momentum_1m=_ONE, max_hold_sessions=21),
    ),
    Arm(
        label="Delivery acceleration",
        family="single leg",
        reference=_M10_7,
        note="scores on the 5/63-session delivery ratio alone — the change, not the level",
        swing=_swing(**_LEGS_OFF, weight_delivery_trend=_ONE),
    ),
    Arm(
        label="Turnover expansion",
        family="single leg",
        reference=_M10_7,
        note="scores on the 5/63-session traded-value ratio alone",
        swing=_swing(**_LEGS_OFF, weight_turnover_expansion=_ONE),
    ),
    Arm(
        label="Mean proximity (50d)",
        family="single leg",
        reference=_M10_7,
        note="scores on the close over its own 50-session mean alone",
        swing=_swing(**_LEGS_OFF, weight_ma_proximity=_ONE),
    ),
    # ── composites built from the short-horizon legs ─────────────────────────────────────────────
    Arm(
        label=_SHORT,
        family="composite",
        reference=_M10_7,
        note="52w-high + delivery acceleration + 1-month trend, replacing M10.7's three legs",
        swing=_swing(**_SHORT_COMPOSITE),
    ),
    Arm(
        label="Short composite + reversal",
        family="composite",
        reference=_SHORT,
        note="a fourth leg: -1 x the 5-session return",
        swing=_swing(**_SHORT_COMPOSITE, weight_return_5=-_ONE),
    ),
    Arm(
        label="Breakout",
        family="composite",
        reference=_M10_7,
        note="52w-high + turnover expansion + 50-session mean proximity",
        swing=_swing(
            weight_high=_ONE,
            weight_delivery=_ZERO,
            weight_momentum=_ZERO,
            weight_turnover_expansion=_ONE,
            weight_ma_proximity=_ONE,
        ),
    ),
    Arm(
        label="M10.7 + delivery acceleration",
        family="composite",
        reference=_M10_7,
        note="a fourth leg on the M10.7 composite, keeping all three of its own",
        swing=_swing(weight_delivery_trend=_ONE),
    ),
    Arm(
        label="M10.7 + 1-month trend",
        family="composite",
        reference=_M10_7,
        note="a fourth leg on the M10.7 composite, keeping all three of its own",
        swing=_swing(weight_momentum_1m=_ONE),
    ),
    # ── the holding-period axis, on the short composite ──────────────────────────────────────────
    Arm(
        label="Short composite, 2-week holds",
        family="holding period",
        reference=_SHORT,
        note="weekly decisions, 10-session re-underwrite — the bottom of the 7-90 day band",
        swing=_swing(
            **_SHORT_COMPOSITE,
            rebalance_interval_sessions=5,
            max_hold_sessions=10,
            min_hold_sessions=2,
        ),
    ),
    Arm(
        label="Short composite, 1-month holds",
        family="holding period",
        reference=_SHORT,
        note="weekly decisions, 21-session re-underwrite",
        swing=_swing(**_SHORT_COMPOSITE, rebalance_interval_sessions=5, max_hold_sessions=21),
    ),
    Arm(
        label="Short composite, 3-month holds",
        family="holding period",
        reference=_SHORT,
        note="monthly decisions, 63-session re-underwrite — the top of the band",
        swing=_swing(**_SHORT_COMPOSITE, rebalance_interval_sessions=21),
    ),
    # ── risk overlays ────────────────────────────────────────────────────────────────────────────
    Arm(
        label="M10.7 + regime gate",
        family="risk overlay",
        reference=_M10_7,
        note="no new buys while the market sits below its 200-session mean",
        swing=_swing(regime_filter=True),
    ),
    Arm(
        label="Short composite + regime gate",
        family="risk overlay",
        reference=_SHORT,
        note="no new buys while the market sits below its 200-session mean",
        swing=_swing(**_SHORT_COMPOSITE, regime_filter=True),
    ),
    Arm(
        label="Short composite + low-vol leg",
        family="risk overlay",
        reference=_SHORT,
        note="a fourth leg: -1 x trailing volatility, scored rather than only screened",
        swing=_swing(**_SHORT_COMPOSITE, weight_volatility=-_ONE),
    ),
    Arm(
        label="Short composite + regime + low-vol",
        family="risk overlay",
        reference=_SHORT,
        note="both overlays at once",
        swing=_swing(**_SHORT_COMPOSITE, regime_filter=True, weight_volatility=-_ONE),
    ),
    # ── concentration ────────────────────────────────────────────────────────────────────────────
    Arm(
        label="Short composite, top-10",
        family="concentration",
        reference=_SHORT,
        note="ten names instead of twenty; band stays 3x the basket",
        swing=_swing(**_SHORT_COMPOSITE, top_n=10, sell_band=30),
    ),
    Arm(
        label="Short composite, top-5",
        family="concentration",
        reference=_SHORT,
        note="five names instead of twenty; band stays 3x the basket",
        swing=_swing(**_SHORT_COMPOSITE, top_n=5, sell_band=15),
    ),
    Arm(
        label="Short composite, top-10 + regime",
        family="concentration",
        reference="Short composite, top-10",
        note="the regime gate on the concentrated arm",
        swing=_swing(**_SHORT_COMPOSITE, top_n=10, sell_band=30, regime_filter=True),
    ),
    # ── the baselines the sweep has to beat ──────────────────────────────────────────────────────
    _NAIVE_BASELINE,
    _V2_BASELINE,
)


# ── the duration axis, on the M10.7 composite itself (M12.3) ─────────────────────────────────────
#
# The arms above vary the *signal*: the holding-period family there sits on the short composite, so
# nothing in this module priced M10.7's own three legs at a different cadence. The owner asked for
# the composite tried "in multiple duration and window", and this is the duration half of it. Every
# arm below scores on exactly M10.7's three legs (52-week-high proximity, delivery share, 12-1) with
# every M12.1 leg at zero and every non-duration knob at its default, so a row is the price of the
# holding-period machinery and of nothing else.
#
# Two axes, both stated by the owner: the cadence (how often a decision is made) crossed with the
# re-underwrite (how long a still-qualifying name is carried before it must re-earn its place), and
# the sell band at the default cadence. `fortnightly / 63` and `band 3x` are the M10.7 default
# itself, so they are the reference row rather than duplicated cells.

#: Cadences in sessions. A trading week is 5 sessions, a month 21, a quarter 63.
_WEEKLY, _FORTNIGHTLY, _MONTHLY, _QUARTERLY = 5, 10, 21, 63

_FN_21 = "M10.7 @ fortnightly / 21-session hold"
_WK_21 = "M10.7 @ weekly / 21-session hold"
_MO_63 = "M10.7 @ monthly / 63-session hold"
_MO_126 = "M10.7 @ monthly / 126-session hold"

#: The duration grid plus the rows that price it: the M10.7 reference and the two momentum
#: baselines. Deliberately *not* folded into :data:`ARMS` — the M12.2 sweep is a signal comparison
#: with its own twenty-three arms and its own running campaign, and adding cells to it would change
#: a report that is already being generated.
#:
#: Two of these arms re-underwrite at 126 sessions, which is outside the 7-90 day band M10.7 was
#: briefed for. That is the point of a duration axis: the band was an assumption, and an axis that
#: stops at the assumption cannot say whether the assumption was right. The report says so.
DURATION_ARMS: tuple[Arm, ...] = (
    # ── the reference: fortnightly decisions, 63-session re-underwrite, 3x band ──────────────────
    Arm(
        label=_M10_7,
        family="reference",
        reference="—",
        note="fortnightly, 3x band, 25% trail, 63-session re-underwrite — the M10.7 default",
        swing=_swing(),
    ),
    # ── the re-underwrite axis at the default cadence ────────────────────────────────────────────
    Arm(
        label=_FN_21,
        family="duration",
        reference=_M10_7,
        note="re-underwrite at 21 sessions instead of 63",
        swing=_swing(max_hold_sessions=21),
    ),
    Arm(
        label="M10.7 @ fortnightly / 42-session hold",
        family="duration",
        reference=_M10_7,
        note="re-underwrite at 42 sessions instead of 63",
        swing=_swing(max_hold_sessions=42),
    ),
    # ── the cadence axis ─────────────────────────────────────────────────────────────────────────
    Arm(
        label=_WK_21,
        family="duration",
        reference=_FN_21,
        note="decide weekly instead of fortnightly (5 sessions, not 10)",
        swing=_swing(rebalance_interval_sessions=_WEEKLY, max_hold_sessions=21),
    ),
    Arm(
        label="M10.7 @ weekly / 10-session hold",
        family="duration",
        reference=_WK_21,
        note="re-underwrite at 10 sessions instead of 21, with the min-hold floor moved 5 -> 2 so "
        "it stays below half the hold",
        swing=_swing(
            rebalance_interval_sessions=_WEEKLY, max_hold_sessions=10, min_hold_sessions=2
        ),
    ),
    Arm(
        label=_MO_63,
        family="duration",
        reference=_M10_7,
        note="decide monthly instead of fortnightly (21 sessions, not 10)",
        swing=_swing(rebalance_interval_sessions=_MONTHLY),
    ),
    Arm(
        label=_MO_126,
        family="duration",
        reference=_MO_63,
        note="re-underwrite at 126 sessions instead of 63 — past the 7-90 day band, deliberately",
        swing=_swing(rebalance_interval_sessions=_MONTHLY, max_hold_sessions=126),
    ),
    Arm(
        label="M10.7 @ quarterly / 126-session hold",
        family="duration",
        reference=_MO_126,
        note="decide quarterly instead of monthly (63 sessions, not 21)",
        swing=_swing(rebalance_interval_sessions=_QUARTERLY, max_hold_sessions=126),
    ),
    # ── the sell band at the default cadence ─────────────────────────────────────────────────────
    Arm(
        label="M10.7, band 1.5x top_n",
        family="duration",
        reference=_M10_7,
        note="sell outside the top 30 instead of the top 60 — 1.5x the basket, not 3x",
        swing=_swing(sell_band=30),
    ),
    Arm(
        label="M10.7, band 5x top_n",
        family="duration",
        reference=_M10_7,
        note="sell outside the top 100 instead of the top 60 — 5x the basket, not 3x",
        swing=_swing(sell_band=100),
    ),
    # ── the baselines every row is priced against ────────────────────────────────────────────────
    _NAIVE_BASELINE,
    _V2_BASELINE,
)

# ── running them ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SweepRow:
    """One arm's result on one liquidity floor — or the reason it has no result."""

    arm: Arm
    floor: Decimal
    run: BacktestResult | None = None
    error: str | None = None
    round_trips: int = 0
    median_hold_days: int = 0

    @property
    def ok(self) -> bool:
        return self.run is not None

    @property
    def xirr(self) -> Decimal:
        return self.run.comparison.portfolio_xirr if self.run is not None else _ZERO

    @property
    def max_drawdown(self) -> Decimal:
        return self.run.max_drawdown if self.run is not None else _ZERO

    @property
    def excess(self) -> Decimal:
        return self.run.comparison.excess_over_benchmark if self.run is not None else _ZERO

    @property
    def return_per_drawdown(self) -> Decimal:
        """XIRR / max drawdown — the ranking key (owner decision, 2026-09-07).

        A run whose NAV never fell has no drawdown to divide by. That is not an infinitely good
        arm, it is an arm the sampler never caught falling, so it is ranked last rather than first:
        an unmeasurable denominator is not a measurement.
        """
        if self.run is None or self.max_drawdown <= _ZERO:
            return _ZERO
        return self.xirr / self.max_drawdown


@dataclass(slots=True)
class SweepResult:
    """Every row, plus what the run itself needs to state about how it was produced."""

    rows: list[SweepRow] = field(default_factory=list)
    start: date = date.min
    terminal: date = date.min
    sessions: int = 0
    feature_dates: int = 0
    lake_seconds: float = 0.0
    total_seconds: float = 0.0
    benchmark_xirr: Decimal = _ZERO
    benchmark_name: str = ""

    def ranked(self, floor: Decimal) -> list[SweepRow]:
        """Rows on ``floor``, best return-per-drawdown first, failures last."""
        rows = [row for row in self.rows if row.floor == floor]
        return sorted(rows, key=lambda r: (-r.return_per_drawdown, r.arm.label))


def rank_of(result: SweepResult, label: str, floor: Decimal) -> int | None:
    """Where the arm ``label`` placed in ``result`` on ``floor``, or ``None`` if it has no row."""
    for position, row in enumerate(result.ranked(floor), start=1):
        if row.arm.label == label:
            return position
    return None


def row_of(result: SweepResult, label: str, floor: Decimal) -> SweepRow | None:
    """The arm ``label``'s row in ``result`` on ``floor``, or ``None`` if it has none."""
    for row in result.ranked(floor):
        if row.arm.label == label:
            return row
    return None


def run_sweep(
    *,
    start: date,
    end: date,
    arms: Sequence[Arm] = ARMS,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    adjusted: bool = True,
) -> SweepResult:
    """Replay every arm on every floor against one shared lake, and collect the rows (M12.2).

    Assumes the arms are stated configurations, not a grid to be searched: nothing here is fitted to
    the window. Never drops a failing arm — its row carries the error instead of a result.
    """
    began = time.perf_counter()
    lake = open_swing_lake(
        start=start, end=end, floors=floors, data_root=data_root, adjusted=adjusted
    )
    out = SweepResult(start=lake.first_session, terminal=lake.terminal, sessions=len(lake.sessions))
    try:
        # The one windowed pass: the union of every swing arm's decision dates, loaded once.
        intervals = {arm.swing.rebalance_interval_sessions for arm in arms if arm.swing is not None}
        decision_dates = sorted(
            {session for step in intervals for session in lake.sessions[::step]}
        )
        lake.features.load(decision_dates)
        out.feature_dates = len(decision_dates)
        out.lake_seconds = time.perf_counter() - began
        _LOG.info(
            "sweep.lake_ready",
            sessions=len(lake.sessions),
            decision_dates=len(decision_dates),
            cadences=sorted(intervals),
            seconds=round(out.lake_seconds, 1),
        )

        for floor in floors:
            universe = UniverseParameters(median_turnover_floor=floor)
            for arm in arms:
                started = time.perf_counter()
                try:
                    run = _run_arm(
                        arm,
                        start=start,
                        end=end,
                        universe=universe,
                        opening_cash=opening_cash,
                        data_root=data_root,
                        adjusted=adjusted,
                        lake=lake,
                    )
                except (BacktestError, ValueError, ArithmeticError) as error:
                    _LOG.warning(
                        "sweep.arm_failed", arm=arm.label, floor=str(floor), error=str(error)
                    )
                    out.rows.append(SweepRow(arm=arm, floor=floor, error=str(error)))
                    continue
                _mean, median, trips = _holding_periods(run.result.journal)
                out.rows.append(
                    SweepRow(
                        arm=arm,
                        floor=floor,
                        run=run,
                        round_trips=trips,
                        median_hold_days=median,
                    )
                )
                if not out.benchmark_name:
                    out.benchmark_name = run.benchmark_index_name
                    out.benchmark_xirr = run.comparison.benchmark_xirr
                _LOG.info(
                    "sweep.arm_done",
                    arm=arm.label,
                    floor=str(floor),
                    xirr=str(run.comparison.portfolio_xirr),
                    max_drawdown=str(run.max_drawdown),
                    round_trips=trips,
                    seconds=round(time.perf_counter() - started, 1),
                )
    finally:
        lake.close()
    out.total_seconds = time.perf_counter() - began
    return out


def _run_arm(
    arm: Arm,
    *,
    start: date,
    end: date,
    universe: UniverseParameters,
    opening_cash: Decimal,
    data_root: Path | None,
    adjusted: bool,
    lake: object,
) -> BacktestResult:
    """Drive whichever policy this arm carries. The baselines build their own lake by design."""
    if arm.swing is not None:
        return run_swing_composite(
            start=start,
            end=end,
            parameters=arm.swing,
            opening_cash=opening_cash,
            data_root=data_root,
            adjusted=adjusted,
            universe=universe,
            lake=lake,  # type: ignore[arg-type]
        )
    if arm.naive is not None:
        return run_naive_momentum(
            start=start,
            end=end,
            parameters=arm.naive,
            opening_cash=opening_cash,
            data_root=data_root,
            adjusted=adjusted,
            universe=universe,
        )
    assert arm.v2 is not None
    return run_momentum_v2(
        start=start,
        end=end,
        v2_parameters=arm.v2,
        opening_cash=opening_cash,
        data_root=data_root,
        adjusted=adjusted,
        universe=universe,
    )


# ── many windows, one pass each (M12.3) ──────────────────────────────────────────────────────────
#
# A sweep over one window *selects* on that window. The answer is not a better single window but
# several stated ones, each reported on its own — so this runs the same arm set over a list of
# windows in one invocation, giving each window its own lake pass and its own table.
#
# **Nothing here averages.** There is deliberately no accessor that pools rows from two windows into
# one ranking, because a mean XIRR across a decade and a six-year window inside it would hide the
# one fact `ops/gates/algo-reevaluation-2026-09-07.md` established: the same policies earn ~22 %
# over six years and 12-15 % over ten, and the six-year window is the flattering one. Averaging
# those is not a summary, it is the deletion of the finding.


class WindowRole(StrEnum):
    """What a window is for.

    Only the ``SELECTION``/``VERIFICATION`` pair carries a rule: the selection window is swept
    first and its winner frozen *before* the verification window is read, so the choice on record
    is the choice that was actually available at the time. ``STANDALONE`` windows are reported on
    their own and take no part in that.
    """

    STANDALONE = "standalone"
    SELECTION = "selection"
    VERIFICATION = "verification"


@dataclass(frozen=True, slots=True)
class Window:
    """One stated window: what to call it, when it opens and closes, and what it is for."""

    label: str
    start: date
    end: date
    role: WindowRole = WindowRole.STANDALONE

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"{self.label}: {self.end.isoformat()} is before {self.start}")
        if not self.label.strip():
            raise ValueError("a window must be labelled — an unnamed table cannot be read")


@dataclass(frozen=True, slots=True)
class WindowSweep:
    """One window and the sweep that ran on it. Never merged with another window's."""

    window: Window
    result: SweepResult


@dataclass(slots=True)
class MultiWindowSweep:
    """Every window's own table, plus the name frozen from the selection window.

    Exposes per-window lookups only. There is no pooled ranking and no cross-window mean by
    design — see the section comment above.
    """

    windows: list[WindowSweep] = field(default_factory=list)
    #: The arm that won the selection window, frozen before the verification window was swept.
    selected: str = ""

    def result_for(self, label: str) -> SweepResult:
        """The sweep for the window called ``label``. Raises if there is no such window."""
        for entry in self.windows:
            if entry.window.label == label:
                return entry.result
        raise KeyError(f"no window called {label!r} in this sweep")

    def with_role(self, role: WindowRole) -> WindowSweep | None:
        """The single window in ``role``, or ``None``. Roles other than standalone are unique."""
        return next((entry for entry in self.windows if entry.window.role is role), None)


def _validate_windows(windows: Sequence[Window]) -> None:
    """Refuse a window list that cannot produce an honest walk-forward, rather than warn on it."""
    if not windows:
        raise ValueError("a multi-window sweep needs at least one window")
    labels = [window.label for window in windows]
    if len(set(labels)) != len(labels):
        raise ValueError(f"window labels must be unique, got {labels}")
    for role in (WindowRole.SELECTION, WindowRole.VERIFICATION):
        if sum(1 for window in windows if window.role is role) > 1:
            raise ValueError(f"at most one {role.value} window, got more")
    selection = next((w for w in windows if w.role is WindowRole.SELECTION), None)
    verification = next((w for w in windows if w.role is WindowRole.VERIFICATION), None)
    if verification is None:
        return
    if selection is None:
        raise ValueError("a verification window without a selection window verifies nothing")
    if selection.end >= verification.start:
        raise ValueError(
            f"the selection window must close before verification opens: "
            f"{selection.end.isoformat()} is not before {verification.start.isoformat()}"
        )
    if windows.index(selection) > windows.index(verification):
        raise ValueError(
            "the selection window must be swept before the verification window — a name chosen "
            "with the verification figures already in hand is not a choice"
        )


def run_multi_window_sweep(
    *,
    windows: Sequence[Window],
    arms: Sequence[Arm] = DURATION_ARMS,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    adjusted: bool = True,
) -> MultiWindowSweep:
    """Run ``arms`` over every window in one invocation — one lake pass per window (M12.3).

    Assumes each window is a stated choice, not a search: nothing is fitted to any of them. Sweeps
    the windows in the order given, so a selection window's winner is frozen before a verification
    window is read. Never averages a figure across windows and never pools two windows' rows into
    one ranking — each window keeps its own table.
    """
    _validate_windows(windows)
    out = MultiWindowSweep()
    for window in windows:
        _LOG.info(
            "sweep.window_start",
            window=window.label,
            role=window.role.value,
            start=window.start.isoformat(),
            end=window.end.isoformat(),
            arms=len(arms),
        )
        # One `run_sweep` per window is one `open_swing_lake` per window: a second pass on the same
        # window is the defect `tests/unit/test_sweep.py` exists to catch.
        result = run_sweep(
            start=window.start,
            end=window.end,
            arms=arms,
            floors=floors,
            opening_cash=opening_cash,
            data_root=data_root,
            adjusted=adjusted,
        )
        out.windows.append(WindowSweep(window=window, result=result))
        if window.role is WindowRole.SELECTION:
            # Frozen here. Nothing below this line may change it — the verification window has not
            # been swept yet, which is the whole point.
            out.selected = next((row.arm.label for row in result.ranked(floors[0]) if row.ok), "")
            _LOG.info("sweep.window_selected", window=window.label, arm=out.selected)
    return out


# ── the report ───────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value:.2%}"


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.0f}"


def _floor_label(floor: Decimal) -> str:
    """A liquidity floor as the crore figure a reader thinks in."""
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


def _table(result: SweepResult, floor: Decimal) -> list[str]:
    lines = [
        "| # | Strategy | Family | XIRR | Max DD | **XIRR/DD** | Round trips | Median hold | "
        "Cost | Excess |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(result.ranked(floor), start=1):
        if not row.ok:
            lines.append(
                f"| — | {row.arm.label} | {row.arm.family} | **failed** | — | — | — | — | — | "
                f"{row.error} |"
            )
            continue
        assert row.run is not None
        lines.append(
            f"| {position} | {row.arm.label} | {row.arm.family} | {_pct(row.xirr)} | "
            f"{_pct(row.max_drawdown)} | **{row.return_per_drawdown:.2f}** | {row.round_trips} | "
            f"{row.median_hold_days}d | {_rupees(row.run.total_charges)} | {_pct(row.excess)} |"
        )
    return lines


def render_sweep_report(result: SweepResult, *, floors: Sequence[Decimal]) -> str:
    """The M12.2 markdown: one ranked table per liquidity floor, then what each arm changed."""
    best = result.ranked(floors[0])
    top = next((row for row in best if row.ok), None)
    lines = [
        "# M12.2 — The strategy sweep: every arm on one lake pass, ranked on return per drawdown",
        "",
        "*Generated by `python -m backtest.sweep --report`. Every arm replays the same sessions, "
        "the same investable universe, the same shared cost model and the same benchmark, so a row "
        "differs from its stated reference by exactly the change named in the last table. Ranked "
        "on XIRR divided by max drawdown — an owner decision (2026-09-07), because ranked on "
        "return alone the winner is whichever arm carried the most risk.*",
        "",
        "## Window and cost of the run",
        "",
        f"- {result.start.isoformat()} → {result.terminal.isoformat()} "
        f"({result.sessions} sessions)",
        f"- Benchmark: **{_pct(result.benchmark_xirr)}** ({result.benchmark_name})",
        f"- One windowed lake pass over **{result.feature_dates}** decision dates, built in "
        f"{result.lake_seconds:.0f}s and shared by every swing arm",
        f"- {len(result.rows)} arm-runs in {result.total_seconds / 60:.0f} min total",
        "",
    ]
    if top is not None:
        lines += [
            f"**Best on return per unit of drawdown:** {top.arm.label} — {_pct(top.xirr)} XIRR "
            f"against a {_pct(top.max_drawdown)} drawdown ({top.return_per_drawdown:.2f}), on the "
            f"{_floor_label(floors[0])} floor.",
            "",
        ]
    for floor in floors:
        lines += [
            f"## Ranked — {_floor_label(floor)} liquidity floor",
            "",
            *_table(result, floor),
            "",
        ]
    lines += [
        "## What each arm changed",
        "",
        "| Strategy | Differs from | By |",
        "| --- | --- | --- |",
    ]
    for arm in dict.fromkeys(row.arm for row in result.rows):
        lines.append(f"| {arm.label} | {arm.reference} | {arm.note} |")
    lines += [
        "",
        "## What this table cannot be asked to prove",
        "",
        "- **A sweep selects on the window it runs.** Twenty-odd arms on one decade means the "
        "top row is partly the arm this decade flattered. M12.3's walk-forward split is the "
        "answer to that, and until it has run, no row here is an out-of-sample number.",
        "- **The low floor is the optimistic end.** At a ₹1 crore median-turnover floor the "
        "median name a basket picks trades a few crore a day, where the fill model's base "
        "slippage is a claim and not a measurement. The ₹10 crore table is the one to plan "
        "against; where the two disagree, believe the second.",
        "- **Excess is against a price-return L1 proxy** (M9.4), not a licensed total-return "
        "index, so it overstates excess by roughly the market's dividend yield.",
        "- **Return per drawdown is a ratio of two noisy numbers.** Max drawdown is a single "
        "worst path, not a distribution, and two arms within a few hundredths of each other are "
        "not distinguishable on this evidence.",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.sweep",
        description="Sweep every strategy arm over one lake pass and rank them (M12.2).",
    )
    parser.add_argument("--from", dest="start", required=True, help="start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="end date, YYYY-MM-DD")
    parser.add_argument(
        "--report",
        nargs="?",
        const=str(_REPORT_PATH),
        default=None,
        help=f"write the markdown report (default path {_REPORT_PATH})",
    )
    parser.add_argument(
        "--arms",
        default=None,
        help="comma-separated substrings; only arms whose label matches one are run. For "
        "smoke-testing the wiring, never for reporting a subset as the sweep",
    )
    parser.add_argument(
        "--floors",
        default="low,high",
        help="which liquidity floors to run: low (₹1cr), high (₹10cr), or both (default)",
    )
    parser.add_argument("--opening-cash", type=Decimal, default=_DEFAULT_OPENING_CASH)
    parser.add_argument("--data-root", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.sweep``. Returns a process exit code."""
    args = _parse_args(argv)
    try:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as error:
        print(f"error: bad date: {error}", file=sys.stderr)
        return 2
    if end < start:
        print(f"error: --to {end} is before --from {start}", file=sys.stderr)
        return 2

    names = {"low": LOW_FLOOR, "high": HIGH_FLOOR}
    try:
        floors = tuple(names[part.strip()] for part in args.floors.split(","))
    except KeyError as error:
        print(f"error: unknown floor {error}; use low, high or low,high", file=sys.stderr)
        return 2

    arms = ARMS
    if args.arms:
        wanted = [part.strip().lower() for part in args.arms.split(",") if part.strip()]
        arms = tuple(a for a in ARMS if any(w in a.label.lower() for w in wanted))
        if not arms:
            print(f"error: no arm matches {args.arms}", file=sys.stderr)
            return 2

    result = run_sweep(
        start=start,
        end=end,
        arms=arms,
        floors=floors,
        opening_cash=args.opening_cash,
        data_root=args.data_root,
    )
    for floor in floors:
        print(f"\n  {_floor_label(floor)}:")
        for position, row in enumerate(result.ranked(floor), start=1):
            if not row.ok:
                print(f"    --  {row.arm.label}: FAILED — {row.error}")
                continue
            print(
                f"    {position:>2}. {row.arm.label:<34} XIRR {_pct(row.xirr):>8}  "
                f"DD {_pct(row.max_drawdown):>7}  ratio {row.return_per_drawdown:>5.2f}  "
                f"trips {row.round_trips:>5}"
            )
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_sweep_report(result, floors=floors), encoding="utf-8")
        print(f"\n  report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
