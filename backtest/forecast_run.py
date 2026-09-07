"""Run the fitted-forecast daily policy over history and report it against the momentum arms (X2).

``uv run python -m backtest.forecast_run --from 2016-09-01 --to 2026-08-31 --report``

Why this is its own module rather than another branch of :mod:`backtest.run`: that file is the
shared choke point every policy's runner lands in, and it is the one place concurrent work on this
repo collides. Nothing here is duplicated from it — the L1 reader, the investable-universe screen,
the regime source, the ``SimBroker`` wiring, the accounting broker, the benchmark resolution and the
report helpers are all imported and reused, so there is exactly one implementation of the engine
stack and this module only adds the adapter and the driver the forecast policy needs.

What the adapter does, and where the point-in-time risk actually lives. A fitted model has two
distinct ways to see the future and only one of them is the usual one:

* **The features** must not reach forward. Every window frame in the bulk query below is
  ``ROWS BETWEEN n PRECEDING AND CURRENT ROW`` over ``PARTITION BY isin ORDER BY trade_date``, so a
  row dated ``t`` sees only rows dated ``t`` or earlier, and each record is stamped
  ``knowable_date = t`` for ``ctx.pit.admit`` to re-check.
* **The training targets** are forward returns by definition — that is what the model predicts — so
  the query *does* compute ``lead(px, horizon)``. The discipline is entirely in *when* a pair may be
  used: a pair is held back until the session its target window closes, and only then added to the
  accumulator. The model that scores session ``s`` has therefore been fitted on pairs whose targets
  were all realised on or before ``s``. This is the single most important property in the module and
  ``tests/unit/test_forecast_run.py`` pins it directly.

One streaming pass, constant memory. The feature query is issued once, ordered by ``trade_date``,
and consumed as the replay walks forward: each session's cross-section is taken off the cursor, its
name-level features are rank-transformed together (which is why they must be read a whole session at
a time), and each row's own ``(target_date, target_return)`` is parked until that date arrives. So
the walk holds one session's cross-section plus ``horizon`` sessions of pending pairs, never the
2.2 M-row table.

The signal price follows the same "raw base, L2 adjusted overlaid where it exists" rule the rest of
the M9.2 stack uses, expressed as one LEFT JOIN. Adjusted closes are not optional here: a forward
return computed on raw closes would score a 2:1 split as a realised -50 %, which would not merely
add noise to the training set but teach the model that splits predict collapses. ``price`` stays the
raw close — the signal is adjusted, the fill and the sizing are not (invariant #3).

Delivery coverage is the one feature with a stated hole: `deliv_pct` is populated on ~65 % of 2016
prints rising to ~86 % by 2026 (`ops/gates/delivery-rebuild-2026-09-07.md`), and the unresolved
names skew to those later renamed or delisted. A name with no delivery in the window scores 0 — the
neutral middle — rather than being dropped, so the candidate set does not silently change with the
coverage; the report states how often that happened.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from backtest.accounting import PortfolioBook
from backtest.forecast import (
    FEATURE_NAMES,
    LOOKBACK_1M,
    LOOKBACK_6M,
    LOOKBACK_12M,
    LOOKBACK_52W_HIGH,
    VOL_WINDOW,
    Features,
    ForecastAccumulator,
    ForecastModel,
    rank_scale,
)
from backtest.policies.forecast_daily import (
    ForecastDailyParameters,
    ForecastDailyPolicy,
    ForecastRecord,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters
from backtest.policies.naive_momentum import MomentumParameters
from backtest.replay import ReplayEngine
from backtest.run import (
    _ACCOUNT_STATE,
    _BENCHMARK_BASKET,
    _BENCHMARK_TRI_SLUG,
    _DEFAULT_OPENING_CASH,
    _REGIME_MA_DAYS,
    BacktestError,
    BacktestResult,
    UniverseParameters,
    _AccountingBroker,
    _decision_counts,
    _InvestableUniverse,
    _L1Market,
    _L1Reader,
    _max_drawdown,
    _pct,
    _RegimeSource,
    _reserve_fill_headroom,
    _resolve_benchmark,
    _rupees,
    _terminal_prices,
    _trades,
    run_momentum_v2,
)
from dataplatform.clock import FrozenClock
from dataplatform.logging import get_logger
from dataplatform.query.pit import Dataset
from dataplatform.store.l2 import open_connection, register_adjusted_view, register_raw_view
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker

_LOG = get_logger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
#: Sessions of history a name needs before it has every feature (the 252-session lag, plus one).
_MIN_HISTORY: Final = LOOKBACK_12M + 1
#: Sessions of delivery the ratio's numerator and denominator are struck over.
_DELIV_FAST: Final = 5
_DELIV_SLOW: Final = VOL_WINDOW
#: Sessions the turnover ratio's denominator averages over.
_TURNOVER_WINDOW: Final = VOL_WINDOW
#: Calendar days of history the base scan keeps before the window's first session. The longest
#: look-back is 252 *sessions*; 600 days covers that plus every holiday and a wide margin, and
#: bounding the scan is what keeps a two-year run from paying for the whole decade's window
#: functions. `row_number() >= _MIN_HISTORY` still means "252 prior sessions on or before this row",
#: because a name has to have traded through that history to accumulate the rows.
_HISTORY_DAYS: Final = 600
#: Quantum for a Decimal carried from a float statistic — eight places, far past any decision.
_Q8: Final = Decimal("0.00000001")
_REPORT_PATH: Final = Path("ops/gates/X2-forecast-daily-report.md")


# ── the bulk feature pass ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Row:
    """One (isin, session) row off the feature cursor: the raw features, the price, the target."""

    session: date
    isin: str
    price: Decimal
    mom_12_1: float | None
    mom_1: float | None
    mom_6: float | None
    high_prox: float | None
    vol_63: float | None
    deliv_ratio: float | None
    turnover_ratio: float | None
    target_date: date | None
    target_return: float | None


class _FeatureCursor:
    """One windowed query over the lake, streamed in session order.

    Issued once and consumed forward, so the walk never holds the whole table. `take` returns the
    rows for exactly one session and asserts the requests are monotonic — a replay walks forward,
    and a backwards request would silently return nothing rather than re-reading, which is the kind
    of quiet wrong answer this class exists to make impossible.
    """

    _CHUNK: Final = 50_000

    def __init__(
        self, *, horizon: int, data_root: Path | None, start: date, end: date, adjusted: bool = True
    ) -> None:
        self._con = open_connection()
        register_raw_view(self._con, view="l1_fc_raw", data_root=data_root)
        if adjusted:
            register_adjusted_view(self._con, view="l2_fc_adj", data_root=data_root)
        self._buffer: list[tuple[Any, ...]] = []
        self._offset = 0
        self._exhausted = False
        self._last_taken: date | None = None
        scan_from = start - timedelta(days=_HISTORY_DAYS)
        started = time.perf_counter()
        self._con.execute(self._sql(horizon, adjusted=adjusted), [scan_from, end, start, end])
        _LOG.info(
            "forecast.features_queried",
            scan_from=scan_from.isoformat(),
            start=start.isoformat(),
            end=end.isoformat(),
            seconds=round(time.perf_counter() - started, 1),
            adjusted=adjusted,
        )

    @staticmethod
    def _sql(horizon: int, *, adjusted: bool) -> str:
        px = "COALESCE(a.adj_close, r.close)" if adjusted else "r.close"
        join = (
            "LEFT JOIN l2_fc_adj a ON a.isin = r.isin AND a.trade_date = r.trade_date "
            "AND a.exchange = 'NSE'"
            if adjusted
            else ""
        )
        w = "PARTITION BY isin ORDER BY trade_date"
        return f"""
        WITH base AS (
            SELECT r.isin, r.trade_date,
                   CAST({px} AS DOUBLE) AS px,
                   CAST(r.close AS DOUBLE) AS raw_close,
                   CAST(r.deliv_pct AS DOUBLE) AS dpct,
                   CAST(r.total_traded_value AS DOUBLE) AS ttv
            FROM l1_fc_raw r {join}
            WHERE r.exchange = 'NSE' AND r.series = 'EQ' AND r.close > 0
              AND r.trade_date BETWEEN ? AND ?
        ),
        ret AS (
            SELECT *, ln(px / NULLIF(lag(px, 1) OVER ({w}), 0)) AS lr,
                   row_number() OVER ({w}) AS n
            FROM base
        ),
        feat AS (
            SELECT isin, trade_date, raw_close, n,
                lag(px, {LOOKBACK_1M}) OVER ({w})
                    / NULLIF(lag(px, {LOOKBACK_12M}) OVER ({w}), 0) - 1 AS mom_12_1,
                px / NULLIF(lag(px, {LOOKBACK_1M}) OVER ({w}), 0) - 1 AS mom_1,
                px / NULLIF(lag(px, {LOOKBACK_6M}) OVER ({w}), 0) - 1 AS mom_6,
                px / NULLIF(max(px) OVER ({w}
                    ROWS BETWEEN {LOOKBACK_52W_HIGH - 1} PRECEDING AND CURRENT ROW), 0)
                    AS high_prox,
                stddev_samp(lr) OVER ({w}
                    ROWS BETWEEN {VOL_WINDOW - 1} PRECEDING AND CURRENT ROW) AS vol_63,
                avg(dpct) OVER ({w} ROWS BETWEEN {_DELIV_FAST - 1} PRECEDING AND CURRENT ROW)
                    / NULLIF(avg(dpct) OVER ({w}
                    ROWS BETWEEN {_DELIV_SLOW - 1} PRECEDING AND CURRENT ROW), 0) AS deliv_ratio,
                ttv / NULLIF(avg(ttv) OVER ({w}
                    ROWS BETWEEN {_TURNOVER_WINDOW - 1} PRECEDING AND CURRENT ROW), 0)
                    AS turnover_ratio,
                -- The forward return the model is fitted on, and the date it becomes knowable.
                -- Forward by definition; the maturity gate in _L1ForecastData is what keeps it PIT.
                lead(trade_date, {horizon}) OVER ({w}) AS target_date,
                lead(px, {horizon}) OVER ({w}) / NULLIF(px, 0) - 1 AS target_return
            FROM ret
        )
        SELECT trade_date, isin, raw_close, mom_12_1, mom_1, mom_6, high_prox, vol_63,
               deliv_ratio, turnover_ratio, target_date, target_return
        FROM feat
        WHERE trade_date BETWEEN ? AND ?
          AND n >= {_MIN_HISTORY}
          AND raw_close > 0
        ORDER BY trade_date, isin
        """

    def _fill(self) -> None:
        if self._exhausted or self._offset < len(self._buffer):
            return
        chunk = self._con.fetchmany(self._CHUNK)
        self._buffer = list(chunk)
        self._offset = 0
        if not self._buffer:
            self._exhausted = True

    def take(self, session: date) -> list[_Row]:
        """Every row dated `session`, consuming them off the cursor. Requests must move forward."""
        if self._last_taken is not None and session < self._last_taken:
            raise BacktestError(
                f"the feature cursor is streamed in session order and cannot go back: asked for "
                f"{session.isoformat()} after {self._last_taken.isoformat()}"
            )
        self._last_taken = session
        out: list[_Row] = []
        while True:
            self._fill()
            if self._offset >= len(self._buffer):
                return out
            row = self._buffer[self._offset]
            row_date = row[0]
            if row_date < session:
                self._offset += 1  # a session the replay is not walking (headroom, or a gap)
                continue
            if row_date > session:
                return out
            self._offset += 1
            out.append(
                _Row(
                    session=row_date,
                    isin=str(row[1]),
                    price=Decimal(str(row[2])),
                    mom_12_1=row[3],
                    mom_1=row[4],
                    mom_6=row[5],
                    high_prox=row[6],
                    vol_63=row[7],
                    deliv_ratio=row[8],
                    turnover_ratio=row[9],
                    target_date=row[10],
                    target_return=row[11],
                )
            )

    def close(self) -> None:
        self._con.close()


# ── the policy's data source ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _ForecastStats:
    """What the run learned about its own model, for the report."""

    sessions_with_model: int = 0
    sessions_without_model: int = 0
    pairs_fitted: int = 0
    delivery_absent: int = 0
    candidates: int = 0
    scored_sessions: int = 0
    final_model: ForecastModel | None = None

    @property
    def mean_candidates(self) -> Decimal:
        if not self.scored_sessions:
            return _ZERO
        return (Decimal(self.candidates) / Decimal(self.scored_sessions)).quantize(Decimal("0.1"))


class _L1ForecastData:
    """The daily policy's :class:`ForecastSignalData` — features, the expanding fit, the scoring.

    Owns the accumulator and refits once per session, after adding the pairs that matured on it and
    before scoring anything. That order is the point-in-time rule made operational: the model that
    scores ``s`` has seen every target realised on or before ``s`` and none realised after it.
    """

    def __init__(
        self,
        reader: _L1Reader,
        cursor: _FeatureCursor,
        regime: _RegimeSource,
        *,
        horizon: int,
        universe_filter: _InvestableUniverse | None,
    ) -> None:
        self._reader = reader
        self._cursor = cursor
        self._regime = regime
        self._horizon = horizon
        self._universe = universe_filter
        self._accumulator = ForecastAccumulator()
        self._model: ForecastModel | None = None
        #: Ranked features per session, held until their targets mature, then evicted.
        self._features: dict[date, dict[str, Features]] = {}
        #: Pairs waiting for their target date: target_date -> [(feature_date, isin, target)].
        self._pending: dict[date, list[tuple[date, str, float]]] = {}
        self._scored: dict[date, tuple[ForecastRecord, ...]] = {}
        self._advanced: date | None = None
        #: The investable set, screened once a month and carried forward. See `_admit`.
        self._admitted: tuple[tuple[int, int], frozenset[str]] | None = None
        self.stats = _ForecastStats()

    # ── the session walk ─────────────────────────────────────────────────────────────────────────

    def _advance(self, session: date) -> None:
        """Take this session's cross-section, mature what is due, refit, and score. Idempotent."""
        if self._advanced == session:
            return
        rows = self._cursor.take(session)
        self._mature(session)
        self._refit()
        self._score(session, rows)
        self._advanced = session

    def _mature(self, session: date) -> None:
        """Add every pair whose target window closed on or before `session`, then evict its cache.

        Dates at or before the session only. A pair is added exactly once, in date order and then
        ISIN order within a date, so the accumulated sums — and therefore the coefficients — are
        reproducible run to run and machine to machine.
        """
        due = sorted(day for day in self._pending if day <= session)
        for day in due:
            for feature_date, isin, target in sorted(self._pending.pop(day), key=lambda p: p[1]):
                cached = self._features.get(feature_date, {}).get(isin)
                if cached is None:
                    continue  # the name was not in that session's scored cross-section
                self._accumulator.add(cached, target)
        # Evict feature caches no pending pair can still reference.
        oldest_needed = min(
            (feature_date for pairs in self._pending.values() for feature_date, _, _ in pairs),
            default=None,
        )
        if oldest_needed is not None:
            for day in [d for d in self._features if d < oldest_needed]:
                del self._features[day]

    def _refit(self) -> None:
        model = self._accumulator.fit(horizon=self._horizon)
        if model is not None:
            self._model = model
            self.stats.pairs_fitted = model.observations
            self.stats.final_model = model

    def _score(self, session: date, rows: Sequence[_Row]) -> None:
        """Rank this session's cross-section, cache the features, and predict where a model exists.

        The ranks are struck over the *investable* cross-section, so the features a pair contributes
        to the fit are the same ones the policy was scored on — a rank against a different universe
        would be a different number.
        """
        if not rows:
            self._scored[session] = ()
            return
        admitted = self._admit(session, rows)
        if admitted is not None:
            rows = [row for row in rows if row.isin in admitted]
        if not rows:
            self._scored[session] = ()
            return

        market = self._market_state(session)
        columns = {
            name: rank_scale([getattr(row, name) for row in rows])
            for name in FEATURE_NAMES
            if name not in ("earnings_yield", "market_state")
        }
        self.stats.delivery_absent += sum(1 for row in rows if row.deliv_ratio is None)

        features: dict[str, Features] = {}
        records: list[ForecastRecord] = []
        for index, row in enumerate(rows):
            vector = Features(
                mom_12_1=columns["mom_12_1"][index],
                mom_1=columns["mom_1"][index],
                mom_6=columns["mom_6"][index],
                high_prox=columns["high_prox"][index],
                vol_63=columns["vol_63"][index],
                deliv_ratio=columns["deliv_ratio"][index],
                turnover_ratio=columns["turnover_ratio"][index],
                # No fundamental in this arm: the neutral middle, which the fit then reports as a
                # dropped constant feature rather than pretending to a view it does not have.
                earnings_yield=0.0,
                market_state=market,
            )
            features[row.isin] = vector
            if row.target_date is not None and row.target_return is not None:
                self._pending.setdefault(row.target_date, []).append(
                    (session, row.isin, row.target_return)
                )
            if self._model is not None:
                records.append(
                    ForecastRecord(
                        isin=row.isin,
                        expected_return=Decimal(str(self._model.predict(vector))).quantize(_Q8),
                        price=row.price,
                        knowable_date=session,
                    )
                )
        self._features[session] = features
        self._scored[session] = tuple(records)
        if self._model is None:
            self.stats.sessions_without_model += 1
        else:
            self.stats.sessions_with_model += 1
            self.stats.candidates += len(records)
            self.stats.scored_sessions += 1

    def _admit(self, session: date, rows: Sequence[_Row]) -> frozenset[str] | None:
        """The investable set for this session — screened monthly, then carried forward.

        The M9.3 screen measures a median daily turnover over a trailing year, which is one
        aggregate over the whole L1 relation per distinct window. A monthly policy pays that 120
        times over a decade; a *daily* one would pay it 2,470 times, and it is the single most
        expensive thing in the run by a wide margin.

        So the screen is struck on the first session of each calendar month and held for the rest of
        it. That is point-in-time — the screen reads only sessions on or before the date it is
        struck — and conservative in the one direction it can be: a name that becomes liquid enough
        mid-month waits until the next month to be admitted, so the universe is never widened by a
        fact the screen has not yet seen. Liquidity is a slow-moving property and the floor is a
        round ₹1 crore, so the difference from screening daily is immaterial against the cost.
        """
        if self._universe is None:
            return None
        key = (session.year, session.month)
        if self._admitted is None or self._admitted[0] != key:
            admitted = frozenset(
                self._universe.constrain(session, frozenset(row.isin for row in rows))
            )
            self._admitted = (key, admitted)
            _LOG.info(
                "forecast.universe_screened",
                session=session.isoformat(),
                candidates=len(rows),
                admitted=len(admitted),
            )
        return self._admitted[1]

    def _market_state(self, session: date) -> float:
        """`index level / its moving average - 1` — the one feature that is a level, not a rank."""
        reading = self._regime.reading(session)
        if reading.moving_average <= _ZERO:
            return 0.0  # no average yet: the market term carries nothing rather than a guess
        return float(reading.index_level / reading.moving_average - _ONE)

    # ── the ForecastSignalData surface ───────────────────────────────────────────────────────────

    def signal(self, as_of: date) -> Dataset[ForecastRecord]:
        self._advance(as_of)
        return Dataset.declaring(
            f"forecast@{as_of.isoformat()}",
            self._scored.get(as_of, ()),
            knowable_date=lambda record: record.knowable_date,
        )

    def marks(self, as_of: date) -> Mapping[str, Decimal]:
        return self._reader.closes_on(as_of)


