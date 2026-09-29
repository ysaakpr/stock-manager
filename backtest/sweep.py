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
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from backtest.book_actions import add_book_actions_flag, store_book_actions_unless
from backtest.cash_interest import (
    add_cash_interest_flag,
    cash_interest_unless,
    describe_cash_interest,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters
from backtest.policies.naive_momentum import MomentumParameters
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import (
    BacktestError,
    BacktestResult,
    UniverseParameters,
    _holding_periods,
    backtest_spec,
    open_swing_lake,
    run_momentum_v2,
    run_naive_momentum,
    run_swing_composite,
)
from backtest.run_ledger import (
    RunSummary,
    add_ledger_dir_flag,
    current_ledger_dir,
    ledger_dir_unless,
    load_run,
    run_digest,
)
from backtest.tax import (
    AfterTaxResult,
    GrandfatheringPrices,
    InvestorProfile,
    RunLedger,
    TaxError,
    TaxSchedule,
    compute_after_tax,
    load_tax_schedule,
)
from backtest.tax_report import (
    L1GrandfatheringPrices,
    add_investor_flags,
    investor_profile_from_args,
)
from dataplatform.logging import get_logger
from dataplatform.query import QueryService

__all__ = [
    "ARMS",
    "BAND_HIT_ARM",
    "RETIRED_ARMS",
    "Arm",
    "SweepResult",
    "SweepRow",
    "attach_after_tax",
    "investor_assumption_lines",
    "render_sweep_report",
    "run_digests",
    "run_sweep",
    "tax_cells",
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
    #: X2 H2: no new buy of a name that hit a daily price band in the last five sessions
    #: (``backtest.band_hits``). Swing arms only. Off by default, and absent from the spec when
    #: off, so no arm defined before it changes digest.
    band_hit_avoidance: bool = False

    def __post_init__(self) -> None:
        driving = [p for p in (self.swing, self.naive, self.v2) if p is not None]
        if len(driving) != 1:
            raise ValueError(f"{self.label}: an arm drives exactly one policy, got {len(driving)}")
        if self.band_hit_avoidance and self.swing is None:
            raise ValueError(f"{self.label}: band-hit avoidance is a swing-policy filter")


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
        label="Short composite, top-10 + regime",
        family="concentration",
        reference="Short composite, top-10",
        note="the regime gate on the concentrated arm",
        swing=_swing(**_SHORT_COMPOSITE, top_n=10, sell_band=30, regime_filter=True),
    ),
    # ── the baselines the sweep has to beat ──────────────────────────────────────────────────────
    Arm(
        label="Naive momentum (M4.10)",
        family="baseline",
        reference="—",
        note="monthly, top-20 by trailing 12-month return",
        naive=MomentumParameters(top_n=20),
    ),
    Arm(
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
    ),
)


#: H2 of the round-2 pre-registration (``ops/studies/preregistration-signals-2026-09-29.md`` §3):
#: the fixed-code M10.7 composite, with no new buy of a name that closed at a daily price band in
#: any of the last five sessions. Not in :data:`ARMS`: that tuple is the round-1 sweep the campaign
#: and verdict default to, and the round-2 H-arms run only after the baseline is frozen.
BAND_HIT_ARM: Final = Arm(
    label="Swing composite + band-hit avoidance (H2)",
    family="round-2 hypothesis",
    reference=_M10_7,
    note="no new buy of a name at its upper or lower price band in the last 5 sessions",
    swing=_swing(),
    band_hit_avoidance=True,
)


#: Arms taken out of the sweep, each with the reason it is gone — printed in every sweep report so
#: a reader comparing against an older table knows the row was removed rather than lost.
RETIRED_ARMS: tuple[tuple[str, str], ...] = (
    (
        "Short composite, top-5",
        "never traded (0 trades on every window): an equal-weight top-5 buy is 20 % of the case, "
        "and the ratified rails cap a position and a single order at 15 % (RailId.MAX_POSITION, "
        "MAX_ORDER_PCT), so A8 blocked every entry. The rails are not loosened to admit it; the "
        "most concentrated basket they admit at ₹10 lakh is nine names (1/9 = 11.1 % < 15 %, "
        "₹9.8 lakh / 9 < the ₹1.2 lakh order cap, >= the 8-holding floor), and "
        "'Short composite, top-10' already measures concentration at that end",
    ),
)


