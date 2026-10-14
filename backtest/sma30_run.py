"""Research (MA-timing study, part 3): run the stock-level 30-DMA arms through the repo's harness.

The features are struck in one DuckDB pass on the swing composite's seam-consistent adjusted price
(:func:`backtest.run._seam_consistent_px`, L2 overlaid on raw L1, pre-seam split factors from the
store), so a split or bonus moves no ratio a rule reads. The run is wired exactly as
:func:`backtest.run.run_swing_composite`: raw L1 bars, ``SimBroker`` with the one shared cost model
(fills at the next session's open), M4.6 accounting, A8's ratified rails through ``RailGate``, the
same investable/liquid screen, the published NIFTY 50 TRI benchmark, and the run persisted through
``backtest.run_ledger`` so the campaign is resumable and the M14.5 collector can read it.

The campaign (:func:`main`) runs every arm on the mandate's windows (decade, six-year, the
walk-forward halves) and both liquidity floors, at most two workers, one lake per (window, floor).

What it never does: write under the lake, fetch, read a wall clock into a result, or let a frame
reach forward (every window frame is ``ROWS BETWEEN n PRECEDING AND CURRENT ROW``).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Final

from backtest.accounting import PortfolioBook
from backtest.book_actions import (
    corporate_actions_in_force,
    current_signal_split_factors,
    load_store_book_actions,
)
from backtest.campaign import MAX_WORKERS, CampaignError, _git_commit, check_manifest
from backtest.cash_interest import accrue_cash_interest, load_repo_rate_schedule
from backtest.policies.momentum_v2 import RegimeReading
from backtest.policies.naive_momentum import MomentumParameters
from backtest.policies.sma30_stock import Entry, Sma30Parameters, Sma30Record, Sma30StockPolicy
from backtest.rails import RailGate, ratified_backtest_rail_policy
from backtest.replay import ReplayEngine
from backtest.run import (
    _ACCOUNT_STATE,
    _BENCHMARK_TRI_SLUG,
    _DEFAULT_OPENING_CASH,
    _SWING_MIN_HISTORY,
    _SWING_MOM_LONG,
    _SWING_MOM_SHORT,
    _SWING_VOL_WINDOW,
    UNIVERSE_TURNOVER_FLOOR,
    BacktestResult,
    UniverseParameters,
    _AccountingBroker,
    _decision_counts,
    _finish_backtest,
    _held_by,
    _HoldingMarks,
    _InvestableUniverse,
    _L1Market,
    _L1Reader,
    _max_drawdown,
    _RegimeSource,
    _register_split_factors,
    _reserve_fill_headroom,
    _resolve_benchmark,
    _seam_consistent_px,
    _terminal_prices,
    backtest_spec,
)
from backtest.run_ledger import (
    persist_run_ledgers,
    refuse_lake_location,
    run_digest,
    summary_path,
)
from backtest.sweep import HIGH_FLOOR, LOW_FLOOR
from dataplatform.clock import FrozenClock
from dataplatform.logging import get_logger
from dataplatform.query import default_price_quarantine
from dataplatform.query.pit import Dataset
from dataplatform.query.universe import InMemoryListingCalendar, pit_universe
from dataplatform.store.l2 import open_connection
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker

__all__ = [
    "ARMS",
    "EMA_SPAN",
    "SMA_WINDOW",
    "Sma30Arm",
    "Sma30Features",
    "Sma30Lake",
    "main",
    "open_sma30_lake",
    "run_sma30",
]

_LOG = get_logger(__name__)
_ZERO = Decimal("0")
_ONE = Decimal("1")

SMA_WINDOW: Final = 30
EMA_SPAN: Final = 10
#: EMA10 is the exponentially weighted sum over this many sessions, renormalised: the omitted
#: tail weighs (9/11)^60 ~ 6e-6 of the total, below any decision's resolution.
_EMA_TERMS: Final = 60
_UPTREND_SESSIONS: Final = 5
_PULLBACK_LOW_BAND: Final = Decimal("0.02")
_PULLBACK_CLOSE_FLOOR: Final = Decimal("0.01")
_CROSS_FRESH_SESSIONS: Final = 5

#: Process-wide caches, shared by every arm, floor and window a worker runs (all read the same
#: lake): the trailing median turnover per (start, end) — the expensive half of the liquidity
#: screen, and floor-independent — and the screened universe per (floor, session).
_MEDIANS: dict[tuple[date, date], dict[str, Decimal]] = {}
_UNIVERSE: dict[tuple[Decimal, date], frozenset[str]] = {}


@dataclass(frozen=True, slots=True)
class Sma30Arm:
    label: str
    note: str
    params: Sma30Parameters


def _arm(label: str, note: str, **kw: Any) -> Sma30Arm:
    return Sma30Arm(label, note, Sma30Parameters(**kw))


_STOP = Decimal("0.03")
#: Every arm, fixed before any window was run: P3a/P3b/P3c x (no gate, 200d gate), equal weight,
#: and the gated P3c again vol-scaled. (A 30-session-gate arm and two more vol-scaled rows were
#: cut before any mandate window was read, for compute: the box could not fit 10 arms x 8 units
#: before the evening paper session.)
ARMS: Final[tuple[Sma30Arm, ...]] = (
    _arm("P3a crossover", "buy EMA10 up through SMA30, sell below", entry=Entry.CROSSOVER),
    _arm(
        "P3a crossover + 200d gate",
        "P3a; no new buys while NIFTY 50 TRI < its 200-session SMA",
        entry=Entry.CROSSOVER,
        regime_ma_days=200,
    ),
    _arm("P3b pullback", "uptrend + pullback-bounce entry, death-cross exit", entry=Entry.PULLBACK),
    _arm(
        "P3b pullback + 200d gate",
        "P3b; no new buys while NIFTY 50 TRI < its 200-session SMA",
        entry=Entry.PULLBACK,
        regime_ma_days=200,
    ),
    _arm(
        "P3c pullback + 3% stop",
        "P3b + sell on a close more than 3% below SMA30",
        entry=Entry.PULLBACK,
        protective_exit=_STOP,
    ),
    _arm(
        "P3c pullback + 3% stop + 200d gate",
        "P3c; no new buys while NIFTY 50 TRI < its 200-session SMA",
        entry=Entry.PULLBACK,
        protective_exit=_STOP,
        regime_ma_days=200,
    ),
    _arm(
        "P3c pullback + 3% stop + 200d gate, vol-scaled",
        "as the gated P3c, vol-scaled",
        entry=Entry.PULLBACK,
        protective_exit=_STOP,
        regime_ma_days=200,
        vol_scaled=True,
    ),
)


# ── features ─────────────────────────────────────────────────────────────────────────────────────


def _q(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 8)))


class Sma30Features:
    """Every 30-DMA fact for every (session, ISIN) in a window, in one backward-only pass.

    Keeps only the rows a decision can act on — an entry signal, a death cross, or a close below
    SMA30 — so a decade fits in memory; a name without a row that session has no exit and no entry.
    """

    def __init__(self, *, data_root: Path | None, split_factors: Any) -> None:
        self._con = open_connection()
        quarantine = default_price_quarantine()
        quarantine.register_raw_view(self._con, view="l1_sma_raw", data_root=data_root)
        quarantine.register_adjusted_view(self._con, view="l2_sma_adj", data_root=data_root)
        self._have_factors = _register_split_factors(self._con, "sma_split_factors", split_factors)
        self._by_date: dict[date, tuple[Sma30Record, ...]] = {}

    def sql(self) -> str:
        px = _seam_consistent_px(factors="sma_split_factors", have_factors=self._have_factors)
        alpha = 2.0 / (EMA_SPAN + 1)
        weights = [alpha * (1 - alpha) ** k for k in range(_EMA_TERMS)]
        total = sum(weights)
        ema = " + ".join(
            f"{w / total!r} * lag(px, {k}) OVER w" if k else f"{w / total!r} * px"
            for k, w in enumerate(weights)
        )
        n = SMA_WINDOW
        return f"""
        WITH l2_sma_anchor AS (SELECT isin, min(trade_date) AS d0,
                 arg_min(cum_price_factor, trade_date) AS c0 FROM l2_sma_adj
                 WHERE exchange = 'NSE' GROUP BY isin),
        base AS (
            SELECT r.isin, r.trade_date, {px} AS px, r.close AS raw_close, r.low AS raw_low
            FROM l1_sma_raw r
            LEFT JOIN l2_sma_adj a ON a.isin = r.isin AND a.trade_date = r.trade_date
                AND a.exchange = 'NSE'
            LEFT JOIN l2_sma_anchor an ON an.isin = r.isin
            WHERE r.exchange = 'NSE' AND r.series = 'EQ' AND r.close > 0
        ),
        ind AS (
            SELECT *,
                raw_low * px / raw_close AS adj_low,
                row_number() OVER w AS n,
                avg(px) OVER (w ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS sma,
                count(px) OVER (w ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW) AS sma_n,
                {ema} AS ema,
                ln(px / NULLIF(lag(px, 1) OVER w, 0)) AS lr,
                lag(px, {_SWING_MOM_SHORT}) OVER w
                    / NULLIF(lag(px, {_SWING_MOM_LONG}) OVER w, 0) - 1 AS mom
            FROM base WINDOW w AS (PARTITION BY isin ORDER BY trade_date)
        ),
        fl AS (
            SELECT *,
                stddev_samp(lr) OVER (w ROWS BETWEEN {_SWING_VOL_WINDOW - 1} PRECEDING
                    AND CURRENT ROW) AS vol,
                (sum(CASE WHEN px > sma THEN 1 ELSE 0 END) OVER (w ROWS BETWEEN
                    {_UPTREND_SESSIONS - 1} PRECEDING AND CURRENT ROW) = {_UPTREND_SESSIONS}
                 AND sma > lag(sma, {_UPTREND_SESSIONS}) OVER w) AS up,
                (adj_low <= sma * (1 + {_PULLBACK_LOW_BAND})
                 AND px >= sma * (1 - {_PULLBACK_CLOSE_FLOOR})) AS pull,
                -- Rounded ratios, so a flat series (EMA == SMA up to float noise) never "crosses".
                round(px / sma, 8) AS c2s,
                round(ema / sma, 8) AS e2s
            FROM ind WINDOW w AS (PARTITION BY isin ORDER BY trade_date)
        ),
        sig AS (
            SELECT trade_date, isin, raw_close, c2s, e2s, mom, vol, n, sma_n,
                (COALESCE(lag(up, 2) OVER w, false) AND COALESCE(lag(pull, 1) OVER w, false)
                 AND px > lag(px, 1) OVER w AND px > sma) AS bounce,
                (e2s > 1 AND max(CASE WHEN crossed THEN 1 ELSE 0 END) OVER (w ROWS BETWEEN
                    {_CROSS_FRESH_SESSIONS - 1} PRECEDING AND CURRENT ROW) = 1) AS golden
            FROM (SELECT *, (e2s > 1 AND lag(e2s, 1) OVER w <= 1) AS crossed
                  FROM fl WINDOW w AS (PARTITION BY isin ORDER BY trade_date))
            WINDOW w AS (PARTITION BY isin ORDER BY trade_date)
        ),
        kept AS (
            SELECT *, median(vol) OVER (PARTITION BY trade_date) AS med_vol FROM sig
            WHERE trade_date BETWEEN ? AND ? AND n >= {_SWING_MIN_HISTORY} AND sma_n = {n}
              AND c2s IS NOT NULL AND e2s IS NOT NULL AND raw_close > 0
        )
        SELECT trade_date, isin, raw_close, c2s, e2s, golden, bounce, mom, vol, med_vol
        FROM kept
        WHERE COALESCE(bounce, false) OR COALESCE(golden, false) OR e2s < 1 OR c2s < 1
        ORDER BY trade_date, isin
        """

    def load(self, start: date, end: date) -> None:
        """Materialise every acted-on row in ``[start, end]``. Once per lake."""
        began = time.perf_counter()
        rows = self._con.execute(self.sql(), [start, end]).fetchall()
        grouped: dict[date, list[Sma30Record]] = {}
        for d, isin, raw, c2s, e2s, golden, bounce, mom, vol, med in rows:
            grouped.setdefault(d, []).append(
                Sma30Record(
                    isin=str(isin),
                    price=Decimal(str(raw)),
                    knowable_date=d,
                    close_to_sma=_q(c2s) or _ZERO,
                    ema_to_sma=_q(e2s) or _ZERO,
                    golden_cross=bool(golden),
                    bounce=bool(bounce),
                    momentum_12_1=_q(mom),
                    volatility=_q(vol),
                    median_volatility=_q(med),
                )
            )
        self._by_date = {d: tuple(v) for d, v in grouped.items()}
        _LOG.info(
            "backtest.sma30_features",
            start=start.isoformat(),
            end=end.isoformat(),
            rows=len(rows),
            sessions=len(grouped),
            seconds=round(time.perf_counter() - began, 1),
            split_factors=self._have_factors,
        )

    def records(self, session: date) -> tuple[Sma30Record, ...]:
        return self._by_date.get(session, ())

    def close(self) -> None:
        self._con.close()


class _Sma30Data:
    """:class:`~backtest.policies.sma30_stock.Sma30Data` over a lake, for one arm and floor."""

    def __init__(
        self,
        reader: _L1Reader,
        features: Sma30Features,
        regime: _RegimeSource,
        universe_filter: _InvestableUniverse | None,
    ) -> None:
        self._reader = reader
        self._features = features
        self._regime = regime
        self._universe_filter = universe_filter
        self._windows = reader.listing_windows()

    def _universe(self, as_of: date) -> frozenset[str]:
        floor = (
            self._universe_filter.params.median_turnover_floor
            if self._universe_filter is not None
            else Decimal(-1)
        )
        key = (floor, as_of)
        if key not in _UNIVERSE:
            universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
            if self._universe_filter is not None:
                universe = frozenset(self._universe_filter.constrain(as_of, universe))
            _UNIVERSE[key] = frozenset(universe)
        return _UNIVERSE[key]

    def candidates(self, as_of: date) -> Dataset[Sma30Record]:
        records = self._features.records(as_of)
        if records:
            universe = self._universe(as_of)
            records = tuple(r for r in records if r.isin in universe)
        return Dataset.declaring(
            f"sma30@{as_of.isoformat()}", records, knowable_date=lambda r: r.knowable_date
        )

    def held(self, as_of: date) -> Dataset[Sma30Record]:
        return Dataset.declaring(
            f"sma30_held@{as_of.isoformat()}",
            self._features.records(as_of),
            knowable_date=lambda r: r.knowable_date,
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = self._regime.reading(as_of)
        return Dataset.declaring(
            f"sma30_regime@{as_of.isoformat()}", (reading,), knowable_date=lambda r: r.knowable_date
        )


@dataclass(frozen=True, slots=True)
class Sma30Lake:
    """What every arm on a window shares: the reader, the features, the screens, the regimes."""

    reader: _L1Reader
    features: Sma30Features
    sessions: tuple[date, ...]
    calendar: tuple[date, ...]
    regimes: dict[int, _RegimeSource]
    universe_filters: dict[Decimal, _InvestableUniverse]

    def close(self) -> None:
        self.features.close()
        self.reader.close()


def open_sma30_lake(
    *, start: date, end: date, floors: Sequence[Decimal], data_root: Path | None
) -> Sma30Lake:
    """Open the shared lake for ``[start, end]`` and strike the features once."""
    reader = _L1Reader(data_root=data_root)
    medians = reader.median_turnover_over

    def cached_medians(first: date, last: date) -> dict[str, Decimal]:
        if (first, last) not in _MEDIANS:
            _MEDIANS[(first, last)] = medians(first, last)
        return _MEDIANS[(first, last)]

    reader.median_turnover_over = cached_medians  # type: ignore[method-assign,assignment]
    features = Sma30Features(data_root=data_root, split_factors=current_signal_split_factors())
    sessions = _reserve_fill_headroom(reader.trading_sessions(start, end), reader.all_sessions())
    features.load(sessions[0], sessions[-1])
    regimes = {
        n: _RegimeSource.published(through=sessions[-1], ma_days=n, data_root=data_root)
        for n in sorted({a.params.regime_ma_days or 200 for a in ARMS})
    }
    filters = {
        floor: _InvestableUniverse(
            reader,
            UniverseParameters.for_universe(UNIVERSE_TURNOVER_FLOOR, median_turnover_floor=floor),
            data_root=data_root,
        )
        for floor in floors
    }
    return Sma30Lake(
        reader, features, tuple(sessions), tuple(reader.all_sessions()), regimes, filters
    )


def run_sma30(
    *,
    start: date,
    end: date,
    parameters: Sma30Parameters,
    floor: Decimal,
    lake: Sma30Lake,
    data_root: Path | None,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
) -> BacktestResult:
    """Replay one arm over ``[start, end]`` on one floor — wired as ``run_swing_composite``."""
    universe = lake.universe_filters[floor]
    spec = backtest_spec(
        "sma30_stock",
        start=start,
        end=end,
        parameters=parameters,
        opening_cash=opening_cash,
        adjusted=True,
        universe=universe.params,
    )
    reader = lake.reader
    sessions = list(lake.sessions)
    calendar = list(lake.calendar)
    first_session, terminal = sessions[0], sessions[-1]
    book = PortfolioBook()
    marks = _HoldingMarks(reader, _held_by(book))
    data = _Sma30Data(
        reader, lake.features, lake.regimes[parameters.regime_ma_days or 200], universe
    )
    clock = FrozenClock(first_session)
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
        market=_L1Market(reader, calendar, held=_held_by(book)),
        opening_cash=opening_cash,
    )
    book.deposit(first_session, opening_cash)
    nav_path: list[tuple[date, Decimal]] = []

    def sample_nav(session: date) -> None:
        nav_path.append(
            (session, book.net_asset_value(marks.nav_prices(session, book.positions())))
        )

    broker = _AccountingBroker(sim, book, nav_sink=sample_nav)
    rails = ratified_backtest_rail_policy()
    policy = Sma30StockPolicy(data, parameters, order_caps=rails.rails)
    engine = ReplayEngine(
        policy=policy, broker=broker, clock=clock, sessions=sessions, rails=RailGate(rails, marks)
    )
    started = time.perf_counter()
    result = engine.run()
    runtime = time.perf_counter() - started
    terminal_prices = _terminal_prices(reader, book, sessions)
    resolved = _resolve_benchmark(
        reader,
        sessions[::21],
        first_session,
        terminal,
        slug=_BENCHMARK_TRI_SLUG,
        data_root=data_root,
    )
    comparison = book.compare_to_benchmarks(
        terminal, terminal_prices, benchmark=resolved.series, theme=resolved.series
    )
    return _finish_backtest(
        BacktestResult(
            policy="sma30_stock",
            adjusted=True,
            start=first_session,
            terminal=terminal,
            sessions=len(sessions),
            rebalances=len(sessions),
            parameters=MomentumParameters(
                top_n=parameters.max_names,
                buy_budget_fraction=parameters.buy_budget_fraction,
                sleeve=parameters.sleeve,
            ),
            opening_cash=opening_cash,
            runtime_seconds=runtime,
            result=result,
            book=result.book,
            final_nav=book.net_asset_value(terminal_prices),
            total_charges=broker.total_charges,
            realized_pnl=book.realized_pnl,
            unrealized_pnl=book.unrealized_pnl(terminal_prices),
            comparison=comparison,
            decision_counts=_decision_counts(result.journal),
            universe_filtered=True,
            mean_universe=_ZERO,
            benchmark_source=resolved.source,
            benchmark_index_name=resolved.series.index_name,
            benchmark_method=resolved.series.method,
            max_drawdown=_max_drawdown([nav for _, nav in nav_path]),
            nav_path=tuple(nav_path),
        ),
        broker=broker,
        spec=spec,
        terminal_prices=terminal_prices,
    )


# ── the campaign ─────────────────────────────────────────────────────────────────────────────────

#: The owner mandate's windows (as ``backtest.m12_rerun.WINDOWS``), plus the "today" window the
#: holdings table reads (a short window: an event-driven book turns over within weeks).
WINDOWS: Final[dict[str, tuple[date, date]]] = {
    "decade": (date(2016, 9, 1), date(2026, 8, 31)),
    "six-year": (date(2019, 7, 1), date(2026, 8, 31)),
    "wf-selection": (date(2016, 9, 1), date(2021, 8, 31)),
    "wf-verification": (date(2021, 9, 1), date(2026, 8, 31)),
}
FLOORS: Final = (LOW_FLOOR, HIGH_FLOOR)


def _unit(out_dir: str, data_root: str | None, window: str, floor: str, through: str) -> str:
    """Run every arm on one (window, floor), resuming any run already on disk."""
    out = Path(out_dir)
    root = Path(data_root) if data_root else None
    if window == "today":
        start, end = date(2026, 1, 1), date.fromisoformat(through)
    else:
        start, end = WINDOWS[window]
    floor_d = Decimal(floor)
    with (
        corporate_actions_in_force(load_store_book_actions(data_root=root), apply_to_book=True),
        accrue_cash_interest(load_repo_rate_schedule()),
        persist_run_ledgers(out),
    ):
        lake = open_sma30_lake(start=start, end=end, floors=(floor_d,), data_root=root)
        done = 0
        try:
            for arm in ARMS:
                spec = backtest_spec(
                    "sma30_stock",
                    start=start,
                    end=end,
                    parameters=arm.params,
                    opening_cash=_DEFAULT_OPENING_CASH,
                    adjusted=True,
                    universe=lake.universe_filters[floor_d].params,
                )
                if summary_path(out, run_digest(spec)).is_file():
                    continue
                began = time.perf_counter()
                run_sma30(
                    start=start,
                    end=end,
                    parameters=arm.params,
                    floor=floor_d,
                    lake=lake,
                    data_root=root,
                )
                done += 1
                _LOG.info(
                    "sma30.run_done",
                    arm=arm.label,
                    window=window,
                    floor=floor,
                    seconds=round(time.perf_counter() - began, 1),
                )
        finally:
            lake.close()
    return f"{window}@{floor}: replayed {done}"


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m backtest.sma30_run --out DIR [--workers 2] [--units decade,...]``."""
    parser = argparse.ArgumentParser(prog="python -m backtest.sma30_run")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--units", default="wf-selection,wf-verification,decade,six-year")
    parser.add_argument("--through", default="2026-10-13", help="the today window's end")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        print(f"error: workers must be 1..{MAX_WORKERS}", file=sys.stderr)
        return 2
    out = refuse_lake_location(args.out, args.data_root)
    out.mkdir(parents=True, exist_ok=True)
    units = [u.strip() for u in args.units.split(",") if u.strip()]
    reader = _L1Reader(data_root=args.data_root)
    try:
        last = reader.all_sessions()[-1]
    finally:
        reader.close()
    manifest = {
        "commit": _git_commit(),
        "lake_last_session": last.isoformat(),
        "units": units,
        "universe": UNIVERSE_TURNOVER_FLOOR,
        "arms": [a.label for a in ARMS],
        "windows": {k: [v[0].isoformat(), v[1].isoformat()] for k, v in WINDOWS.items()},
    }
    try:
        check_manifest(out, manifest)
    except CampaignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    items = [(w, str(f)) for f in FLOORS for w in units]
    root = str(args.data_root) if args.data_root else None
    if args.workers == 1:
        results = [_unit(str(out), root, w, f, args.through) for w, f in items]
    else:
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context("spawn")) as pool:
            futures = [pool.submit(_unit, str(out), root, w, f, args.through) for w, f in items]
            results = [f.result() for f in futures]
    for line in results:
        print(f"  {line}")
    (out / "arms.json").write_text(
        json.dumps(
            [{"label": a.label, "note": a.note, "parameters": repr(a.params)} for a in ARMS],
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