# ── the driver ───────────────────────────────────────────────────────────────────────────────────


def run_forecast_daily(
    *,
    start: date,
    end: date,
    parameters: ForecastDailyParameters | None = None,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    universe: UniverseParameters | None = None,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
    adjusted: bool = True,
) -> tuple[BacktestResult, _ForecastStats]:
    """Replay the daily forecast policy over ``[start, end]``; return the metrics and the model.

    Wires the identical stack every other arm uses — the L1 reader, the M9.3 investable screen, the
    one shared cost model behind ``SimBroker``, M4.6 accounting mirrored off the fills, journaling
    through the replay engine — and additionally samples the NAV path each session so the run
    reports a peak-to-trough drawdown.
    """
    params = parameters if parameters is not None else ForecastDailyParameters()
    uni = universe if universe is not None else UniverseParameters()
    reader = _L1Reader(data_root=data_root)
    cursor: _FeatureCursor | None = None
    try:
        sessions = reader.trading_sessions(start, end)
        if not sessions:
            raise BacktestError(f"no trading sessions in [{start.isoformat()}, {end.isoformat()}]")
        calendar = reader.all_sessions()
        sessions = _reserve_fill_headroom(sessions, calendar)
        first_session, terminal = sessions[0], sessions[-1]

        regime = _RegimeSource(
            reader,
            calendar,
            first_session=first_session,
            size=_BENCHMARK_BASKET,
            ma_days=_REGIME_MA_DAYS,
        )
        cursor = _FeatureCursor(
            horizon=params.horizon,
            data_root=data_root,
            start=first_session,
            end=terminal,
            adjusted=adjusted,
        )
        data = _L1ForecastData(
            reader,
            cursor,
            regime,
            horizon=params.horizon,
            universe_filter=_InvestableUniverse(reader, uni, data_root=data_root),
        )

        clock = FrozenClock(first_session)
        sim = SimBroker(
            clock=clock,
            cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
            market=_L1Market(reader, calendar),
            opening_cash=opening_cash,
        )
        book = PortfolioBook()
        book.deposit(first_session, opening_cash)

        last_close: dict[str, Decimal] = {}
        nav_path: list[Decimal] = []

        def sample_nav(session: date) -> None:
            last_close.update(reader.closes_on(session))
            if any(position.isin not in last_close for position in book.positions()):
                return
            nav_path.append(book.net_asset_value(last_close))

        broker = _AccountingBroker(sim, book, nav_sink=sample_nav)
        engine = ReplayEngine(
            policy=ForecastDailyPolicy(data, params), broker=broker, clock=clock, sessions=sessions
        )
        started = time.perf_counter()
        result = engine.run()
        runtime = time.perf_counter() - started

        terminal_prices = _terminal_prices(reader, book, sessions)
        resolved = _resolve_benchmark(
            reader, sessions, first_session, terminal, slug=benchmark_slug, data_root=data_root
        )
        benchmark = resolved.series
        comparison = book.compare_to_benchmarks(
            terminal, terminal_prices, benchmark=benchmark, theme=benchmark
        )
        run = BacktestResult(
            policy="forecast_daily",
            adjusted=adjusted,
            start=first_session,
            terminal=terminal,
            sessions=len(sessions),
            rebalances=data.stats.sessions_with_model,
            parameters=MomentumParameters(top_n=params.top_n),
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
            mean_universe=data.stats.mean_candidates,
            benchmark_source=resolved.source,
            benchmark_index_name=benchmark.index_name,
            benchmark_method=benchmark.method,
            max_drawdown=_max_drawdown(nav_path),
        )
        return run, data.stats
    finally:
        if cursor is not None:
            cursor.close()
        reader.close()