# ── running them ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SweepRow:
    """One arm's result on one liquidity floor — or the reason it has no result.

    A row is backed by the run it just replayed (``run``) or, in a resumed campaign, by the run's
    persisted summary and ledger (``summary``, ``ledger``); every figure reads the same either way.
    ``after_tax`` is attached afterwards by :func:`attach_after_tax`, from the ledger alone;
    ``after_tax_error`` says why a row has none.
    """

    arm: Arm
    floor: Decimal
    run: BacktestResult | None = None
    error: str | None = None
    round_trips: int = 0
    median_hold_days: int = 0
    summary: RunSummary | None = None
    ledger: RunLedger | None = None
    after_tax: AfterTaxResult | None = None
    after_tax_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.run is not None or self.summary is not None

    @property
    def xirr(self) -> Decimal:
        if self.run is not None:
            return self.run.comparison.portfolio_xirr
        return self.summary.xirr if self.summary is not None else _ZERO

    @property
    def max_drawdown(self) -> Decimal:
        if self.run is not None:
            return self.run.max_drawdown
        return self.summary.max_drawdown if self.summary is not None else _ZERO

    @property
    def excess(self) -> Decimal:
        if self.run is not None:
            return self.run.comparison.excess_over_benchmark
        return self.summary.excess if self.summary is not None else _ZERO

    @property
    def total_charges(self) -> Decimal:
        if self.run is not None:
            return self.run.total_charges
        return self.summary.total_charges if self.summary is not None else _ZERO

    @property
    def run_ledger(self) -> RunLedger | None:
        """The ledger after-tax figures are struck from — the run's own, or the persisted one."""
        if self.ledger is not None:
            return self.ledger
        return self.run.ledger if self.run is not None else None

    @property
    def return_per_drawdown(self) -> Decimal:
        """XIRR / max drawdown — the ranking key (owner decision, 2026-09-07).

        A run whose NAV never fell has no drawdown to divide by. That is not an infinitely good
        arm, it is an arm the sampler never caught falling, so it is ranked last rather than first:
        an unmeasurable denominator is not a measurement.
        """
        if not self.ok or self.max_drawdown <= _ZERO:
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
    #: Rows loaded from a persisted run rather than replayed (a resumed campaign).
    resumed: int = 0
    #: The investor the after-tax columns were struck for (``attach_after_tax``); None until then.
    profile: InvestorProfile | None = None

    def ranked(self, floor: Decimal) -> list[SweepRow]:
        """Rows on ``floor``, best return-per-drawdown first, failures last."""
        rows = [row for row in self.rows if row.floor == floor]
        return sorted(rows, key=lambda r: (-r.return_per_drawdown, r.arm.label))


def _arm_spec(
    arm: Arm,
    *,
    start: date,
    end: date,
    universe: UniverseParameters,
    opening_cash: Decimal,
    adjusted: bool,
) -> dict[str, str]:
    """The specification :func:`_run_arm`'s runner will persist this arm under."""
    runner, parameters = (
        ("swing_composite", arm.swing)
        if arm.swing is not None
        else ("naive_momentum", arm.naive)
        if arm.naive is not None
        else ("momentum_v2", arm.v2)
    )
    return backtest_spec(
        runner,
        start=start,
        end=end,
        parameters=parameters,
        opening_cash=opening_cash,
        adjusted=adjusted,
        universe=universe,
        band_hit_avoidance=arm.band_hit_avoidance,
    )


def _resumed_row(arm: Arm, floor: Decimal, summary: RunSummary, ledger: RunLedger) -> SweepRow:
    return SweepRow(
        arm=arm,
        floor=floor,
        summary=summary,
        ledger=ledger,
        round_trips=summary.round_trips,
        median_hold_days=summary.median_hold_days,
    )


def run_digests(
    *,
    start: date,
    end: date,
    arms: Sequence[Arm] = ARMS,
    floors: Sequence[Decimal] = (LOW_FLOOR, HIGH_FLOOR),
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    adjusted: bool = True,
) -> dict[tuple[str, Decimal], str]:
    """The persistence digest of every (arm, floor) run a sweep over this window would make.

    The one place a sweep's run identities are derived: :func:`run_sweep` resumes by them and a
    render-only campaign checks them, so the two can never disagree on what "already run" means.
    """
    digests: dict[tuple[str, Decimal], str] = {}
    for floor in floors:
        universe = UniverseParameters(median_turnover_floor=floor)
        for arm in arms:
            spec = _arm_spec(
                arm,
                start=start,
                end=end,
                universe=universe,
                opening_cash=opening_cash,
                adjusted=adjusted,
            )
            digests[(arm.label, floor)] = run_digest(spec)
    return digests


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

    **Resumable (X2).** Under ``backtest.run_ledger.persist_run_ledgers`` every run persists its
    ledger and summary keyed by its specification's digest, and a run whose summary and ledger are
    already on disk is loaded rather than replayed. When every run is on disk the lake is never
    opened at all. A failed arm persists nothing, so it is retried on the next invocation.
    """
    began = time.perf_counter()
    out_dir = current_ledger_dir()
    universes = {floor: UniverseParameters(median_turnover_floor=floor) for floor in floors}
    done: dict[tuple[str, Decimal], SweepRow] = {}
    if out_dir is not None:
        digests = run_digests(
            start=start,
            end=end,
            arms=arms,
            floors=floors,
            opening_cash=opening_cash,
            adjusted=adjusted,
        )
        for floor in floors:
            for arm in arms:
                loaded = load_run(out_dir, digests[(arm.label, floor)])
                if loaded is not None:
                    done[(arm.label, floor)] = _resumed_row(arm, floor, *loaded)
    pending = [(floor, arm) for floor in floors for arm in arms if (arm.label, floor) not in done]
    _LOG.info(
        "sweep.plan",
        window=f"{start.isoformat()}..{end.isoformat()}",
        runs=len(floors) * len(arms),
        resumed=len(done),
        pending=len(pending),
    )

    out = SweepResult(resumed=len(done))
    fresh: dict[tuple[str, Decimal], SweepRow] = {}
    if pending:
        lake = open_swing_lake(
            start=start,
            end=end,
            floors=floors,
            data_root=data_root,
            adjusted=adjusted,
            band_hits=any(arm.band_hit_avoidance for _, arm in pending),
        )
        out.start, out.terminal, out.sessions = (
            lake.first_session,
            lake.terminal,
            len(lake.sessions),
        )
        try:
            # The one windowed pass: the union of every pending swing arm's decision dates.
            intervals = {
                arm.swing.rebalance_interval_sessions for _, arm in pending if arm.swing is not None
            }
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
            for floor, arm in pending:
                fresh[(arm.label, floor)] = _replay_arm(
                    arm,
                    floor,
                    start=start,
                    end=end,
                    universe=universes[floor],
                    opening_cash=opening_cash,
                    data_root=data_root,
                    adjusted=adjusted,
                    lake=lake,
                )
        finally:
            lake.close()

    for floor in floors:
        for arm in arms:
            row = done.get((arm.label, floor)) or fresh[(arm.label, floor)]
            out.rows.append(row)
            if row.summary is not None and not out.sessions:
                out.start, out.terminal = row.summary.start, row.summary.terminal
                out.sessions = row.summary.sessions
            if row.ok and not out.benchmark_name:
                if row.run is not None:
                    out.benchmark_name = row.run.benchmark_index_name
                    out.benchmark_xirr = row.run.comparison.benchmark_xirr
                elif row.summary is not None:
                    out.benchmark_name = row.summary.benchmark_name
                    out.benchmark_xirr = row.summary.benchmark_xirr
    out.total_seconds = time.perf_counter() - began
    return out


def _replay_arm(
    arm: Arm,
    floor: Decimal,
    *,
    start: date,
    end: date,
    universe: UniverseParameters,
    opening_cash: Decimal,
    data_root: Path | None,
    adjusted: bool,
    lake: object,
) -> SweepRow:
    """One arm on one floor against the shared lake — its row, or its failure as a row."""
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
        _LOG.warning("sweep.arm_failed", arm=arm.label, floor=str(floor), error=str(error))
        return SweepRow(arm=arm, floor=floor, error=str(error))
    _mean, median, trips = _holding_periods(run.result.journal)
    _LOG.info(
        "sweep.arm_done",
        arm=arm.label,
        floor=str(floor),
        xirr=str(run.comparison.portfolio_xirr),
        max_drawdown=str(run.max_drawdown),
        round_trips=trips,
        digest=run.digest,
        seconds=round(time.perf_counter() - started, 1),
    )
    return SweepRow(arm=arm, floor=floor, run=run, round_trips=trips, median_hold_days=median)


def attach_after_tax(
    result: SweepResult,
    profile: InvestorProfile,
    *,
    fmv: GrandfatheringPrices,
    schedule: TaxSchedule | None = None,
) -> SweepResult:
    """Strike every row's after-tax figures from its ledger, for ``profile`` (X2).

    A row whose ledger cannot be taxed — a lot needing a grandfathering price the lake lacks, a
    date the schedule does not cover — keeps its pre-tax figures and carries the reason in
    ``after_tax_error``; it is never silently shown as untaxed. Returns a new result.
    """
    schedule = schedule or load_tax_schedule()
    rows: list[SweepRow] = []
    for row in result.rows:
        ledger = row.run_ledger
        if not row.ok:
            rows.append(row)
            continue
        if ledger is None:
            rows.append(replace(row, after_tax_error="no fill ledger for this run"))
            continue
        try:
            taxed = compute_after_tax(ledger, profile, schedule=schedule, fmv=fmv)
        except TaxError as error:
            _LOG.warning("sweep.after_tax_failed", arm=row.arm.label, error=str(error))
            rows.append(replace(row, after_tax_error=str(error)))
            continue
        rows.append(replace(row, after_tax=taxed))
    return replace(result, rows=rows, profile=profile)


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
            band_hit_avoidance=arm.band_hit_avoidance,
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


# ── the report ───────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value:.2%}"


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.0f}"


def _floor_label(floor: Decimal) -> str:
    """A liquidity floor as the crore figure a reader thinks in."""
    return f"₹{floor / Decimal('10000000'):.0f} crore/day"


def tax_cells(row: SweepRow) -> tuple[str, str, str]:
    """(after-tax XIRR realised, after-tax XIRR liquidated, tax paid) as report cells.

    Raises ``ValueError`` for a row that has results but neither after-tax figures nor a stated
    reason: a report whose after-tax cells were left blank because nobody computed them would read
    as a run that paid no tax.
    """
    if row.after_tax is not None:
        at = row.after_tax
        realised = (
            _pct(at.after_tax_xirr_realised)
            if at.after_tax_xirr_realised is not None
            else f"n/a ({at.realised_xirr_error})"
        )
        if at.after_tax_xirr_liquidated is None:
            return (
                realised,
                f"n/a ({at.liquidation_error})",
                f"{_rupees(at.total_tax)} / n/a",
            )
        return (
            realised,
            _pct(at.after_tax_xirr_liquidated),
            f"{_rupees(at.total_tax)} / {_rupees(at.total_tax_liquidated)}",
        )
    if row.after_tax_error is not None:
        return ("n/a", "n/a", f"not computed: {row.after_tax_error}")
    raise ValueError(
        f"{row.arm.label}: no after-tax figures attached — call attach_after_tax before rendering"
    )


def investor_assumption_lines(profile: InvestorProfile | None) -> list[str]:
    """The header block every after-tax report opens with: who is paying, stated, not defaulted."""
    if profile is None:
        raise ValueError(
            "after-tax columns need an investor profile — call attach_after_tax with the "
            "investor's stated assumptions before rendering"
        )
    return [
        "## Investor assumptions (after-tax columns; every one stated on the command line)",
        "",
        "- **Resident individual.** Listed-equity gains under Secs 111A/112A/10(38), Sec 55(2)(ac) "
        "grandfathering with 31-01-2018 FMVs read from L1, dividends by the regime in force on "
        "the credit date (`backtest/tax_schedule.yaml`).",
        f"- **Slab rate on dividends** (from FY2020-21): {_pct(profile.slab_rate)} before "
        "surcharge and cess.",
        f"- **Surcharge:** {_pct(profile.cg_surcharge_rate)} on 111A/112A tax; "
        f"{_pct(profile.dividend_surcharge_rate)} on dividend tax. Cess: the dated schedule.",
        f"- **Tax paid:** `{profile.payment_timing.value}` — each FY's tax an investor outflow on "
        "that date, from outside the book (the walk and its NAV are untouched).",
        "- **After-tax XIRR (realised)** taxes only what the strategy sold; its closing holdings "
        "stay pre-tax. **(liquidated)** also taxes a deemed sale of every open lot at the "
        "terminal marks. Tax paid is shown as realised / including that deemed sale.",
        "- Ranking stays on pre-tax XIRR / max drawdown (owner decision, 2026-09-07); the "
        "after-tax columns sit beside it and do not re-order the table.",
        "",
    ]


def _table(result: SweepResult, floor: Decimal) -> list[str]:
    lines = [
        "| # | Strategy | Family | XIRR | After-tax XIRR (realised) | After-tax XIRR (liquidated) "
        "| Tax paid (realised / liquidated) | Max DD | **XIRR/DD** | Round trips | Median hold | "
        "Cost | Excess |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for position, row in enumerate(result.ranked(floor), start=1):
        if not row.ok:
            lines.append(
                f"| — | {row.arm.label} | {row.arm.family} | **failed** | — | — | — | — | — | — | "
                f"— | — | {row.error} |"
            )
            continue
        realised, liquidated, paid = tax_cells(row)
        lines.append(
            f"| {position} | {row.arm.label} | {row.arm.family} | {_pct(row.xirr)} | "
            f"{realised} | {liquidated} | {paid} | "
            f"{_pct(row.max_drawdown)} | **{row.return_per_drawdown:.2f}** | {row.round_trips} | "
            f"{row.median_hold_days}d | {_rupees(row.total_charges)} | {_pct(row.excess)} |"
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
        f"- {len(result.rows)} arm-runs in {result.total_seconds / 60:.0f} min total"
        + (
            f"; {result.resumed} loaded from their persisted ledgers rather than replayed"
            if result.resumed
            else ""
        ),
        "",
        *investor_assumption_lines(result.profile),
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
    if RETIRED_ARMS:
        lines += ["", "## Arms removed from the sweep", "", "| Strategy | Why |", "| --- | --- |"]
        lines += [f"| {label} | {reason} |" for label, reason in RETIRED_ARMS]
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
    add_book_actions_flag(parser)
    add_cash_interest_flag(parser, default=True)
    add_ledger_dir_flag(parser)
    add_investor_flags(parser)
    return parser.parse_args(argv)


def l1_grandfathering(data_root: Path | None) -> tuple[QueryService, L1GrandfatheringPrices]:
    """The query service and the Sec 55(2)(ac) FMV reader over it (the tax module's L1 reader)."""
    service = QueryService(data_root=data_root)
    schedule = load_tax_schedule()
    return service, L1GrandfatheringPrices(service, fmv_date=schedule.grandfather_fmv_date)


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

    profile = investor_profile_from_args(args)
    print(f"  {describe_cash_interest(args.cash_interest)}")
    with store_book_actions_unless(args), cash_interest_unless(args), ledger_dir_unless(args):
        result = run_sweep(
            start=start,
            end=end,
            arms=arms,
            floors=floors,
            opening_cash=args.opening_cash,
            data_root=args.data_root,
        )
    service, fmv = l1_grandfathering(args.data_root)
    with service:
        result = attach_after_tax(result, profile, fmv=fmv)
    for floor in floors:
        print(f"\n  {_floor_label(floor)}:")
        for position, row in enumerate(result.ranked(floor), start=1):
            if not row.ok:
                print(f"    --  {row.arm.label}: FAILED — {row.error}")
                continue
            realised, liquidated, _paid = tax_cells(row)
            print(
                f"    {position:>2}. {row.arm.label:<34} XIRR {_pct(row.xirr):>8}  "
                f"after-tax {realised:>8} / {liquidated:>8}  "
                f"DD {_pct(row.max_drawdown):>7}  ratio {row.return_per_drawdown:>5.2f}  "
                f"trips {row.round_trips:>5}"
            )
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"> {describe_cash_interest(args.cash_interest)}\n\n"
        path.write_text(header + render_sweep_report(result, floors=floors), encoding="utf-8")
        print(f"\n  report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