# ── the report ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Arm:
    label: str
    parameters: str
    run: BacktestResult


def run_forecast_report(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    adjusted: bool = True,
) -> str:
    """Run the forecast arm beside naive and all-on momentum on one window; render the report."""
    uni = UniverseParameters()
    forecast, stats = run_forecast_daily(
        start=start,
        end=end,
        opening_cash=opening_cash,
        data_root=data_root,
        universe=uni,
        adjusted=adjusted,
    )
    arms = [_Arm("Forecast, daily", repr(ForecastDailyParameters()), forecast)]
    for label, params in (
        ("Momentum: naive (all off)", MomentumV2Parameters(top_n=20)),
        (
            "Momentum: v2 all-on",
            MomentumV2Parameters(
                top_n=20, use_12_1=True, sell_band=30, regime_filter=True, vol_scaled=True
            ),
        ),
    ):
        arms.append(
            _Arm(
                label,
                repr(params),
                run_momentum_v2(
                    start=start,
                    end=end,
                    v2_parameters=params,
                    opening_cash=opening_cash,
                    data_root=data_root,
                    adjusted=adjusted,
                    universe=uni,
                ),
            )
        )
    return render_forecast_report(arms, stats=stats)


def render_forecast_report(arms: Sequence[_Arm], *, stats: _ForecastStats) -> str:
    """The X2 report: the daily forecast arm against the monthly momentum arms, and the model."""
    head = arms[0].run
    lines = [
        "# X2 — The fitted-forecast daily policy",
        "",
        "*Generated by `python -m backtest.forecast_run --report`. A forward-return model fitted "
        "on an expanding point-in-time window scores every session; the policy trades only what "
        "the projection justifies and a turnover budget allows. Measured against the two monthly "
        "momentum arms on the identical window, universe, cost model and benchmark, so each row "
        "differs by the cadence and the signal and nothing else.*",
        "",
        "## What is being compared",
        "",
        "- **Forecast, daily** — decides every session. Buys a name whose projected return over "
        f"{ForecastDailyParameters().horizon} sessions clears twice the ~0.3 % round trip, "
        "releases it below zero expected return, re-underwrites at 63 sessions, and holds a 25 % "
        "trailing stop. At most **2 fills a session**, stops exempt. No profit target.",
        "- **Momentum arms** — the M9.5 policy at its monthly cadence, unchanged, as the baseline "
        "the daily arm has to beat to justify the machinery.",
        "",
        "## Window",
        "",
        f"- {head.start.isoformat()} -> {head.terminal.isoformat()} ({head.sessions} sessions)",
        f"- Sessions the model could score: {stats.sessions_with_model}; sessions with no model at "
        f"all (the expanding window had not matured): {stats.sessions_without_model}",
        f"- Matured (features, forward return) pairs in the final fit: {stats.pairs_fitted:,}",
        f"- Mean scored candidates / session: {stats.mean_candidates}",
        f"- Rows with no delivery figure in their window (scored at the neutral middle): "
        f"{stats.delivery_absent:,}",
        f"- Benchmark: {_pct(head.comparison.benchmark_xirr)} ({head.benchmark_index_name})",
        "",
        "## Results",
        "",
        "| Strategy | Portfolio XIRR | Max drawdown | Turnover (fills) | Total cost | "
        "Excess vs benchmark |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in arms:
        run = arm.run
        lines.append(
            f"| {arm.label} | {_pct(run.comparison.portfolio_xirr)} | "
            f"{_pct(run.max_drawdown)} | {_trades(run)} | {_rupees(run.total_charges)} | "
            f"{_pct(run.comparison.excess_over_benchmark)} |"
        )
    lines += ["", "## The fitted model, as of the last session", ""]
    model = stats.final_model
    if model is None:
        lines.append(
            "- No model was ever fitted: the expanding window never reached the observation floor. "
            "Every figure above for the forecast arm is therefore a book that never traded."
        )
    else:
        lines += [
            f"- Fitted on **{model.observations:,}** matured pairs; in-sample R² "
            f"{model.r_squared:.4f} (an upper bound on explanatory power, **not** predictive "
            f"accuracy — it describes the past the fit saw).",
            f"- Intercept {model.intercept:+.5f} — the average {model.horizon}-session return the "
            "fit attributes to no feature at all.",
            f"- Dropped as constant: {', '.join(model.dropped) if model.dropped else 'none'}.",
            "",
            "| Feature | Coefficient | Sign expected a priori |",
            "| --- | --- | --- |",
        ]
        expected = {
            "mom_12_1": "positive (trend)",
            "mom_1": "negative (short-term reversal)",
            "mom_6": "positive (trend)",
            "high_prox": "positive",
            "vol_63": "negative (low-volatility effect)",
            "deliv_ratio": "positive (accumulation)",
            "turnover_ratio": "none stated",
            "earnings_yield": "positive",
            "market_state": "positive (a level, not a rank)",
        }
        for name, coefficient in model.described():
            lines.append(f"| `{name}` | {coefficient:+.5f} | {expected[name]} |")
    lines += [
        "",
        "## Reading it",
        "",
        "- **The comparison that matters is the daily arm against the monthly ones.** Same "
        "universe, same basket size, same cost model. If the daily arm does not beat them, the "
        "fitted forecast and the whole apparatus of deciding every session did not earn their "
        "keep, and the honest conclusion is that the monthly cadence was right.",
        "- **Do not read excess-vs-benchmark as alpha.** The benchmark is a price-return L1 proxy, "
        "not a licensed total-return index (M9.4), so it understates the market by roughly its "
        "dividend yield.",
        "- **The coefficients are the interesting output even if the returns are not.** A feature "
        "whose fitted sign contradicts the a-priori expectation above is either a real finding or "
        "a symptom, and the delivery leg in particular is fitted over a window whose coverage "
        "rises from ~65 % to ~86 % (`ops/gates/delivery-rebuild-2026-09-07.md`), skewed toward "
        "names that survived.",
        "- **The early window is weak by construction**, not by accident: the expanding fit has no "
        "model until enough targets have matured, and the sessions-without-a-model count above "
        "says how long that lasted.",
        "",
        "## PIT",
        "",
        "- Every feature window is `ROWS BETWEEN n PRECEDING AND CURRENT ROW`, so no feature "
        "reaches forward. The training targets are forward returns by definition, and the "
        "discipline is the maturity gate: a pair enters the fit only on the session its target "
        "window closes, so the model scoring session *s* has seen only targets realised on or "
        "before *s*. `tests/unit/test_forecast_run.py` pins that directly, and every read still "
        "passes `ctx.pit.admit`.",
        f"- Run digest: `{head.result.digest()}`",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the forecast arm."""
    parser = argparse.ArgumentParser(prog="python -m backtest.forecast_run", description=__doc__)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--report",
        action="store_true",
        help=f"run the momentum arms too and write the comparison report to {_REPORT_PATH}",
    )
    parser.add_argument(
        "--raw",
        dest="adjusted",
        action="store_false",
        help="source the signal and the training targets from raw L1 closes; the default is the "
        "L2 back-adjusted series, which a forward-return target needs (a split would otherwise "
        "read as a realised -50%%)",
    )
    parser.set_defaults(adjusted=True)
    parser.add_argument("--opening-cash", type=Decimal, default=_DEFAULT_OPENING_CASH)
    parser.add_argument("--data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.end < args.start:
        print(f"error: --to {args.end} is before --from {args.start}", file=sys.stderr)
        return 2

    try:
        if args.report:
            report = run_forecast_report(
                start=args.start,
                end=args.end,
                opening_cash=args.opening_cash,
                data_root=args.data_root,
                adjusted=args.adjusted,
            )
            _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            _REPORT_PATH.write_text(report, encoding="utf-8")
            print(f"  forecast report written to {_REPORT_PATH}")
            return 0
        run, stats = run_forecast_daily(
            start=args.start,
            end=args.end,
            opening_cash=args.opening_cash,
            data_root=args.data_root,
            adjusted=args.adjusted,
        )
    except BacktestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"forecast_daily {run.start} -> {run.terminal} ({run.sessions} sessions)")
    print(f"  XIRR {_pct(run.comparison.portfolio_xirr)}  max DD {_pct(run.max_drawdown)}")
    print(f"  fills {_trades(run)}  costs {_rupees(run.total_charges)}")
    print(
        f"  model: {stats.pairs_fitted:,} pairs, "
        f"{stats.sessions_without_model} sessions with no model"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
