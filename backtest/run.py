"""M4.10 — run a backtest policy end-to-end over history and report it (EXECUTION_PLAN §7, X2).

``python -m backtest.run --policy naive_momentum --from 2016-04-01 --to 2026-03-31 --report``

This is the command line that wires the real engine stack around a policy and drives it over a span
of history: the point-in-time universe and price cross-sections (D4) out of the L1 lake, the shared
Indian cost model (invariant #4) behind ``SimBroker``'s fill model, whole-share allocation (M4.7),
portfolio accounting and XIRR (M4.6) mirrored off the fills, and full per-session journaling
(invariant #9) through the replay engine (M4.8). Its purpose is engine validation: prove all of that
survives a ten-year run, report the result against a broad-market total-return benchmark with costs
included, and state the runtime — not to make money (see :mod:`backtest.policies.naive_momentum`).

M9.2 change — the momentum **signal** now reads L2 back-adjusted closes through the query layer.
The trailing-return signal that ranks the universe is the one figure splits and bonuses corrupt: a
name that split 2:1 shows a fake ~-50% twelve-month return on raw closes and the policy wrongly
drops it. So ``_L1MomentumData`` now sources its momentum *ratio* from
:meth:`QueryService.cross_section` (L2 ``prices_adjusted``, materialized by M2.5 from the M9.1
factors), which back-adjusts both endpoints into one share basis. The ratio is point-in-time safe:
a corporate action *after* the rebalance date scales numerator and denominator identically and
cancels, so no future split leaks into a past decision (invariant #7). **Execution stays raw** — the
sizing price, the fill reference bars and the terminal marks are the prices that actually traded
(``ReferenceBar`` is raw by invariant #3; the book holds real raw shares), so only *analysis* moves
to L2. Pass ``adjusted=False`` to source the signal from raw L1 too — the pre-M9.2 baseline the
adjusted-vs-raw delta report is struck against.

Data reality this build runs against: the lake here holds L1 raw NSE prices only, and
``corporate_actions`` / ``adjustment_factors`` are empty — M9.1's live ten-year CA backfill is a
bulk-fetch campaign gated on a human go (AGENTIC_CONTEXT B1). With no CA rows every factor chain is
the identity, so a materialized L2 equals L1 bar-for-bar and the adjusted signal equals the raw one
over *this* store: the adjusted-vs-raw run is byte-identical here and the delta is zero. The
de-corruption itself is proven on a controlled known-split fixture in
``tests/integration/test_backtest_adjusted.py``. The universe and listing windows are still derived
from L1's own observed trading (survivorship-safe: a name is in the universe on a date iff it traded
on or around it, and later-delisted names stay in for earlier dates), and a **broad-market TRI proxy
computed from L1** stands in for the licensed NSE NIFTY-TRI series, which is not in the store. The
proxy flows through the *same* :class:`~dataplatform.ingest.indices.TriSeries` and
:meth:`~backtest.accounting.PortfolioBook.compare_to_benchmarks` code the published series will,
so the comparison plumbing is what is proven; the published TRI slots in unchanged once ingested.

Offline and clockless: DuckDB reads local Parquet, the clock is a ``FrozenClock`` the engine drives
(B10), and no network is touched. Money is ``Decimal`` throughout; joins are on ISIN (invariant #2).
"""

from __future__ import annotations

import argparse
import sys
import time
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import BenchmarkComparison, PortfolioBook
from backtest.policies.naive_momentum import (
    MomentumParameters,
    MomentumRecord,
    NaiveMomentumPolicy,
)
from backtest.replay import BookSnapshot, ReplayEngine, ReplayResult
from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange as IdentityExchange
from dataplatform.identity.master import ListingStatus
from dataplatform.ingest.indices import TriPoint, TriSeries
from dataplatform.logging import get_logger
from dataplatform.query.pit import Dataset
from dataplatform.query.service import QueryService
from dataplatform.query.shapes import CrossSectionRequest
from dataplatform.query.universe import InMemoryListingCalendar, ListingWindow, pit_universe
from dataplatform.store.l2 import open_connection, register_raw_view
from dataplatform.store.paths import l1_partition_path
from execution.broker import (
    Exchange,
    Holding,
    LedgerEntry,
    Margins,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
)
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker

_LOG = get_logger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: A-priori defaults, stated so the "no tuning" claim (M4.10 acceptance #3) is checkable.
_DEFAULT_OPENING_CASH = Decimal("1000000")  # ₹10 lakh nominal capital
_LOOKBACK_DAYS = 365  # trailing 12-month momentum
_BENCHMARK_BASKET = 50  # broad-market TRI proxy basket size (most-liquid names at the start)
_TRI_SEED = Decimal("1000")  # the proxy index is seeded to 1000 on the first session
_ACCOUNT_STATE = "MH"  # Maharashtra — the account's state for state-wise stamp duty (a priori)
_REPORT_PATH = Path("ops/gates/M4-momentum-report.md")
_DELTA_REPORT_PATH = Path("ops/gates/M9-adjusted-backtest-report.md")


# ── L1 lake reader ────────────────────────────────────────────────────────────────────────────────


class _L1Reader:
    """Read the raw NSE equity lake (L1) the backtest needs, through one reused DuckDB connection.

    Every method is point-in-time by construction: a caller asks for a specific date's closes or the
    static listing windows, never "latest". Closes are cached per date, so the walk pays for
    each session's cross-section once. Equity only (``series = 'EQ'``) and priced (``close > 0``);
    money comes back as ``Decimal`` off the lake's ``decimal128`` columns.
    """

    _VIEW = "l1_prices_raw"
    _DATASET = "prices_raw"

    def __init__(self, *, data_root: Path | None = None) -> None:
        self._data_root = data_root
        self._con = open_connection()
        register_raw_view(self._con, view=self._VIEW, data_root=data_root)
        self._closes: dict[date, dict[str, Decimal]] = {}
        self._refbars: dict[date, dict[str, ReferenceBar]] = {}

    def _partition(self, session: date) -> str:
        """The single L1 parquet file for one session — read directly, not via a lake scan.

        A per-date query over the whole-lake view opens every partition's footer to prune; reading
        the one ``date=<session>`` file instead is the difference between a ten-year run in seconds
        and in many minutes. Returns the path as a string for ``read_parquet``; missing files (a
        date not in the store) surface as an empty result from the caller, not an error.
        """
        return str(l1_partition_path(self._DATASET, session, data_root=self._data_root))

    def close(self) -> None:
        self._con.close()

    def trading_sessions(self, start: date, end: date) -> tuple[date, ...]:
        """Every distinct NSE-equity trading session in ``[start, end]``, ascending."""
        rows = self._con.execute(
            f"SELECT DISTINCT trade_date FROM {self._VIEW} "
            f"WHERE series = 'EQ' AND trade_date BETWEEN $start AND $end ORDER BY trade_date",
            {"start": start, "end": end},
        ).fetchall()
        return tuple(row[0] for row in rows)

    def all_sessions(self) -> tuple[date, ...]:
        """Every distinct trading session in the store, ascending — the market's own calendar."""
        rows = self._con.execute(
            f"SELECT DISTINCT trade_date FROM {self._VIEW} WHERE series = 'EQ' ORDER BY trade_date"
        ).fetchall()
        return tuple(row[0] for row in rows)

    def listing_windows(self) -> tuple[ListingWindow, ...]:
        """One tradeable window per ISIN, from its first to its last observed L1 session.

        Survivorship-safe: ``listed_from`` is the first date the name printed and ``delisted_on`` is
        the session after its last print (``None`` when it still trades at the store's edge). A name
        that stopped trading in 2020 is still tradeable in a 2018 universe and correctly absent from
        a 2021 one — the property that kills survivorship bias (§4.5). Derived from L1 because no
        identity master is loaded here; the production path reads ``store_listing_calendar``.
        """
        store_end = self.all_sessions()[-1]
        rows = self._con.execute(
            f"SELECT isin, min(trade_date), max(trade_date) FROM {self._VIEW} "
            f"WHERE series = 'EQ' AND close > 0 GROUP BY isin"
        ).fetchall()
        windows: list[ListingWindow] = []
        for isin, first_seen, last_seen in rows:
            # Still trading if it printed on the last store session; else delisted the day after its
            # final print. Using the day-after keeps the name in a universe as of its last session.
            still_open = last_seen >= store_end
            windows.append(
                ListingWindow(
                    isin=str(isin),
                    listed_from=first_seen,
                    delisted_on=None if still_open else last_seen + timedelta(days=1),
                    status=ListingStatus.ACTIVE if still_open else ListingStatus.DELISTED,
                )
            )
        return tuple(sorted(windows, key=lambda window: window.isin))

    def closes_on(self, session: date) -> dict[str, Decimal]:
        """The equity close for every priced name on ``session`` (cached)."""
        cached = self._closes.get(session)
        if cached is not None:
            return cached
        path = self._partition(session)
        if not Path(path).exists():
            self._closes[session] = {}
            return {}
        rows = self._con.execute(
            "SELECT isin, close FROM read_parquet($path) WHERE series = 'EQ' AND close > 0",
            {"path": path},
        ).fetchall()
        closes = {str(isin): Decimal(close) for isin, close in rows}
        self._closes[session] = closes
        return closes

    def reference_bars_on(self, session: date) -> dict[str, ReferenceBar]:
        """The next-session fill reference (open, vwap, turnover) for every tradeable name (cached).

        ``vwap`` is turnover / traded quantity — the session's volume-weighted price — and
        ``traded_value`` is the turnover slippage scales against. Only rows with a positive
        open, a positive quantity and positive turnover qualify: a name the fill model cannot price
        or size against is simply absent, and a staged order for it is rejected by the broker.
        """
        cached = self._refbars.get(session)
        if cached is not None:
            return cached
        path = self._partition(session)
        if not Path(path).exists():
            self._refbars[session] = {}
            return {}
        rows = self._con.execute(
            "SELECT isin, open, total_traded_qty, total_traded_value FROM read_parquet($path) "
            "WHERE series = 'EQ' AND open > 0 AND total_traded_qty > 0 "
            "AND total_traded_value > 0",
            {"path": path},
        ).fetchall()
        bars: dict[str, ReferenceBar] = {}
        for isin, open_, qty, turnover in rows:
            open_price = Decimal(open_)
            turnover_d = Decimal(turnover)
            vwap = turnover_d / Decimal(qty)
            bars[str(isin)] = ReferenceBar(
                isin=str(isin),
                session=session,
                exchange=Exchange.NSE,
                open=open_price,
                vwap=vwap,
                traded_value=turnover_d,
            )
        self._refbars[session] = bars
        return bars

    def most_liquid_on(self, session: date, size: int) -> list[str]:
        """The ``size`` most-liquid equity ISINs on ``session`` by turnover (benchmark basket)."""
        path = self._partition(session)
        if not Path(path).exists():
            return []
        rows = self._con.execute(
            "SELECT isin FROM read_parquet($path) "
            "WHERE series = 'EQ' AND close > 0 AND total_traded_value > 0 "
            "ORDER BY total_traded_value DESC, isin LIMIT $n",
            {"path": path, "n": size},
        ).fetchall()
        return [str(row[0]) for row in rows]


# ── market: the SessionMarket SimBroker fills against ─────────────────────────────────────────────


class _L1Market:
    """A ``SessionMarket`` over the L1 calendar and raw bars — the fill data for a replay.

    ``next_session`` walks the market's own full calendar (which outruns the replay window, so an
    order staged on the last replayed session still has a next session to target). ``reference_bar``
    serves the raw open/vwap/turnover the fill model needs, read from L1 and cached per session.
    """

    def __init__(self, reader: _L1Reader, calendar: Sequence[date]) -> None:
        self._reader = reader
        self._calendar = list(calendar)

    def next_session(self, after: date) -> date:
        index = bisect_right(self._calendar, after)
        if index >= len(self._calendar):
            raise NoReferenceBarError(f"no trading session after {after.isoformat()}")
        return self._calendar[index]

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        bar = self._reader.reference_bars_on(session).get(isin)
        if bar is None:
            raise NoReferenceBarError(f"no reference bar for {isin} on {session.isoformat()}")
        return bar


# ── signal closes: raw L1, or L2 back-adjusted through the query layer ────────────────────────────


#: A per-session close source for the momentum *ratio*: session date -> {ISIN: close}. The raw
#: variant is ``_L1Reader.closes_on``; the adjusted variant is ``_AdjustedCloseSource`` below.
SignalCloses = Callable[[date], Mapping[str, Decimal]]


class _AdjustedCloseSource:
    """L2 back-adjusted closes for one session, read through :class:`QueryService` (M9.2).

    The seam the M4.10 backlog names ("read `QueryService.adjusted_series` / `cross_section`"): each
    call answers a session's closes with the L2 ``prices_adjusted`` bars (``raw x cumulative
    factor``), so a split or bonus inside the look-back window is expressed in one share basis and
    the momentum ratio it feeds is no longer corrupted. Only the *close* is taken — execution still
    fills on the raw reference bar (invariant #3: adjusted series are for analysis, never a fill).

    The primary map is supplied, not derived: this lake is single-exchange (NSE), so every ISIN's
    primary is NSE and the liquidity scan `cross_section` would otherwise run is skipped. The ISIN
    set for the map comes from L1's raw closes on the session — the same names L2 was materialized
    from — so a name present in L1 but not yet materialized in L2 simply has no adjusted close and
    drops out of the candidate set, exactly as the query layer reports it. Closes are cached per
    session, so the walk pays for each cross-section once.
    """

    def __init__(self, service: QueryService, reader: _L1Reader) -> None:
        self._service = service
        self._reader = reader
        self._closes: dict[date, dict[str, Decimal]] = {}

    def __call__(self, session: date) -> Mapping[str, Decimal]:
        cached = self._closes.get(session)
        if cached is not None:
            return cached
        # Single-exchange lake: pin every name's primary to NSE so cross_section skips the L1
        # liquidity scan. The ISIN universe is L1's own priced names for the session.
        primary = dict.fromkeys(self._reader.closes_on(session), IdentityExchange.NSE)
        cross = self._service.cross_section(
            CrossSectionRequest(trade_date=session, primary_by_isin=primary)
        )
        closes = {row.isin: row.adj_close for row in cross.rows}
        self._closes[session] = closes
        return closes


# ── data source: the PIT momentum signal the policy reads ─────────────────────────────────────────


class _L1MomentumData:
    """The policy's :class:`~backtest.policies.naive_momentum.MomentumData` (M4.10 + M9.2).

    Rebalances on the first trading session of each month. For each rebalance date it builds the
    candidate set: the PIT universe as of that date (from L1 listing windows), cut to names with
    both a current close and a close on the look-back reference session (the latest session on or
    before the date minus twelve months), and tags each with its trailing return. Every figure is
    knowable on the rebalance date, so the dataset admits cleanly through the point-in-time guard.
    The whole schedule is precomputed once, so the replay walk is dict lookups.

    M9.2 splits the two close reads the record needs. The **momentum ratio** is computed from
    ``signal_closes`` — L2 back-adjusted closes through the query layer
    (:class:`_AdjustedCloseSource`) — so a split in the look-back window no longer reads as a fake
    collapse. The **sizing price**
    is the raw close from L1, because the allocation buys whole shares that fill on the raw
    reference bar; mixing an adjusted price into a raw fill would mis-size the order. With
    ``signal_closes = reader.closes_on`` (the raw source) the two coincide and the result is the
    pre-M9.2 raw baseline.
    """

    def __init__(
        self,
        reader: _L1Reader,
        sessions: Sequence[date],
        *,
        signal_closes: SignalCloses | None = None,
    ) -> None:
        self._reader = reader
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._sessions = list(sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._signals: dict[date, tuple[MomentumRecord, ...]] = {}
        for rebalance_date in sorted(self._rebalance):
            self._signals[rebalance_date] = self._compute(rebalance_date)

    def is_rebalance(self, session: date) -> bool:
        return session in self._rebalance

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        records = self._signals.get(as_of, ())
        return Dataset.declaring(
            f"momentum@{as_of.isoformat()}",
            records,
            knowable_date=lambda record: record.knowable_date,
        )

    def rebalance_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._rebalance))

    def _compute(self, as_of: date) -> tuple[MomentumRecord, ...]:
        reference = self._lookback_session(as_of)
        if reference is None:
            return ()  # no twelve-month history yet — nothing to rank
        universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
        # Momentum ratio from the signal source (L2 adjusted, or raw for the baseline); the sizing
        # price from raw L1, because the order fills on the raw reference bar (invariant #3).
        signal_now = self._signal_closes(as_of)
        signal_then = self._signal_closes(reference)
        raw_now = self._reader.closes_on(as_of)
        records: list[MomentumRecord] = []
        for isin in universe:
            now = signal_now.get(isin)
            then = signal_then.get(isin)
            price = raw_now.get(isin)
            if now is None or then is None or then <= _ZERO or price is None:
                continue
            records.append(
                MomentumRecord(
                    isin=isin,
                    momentum=now / then - _ONE,
                    price=price,
                    knowable_date=as_of,
                )
            )
        return tuple(records)

    def _lookback_session(self, as_of: date) -> date | None:
        """The latest trading session on or before ``as_of`` minus the look-back window."""
        target = as_of - timedelta(days=_LOOKBACK_DAYS)
        index = bisect_right(self._sessions, target) - 1
        if index < 0:
            return None
        return self._sessions[index]


# ── accounting broker: mirror the SimBroker fills into a PortfolioBook for the report ─────────────


class _AccountingBroker:
    """Wraps ``SimBroker`` and mirrors every fill into a ``PortfolioBook`` (M4.6) for the report.

    It *is* the ``SimBroker`` for the replay — the engine drives it, the policy reads it — but on
    each ``execute_session`` it also posts the broker's fills into a ``PortfolioBook``, so
    the run ends with the real accounting book (positions with cost basis, realized P&L, external
    SIP cashflow) needed to strike XIRR and compare to the benchmark. The book is fed the same fills
    priced by the same cost model, so its cash tracks the broker's exactly. Total broker charges are
    accumulated here for the cost line of the report.
    """

    def __init__(self, sim: SimBroker, book: PortfolioBook) -> None:
        self._sim = sim
        self._book = book
        self.total_charges: Decimal = _ZERO

    @property
    def book(self) -> PortfolioBook:
        return self._book

    def execute_session(self, session: date) -> tuple[Order, ...]:
        filled = self._sim.execute_session(session)
        for order in filled:
            if order.status is OrderStatus.COMPLETE and order.fill is not None:
                self._book.record_fill(order.fill)
                self.total_charges += order.fill.cost.total
        return filled

    # ── Broker surface — delegated verbatim (invariant #5: one broker interface) ──────────────────

    def session_valid(self) -> bool:
        return self._sim.session_valid()

    def place(self, request: OrderRequest) -> Order:
        return self._sim.place(request)

    def modify(self, order_id: str, *, quantity: int) -> Order:
        return self._sim.modify(order_id, quantity=quantity)

    def cancel(self, order_id: str) -> Order:
        return self._sim.cancel(order_id)

    def positions(self) -> tuple[Position, ...]:
        return self._sim.positions()

    def holdings(self) -> tuple[Holding, ...]:
        return self._sim.holdings()

    def ledger(self) -> tuple[LedgerEntry, ...]:
        return self._sim.ledger()

    def margins(self) -> Margins:
        return self._sim.margins()


# ── benchmark: a broad-market TRI proxy computed from L1 ──────────────────────────────────────────


def _build_benchmark_tri(
    reader: _L1Reader, rebalance_dates: Sequence[date], start: date, terminal: date
) -> TriSeries:
    """A broad-market total-return index computed from L1 — the NIFTY-TRI stand-in for this lake.

    The index is an equal-weight average of price relatives over the ``_BENCHMARK_BASKET`` top
    names on ``start`` (by turnover), seeded to 1000 on ``start`` and marked on every rebalance date
    and the terminal date. With no dividend data in the lake it is a price-return proxy, stamped
    ``computed_price_plus_div`` (§4.1's fallback method) and labelled a proxy — it stands in
    for the licensed NSE NIFTY-TRI series, which is not loaded here, and flows through the identical
    ``TriSeries`` / ``compare_to_benchmarks`` machinery the published series will.
    """
    basket = _liquid_basket(reader, start, _BENCHMARK_BASKET)
    base_closes = reader.closes_on(start)
    base = {isin: base_closes[isin] for isin in basket if isin in base_closes}
    if not base:
        raise BacktestError(f"cannot build a benchmark: no basket closes on {start.isoformat()}")

    level_dates = sorted({start, *rebalance_dates, terminal})
    points: list[TriPoint] = []
    for when in level_dates:
        if when < start or when > terminal:
            continue
        closes = reader.closes_on(when)
        relatives = [closes[isin] / base[isin] for isin in base if isin in closes]
        if not relatives:
            continue
        level = _TRI_SEED * (sum(relatives, _ZERO) / Decimal(len(relatives)))
        points.append(
            TriPoint(
                index_slug="nifty-broad-proxy",
                index_name="NIFTY Broad-Market TRI (L1 proxy)",
                as_of=when,
                tri_value=level,
                price_close=level,
                method="computed_price_plus_div",
            )
        )
    return TriSeries(
        index_slug="nifty-broad-proxy",
        index_name="NIFTY Broad-Market TRI (L1 proxy)",
        method="computed_price_plus_div",
        points=tuple(points),
    )


def _liquid_basket(reader: _L1Reader, session: date, size: int) -> list[str]:
    """The ``size`` most-liquid equity ISINs on ``session``, by turnover — a broad-market basket."""
    return reader.most_liquid_on(session, size)


# ── the run ───────────────────────────────────────────────────────────────────────────────────────


class BacktestError(Exception):
    """A backtest could not be set up or run. Fails loud (CLAUDE.md), never a silent skip."""


def _reserve_fill_headroom(
    sessions: tuple[date, ...], calendar: Sequence[date]
) -> tuple[date, ...]:
    """Trim the replay window so an order staged on its last session still has a bar to fill on.

    Execution is T+1: an order decided on session D is staged to fill on the *next* session, so the
    market calendar must extend one session beyond the last *replayed* one (the invariant
    ``_L1Market`` documents). When the requested window reaches the last session on disk that does
    not hold, and a rebalance landing on that edge is stranded with no next bar — it raises
    ``NoReferenceBarError`` from inside ``SimBroker.place`` mid-run. Reserving the final on-disk
    session as fill headroom keeps the run inside data that exists instead of crashing: the reserved
    session is only ever a fill target, never itself replayed, so the terminal valuation is struck
    on the session before it. A window with no session left to replay once the edge is reserved
    cannot run at all and is refused loudly.
    """
    if not sessions or not calendar or sessions[-1] < calendar[-1]:
        return sessions
    reserved = sessions[-1]
    trimmed = sessions[:-1]
    if not trimmed:
        raise BacktestError(
            f"the replay window ends on the last session on disk ({reserved.isoformat()}) with no "
            "earlier session to replay; T+1 execution needs one session of fill headroom after the "
            "window, so end the backtest before the last available session"
        )
    _LOG.info(
        "backtest.reserved_fill_headroom",
        reserved_session=reserved.isoformat(),
        terminal=trimmed[-1].isoformat(),
        reason="T+1 execution needs a session after the last replayed one to fill against",
    )
    return trimmed


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything the report needs from one run — engine output plus the derived metrics."""

    policy: str
    adjusted: bool
    start: date
    terminal: date
    sessions: int
    rebalances: int
    parameters: MomentumParameters
    opening_cash: Decimal
    runtime_seconds: float
    result: ReplayResult
    book: BookSnapshot
    final_nav: Decimal
    total_charges: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    comparison: BenchmarkComparison
    decision_counts: Mapping[str, int]

    @property
    def held_names(self) -> int:
        return len(self.book.holdings)


def run_naive_momentum(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    parameters: MomentumParameters | None = None,
    data_root: Path | None = None,
    adjusted: bool = True,
) -> BacktestResult:
    """Run the naive momentum policy over ``[start, end]`` and return the result + report metrics.

    Wires the real stack — L1 data, the shared cost model behind ``SimBroker``, the M4.7 allocator
    inside the policy, the M4.6 accounting mirrored off the fills, and full journaling through the
    replay engine — advances a ``FrozenClock`` session by session, and derives the terminal
    valuation, cost total and benchmark comparison. Raises ``BacktestError`` if the window holds no
    tradeable sessions.

    ``adjusted`` (M9.2) selects the momentum signal's close source: ``True`` reads L2 back-adjusted
    closes through :class:`QueryService` (so splits/bonuses stop corrupting the ranking), ``False``
    reads raw L1 closes — the pre-M9.2 baseline the delta report is struck against. Execution,
    marks and the benchmark are raw in both cases (invariant #3). An adjusted run assumes L2 has
    been materialized (M2.5) for the names in the window; a name with no L2 bar has no adjusted
    close and drops out of the candidate set, as the query layer reports it.
    """
    params = parameters if parameters is not None else MomentumParameters()
    reader = _L1Reader(data_root=data_root)
    service = QueryService(data_root=data_root) if adjusted else None
    try:
        sessions = reader.trading_sessions(start, end)
        if not sessions:
            raise BacktestError(f"no trading sessions in [{start.isoformat()}, {end.isoformat()}]")
        calendar = reader.all_sessions()
        # T+1 execution needs a session after the last replayed one to fill against; reserve it when
        # the window reaches the last session on disk, rather than crashing mid-run on a final-bar
        # rebalance (NoReferenceBarError).
        sessions = _reserve_fill_headroom(sessions, calendar)
        first_session, terminal = sessions[0], sessions[-1]

        signal_closes = _AdjustedCloseSource(service, reader) if service is not None else None
        data = _L1MomentumData(reader, sessions, signal_closes=signal_closes)
        clock = FrozenClock(first_session)
        sim = SimBroker(
            clock=clock,
            # Maharashtra: the account's registered state, needed for state-wise stamp duty. Home
            # state of the exchanges; an a-priori modelling choice, not a tuned parameter.
            cost_model=CostModel(load_rate_card(), account_state=_ACCOUNT_STATE),
            market=_L1Market(reader, calendar),
            opening_cash=opening_cash,
        )
        book = PortfolioBook()
        book.deposit(first_session, opening_cash)  # the one external cashflow: the opening capital
        broker = _AccountingBroker(sim, book)
        policy = NaiveMomentumPolicy(data, params)

        engine = ReplayEngine(policy=policy, broker=broker, clock=clock, sessions=sessions)
        started = time.perf_counter()
        result = engine.run()
        runtime = time.perf_counter() - started

        terminal_prices = _terminal_prices(reader, book, sessions)
        benchmark = _build_benchmark_tri(reader, data.rebalance_dates(), first_session, terminal)
        comparison = book.compare_to_benchmarks(
            terminal, terminal_prices, benchmark=benchmark, theme=benchmark
        )
        return BacktestResult(
            policy="naive_momentum",
            adjusted=adjusted,
            start=first_session,
            terminal=terminal,
            sessions=len(sessions),
            rebalances=len(data.rebalance_dates()),
            parameters=params,
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
        )
    finally:
        if service is not None:
            service.close()
        reader.close()


def _terminal_prices(
    reader: _L1Reader, book: PortfolioBook, sessions: Sequence[date]
) -> dict[str, Decimal]:
    """The mark price for every held ISIN — its last close on or before the terminal session.

    Walks the run's own trading calendar backwards from the terminal session, taking each held
    name's most recent close (a PIT step price — a name that did not print on the terminal date is
    marked at its last real print, never guessed or written to zero). Reuses the per-date close
    cache, so it costs at most a few extra partition reads.
    """
    wanted = {position.isin for position in book.positions()}
    prices: dict[str, Decimal] = {}
    for session in reversed(sessions):
        if not wanted:
            break
        closes = reader.closes_on(session)
        for isin in tuple(wanted):
            price = closes.get(isin)
            if price is not None:
                prices[isin] = price
                wanted.discard(isin)
    if wanted:
        raise BacktestError(
            f"no terminal price for held ISINs {sorted(wanted)} anywhere in the run window"
        )
    return prices


def _decision_counts(journal: Sequence[JournalEntry]) -> dict[str, int]:
    """How many of each decision the journal holds — the shape of a full-history journal."""
    counts: dict[str, int] = {decision.value: 0 for decision in Decision}
    for entry in journal:
        counts[entry.decision.value] += 1
    return counts


def _first_session_of_each_month(sessions: Sequence[date]) -> list[date]:
    """The first trading session of every calendar month present in ``sessions`` (ascending)."""
    seen: set[tuple[int, int]] = set()
    firsts: list[date] = []
    for session in sorted(sessions):
        key = (session.year, session.month)
        if key not in seen:
            seen.add(key)
            firsts.append(session)
    return firsts


# ── report ────────────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    """Render an XIRR ratio as a percentage to two places (0.1234 → '12.34%')."""
    return f"{value * Decimal('100'):.2f}%"


def _rupees(value: Decimal) -> str:
    """Render a rupee amount to two places with a ₹ sign."""
    return f"₹{value:,.2f}"


def render_report(run: BacktestResult) -> str:
    """The M4.10 markdown report — parameters (a priori), returns vs benchmark, costs, runtime."""
    p = run.parameters
    c = run.comparison
    counts = run.decision_counts
    lines = [
        "# M4.10 — Naive momentum backtest (10 years)",
        "",
        "*Generated by `python -m backtest.run --policy naive_momentum --report`. "
        "This is an engine-validation run, not a strategy result — its purpose is to prove the "
        "PIT universe, the shared cost model, whole-share allocation, accounting and journaling "
        "survive a full-history replay.*",
        "",
        "## Run",
        "",
        f"- **Policy:** `{run.policy}`",
        f"- **Window:** {run.start.isoformat()} → {run.terminal.isoformat()} "
        f"({run.sessions} trading sessions, {run.rebalances} monthly rebalances)",
        f"- **Runtime:** {run.runtime_seconds:.1f} s (engine replay, wall clock)",
        (
            "- **Signal source:** L2 back-adjusted closes read through the query layer "
            "(`QueryService.cross_section`); over this CA-free lake L2 equals L1 raw. Execution, "
            "marks and benchmark are raw (invariant #3). Universe and listing windows derived from "
            "L1 observed trading."
            if run.adjusted
            else "- **Signal source:** raw L1 NSE closes (pre-M9.2 baseline; `--raw`). Universe "
            "and listing windows derived from L1 observed trading."
        ),
        "",
        "## Parameters (chosen a priori — no tuning was performed)",
        "",
        f"- **Top-N held:** {p.top_n}, equal-weighted",
        "- **Rebalance:** first trading session of each month",
        "- **Momentum signal:** trailing 12-month total return "
        f"(look-back reference = latest session <= date - {_LOOKBACK_DAYS} days)",
        f"- **Opening capital:** {_rupees(run.opening_cash)} (single deposit at the start)",
        f"- **Buy budget:** {p.buy_budget_fraction} of free cash per rebalance "
        "(mechanical execution margin, not a return knob)",
        "- **Fills:** next-session open + liquidity-scaled slippage; costs from the one shared "
        "Indian cost model at the rates in force on each trade date",
        "",
        "## Result",
        "",
        f"- **Final NAV:** {_rupees(run.final_nav)} (from {_rupees(run.opening_cash)} deposited)",
        f"- **Held names at end:** {run.held_names}",
        f"- **Realized P&L:** {_rupees(run.realized_pnl)}",
        f"- **Unrealized P&L:** {_rupees(run.unrealized_pnl)}",
        f"- **Total costs (STT, charges, GST, stamp, DP):** {_rupees(run.total_charges)}"
        " — costs are included in every fill, not deducted after the fact",
        "",
        "## Return vs benchmark (money-weighted XIRR, identical cashflows)",
        "",
        "| Series | XIRR |",
        "| --- | --- |",
        f"| Portfolio (naive momentum, **costs included**) | {_pct(c.portfolio_xirr)} |",
        f"| NIFTY-TRI (broad-market TRI proxy from L1) | {_pct(c.benchmark_xirr)} |",
        f"| **Excess over benchmark** | {_pct(c.excess_over_benchmark)} |",
        "",
        "> The benchmark is a broad-market total-return **proxy computed from L1** (equal-weight "
        f"average of the {_BENCHMARK_BASKET} most-liquid names at the start, seeded to 1000). The "
        "licensed NSE NIFTY-TRI series is not loaded here; the proxy flows through the same "
        "`TriSeries` / `compare_to_benchmarks` code the published series will, so the comparison "
        "machinery is what this validates. Do not read the excess as alpha.",
        "",
        "## Journal (invariant #9 — every session decided, including no-ops)",
        "",
        f"- **Total entries:** {len(run.result.journal)}",
        f"- **BUY:** {counts[Decision.BUY.value]}  ·  **SELL:** {counts[Decision.SELL.value]}  ·  "
        f"**HEARTBEAT:** {counts[Decision.HEARTBEAT.value]}",
        f"- **Run digest (sha256 of journal + book):** `{run.result.digest()}`",
        "- **PIT:** the run completed with every session's queries scoped to that session; no "
        "`PitError` was raised (a look-ahead read would have failed the run). The dedicated leak "
        "harness is M4.11.",
        "",
    ]
    return "\n".join(lines)


def _trades(run: BacktestResult) -> int:
    """Total fills over the run (BUY + SELL) — the turnover proxy the delta report reads."""
    return run.decision_counts[Decision.BUY.value] + run.decision_counts[Decision.SELL.value]


def render_delta_report(
    raw: BacktestResult, adjusted: BacktestResult, *, flipped: Sequence[str] = ()
) -> str:
    """The M9.2 report: the adjusted 10-year run against the raw baseline, delta by delta.

    States XIRR, turnover (fills) and cost for the raw run and the adjusted run and the delta
    between them, plus the run digests (equal here, because this store has no corporate actions so
    L2 adjusted equals L1 raw — see the run banner). ``flipped`` lists names whose twelve-month
    signal flipped across a known split between the two runs; it is empty over a CA-free store and
    the flip is instead demonstrated on the fixture in ``tests/integration/test_backtest_adjusted``.
    """
    raw_x, adj_x = raw.comparison.portfolio_xirr, adjusted.comparison.portfolio_xirr
    raw_t, adj_t = _trades(raw), _trades(adjusted)
    raw_c, adj_c = raw.total_charges, adjusted.total_charges
    identical = raw.result.digest() == adjusted.result.digest()
    lines = [
        "# M9.2 — Momentum backtest on L2 adjusted prices (10 years)",
        "",
        "*Generated by `python -m backtest.run --policy naive_momentum --delta-report`. The "
        "momentum signal now reads L2 back-adjusted closes through the query layer "
        "(`QueryService.cross_section`), so splits and bonuses stop corrupting the twelve-month "
        "ranking. This report strikes the adjusted run against the pre-M9.2 raw-signal baseline.*",
        "",
        "## Data reality",
        "",
        "This lake holds L1 raw NSE closes only; `corporate_actions` and `adjustment_factors` are "
        "empty because M9.1's live ten-year CA backfill is a bulk-fetch campaign gated on a human "
        "go (AGENTIC_CONTEXT B1). With no CA rows every factor chain is the identity, so the "
        "materialized L2 equals L1 bar-for-bar and the adjusted signal equals the raw one **over "
        "this store**. The delta below is therefore zero by construction, and the two run digests "
        "match — the plumbing (materialize L2 -> read adjusted through the query layer -> replay) "
        "is what this run proves end-to-end. The de-corruption itself — an adjusted signal that "
        "removes a split's fake ~-50% momentum — is asserted on a controlled known-split "
        "fixture in `tests/integration/test_backtest_adjusted.py`.",
        "",
        "## Window",
        "",
        f"- {adjusted.start.isoformat()} -> {adjusted.terminal.isoformat()} "
        f"({adjusted.sessions} sessions, {adjusted.rebalances} monthly rebalances)",
        f"- Adjusted run replay time: {adjusted.runtime_seconds:.1f} s; "
        f"raw run replay time: {raw.runtime_seconds:.1f} s",
        "",
        "## Adjusted vs raw",
        "",
        "| Metric | Raw signal | Adjusted signal | Delta |",
        "| --- | --- | --- | --- |",
        f"| Portfolio XIRR | {_pct(raw_x)} | {_pct(adj_x)} | {_pct(adj_x - raw_x)} |",
        f"| Turnover (BUY+SELL fills) | {raw_t} | {adj_t} | {adj_t - raw_t:+d} |",
        f"| Total costs | {_rupees(raw_c)} | {_rupees(adj_c)} | {_rupees(adj_c - raw_c)} |",
        f"| Final NAV | {_rupees(raw.final_nav)} | {_rupees(adjusted.final_nav)} | "
        f"{_rupees(adjusted.final_nav - raw.final_nav)} |",
        "",
        f"- **Run digest (raw):** `{raw.result.digest()}`",
        f"- **Run digest (adjusted):** `{adjusted.result.digest()}`",
        f"- **Digests identical:** {identical} "
        "(expected here — adjusted equals raw with no CAs in the store).",
        "",
        "## Signal flips across a known split",
        "",
        (
            "- None over this store: it holds no corporate actions, so no name's twelve-month "
            "signal moves between the raw and adjusted runs. The flip is demonstrated on the "
            "fixture split in `tests/integration/test_backtest_adjusted.py`, where the raw signal "
            "shows a fake ~-50% twelve-month momentum across the ex-date and the adjusted signal "
            "does not."
            if not flipped
            else "- " + ", ".join(flipped)
        ),
        "",
        "## PIT",
        "",
        "- Both runs completed with every session's queries scoped to that session; no `PitError` "
        "was raised. The adjusted momentum *ratio* is PIT-safe by construction: a corporate action "
        "after the rebalance date scales both endpoints of the trailing return identically and "
        "cancels, so no future split leaks into a past decision (invariant #7).",
        "",
    ]
    return "\n".join(lines)


def run_delta_report(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    parameters: MomentumParameters | None = None,
    data_root: Path | None = None,
) -> str:
    """Run the raw and adjusted backtests over the same window and render the M9.2 delta report."""
    raw = run_naive_momentum(
        start=start,
        end=end,
        opening_cash=opening_cash,
        parameters=parameters,
        data_root=data_root,
        adjusted=False,
    )
    adjusted = run_naive_momentum(
        start=start,
        end=end,
        opening_cash=opening_cash,
        parameters=parameters,
        data_root=data_root,
        adjusted=True,
    )
    return render_delta_report(raw, adjusted)


def _print_summary(run: BacktestResult) -> None:
    """A terse stdout summary; the full report is the markdown file when `--report` is passed."""
    c = run.comparison
    print(
        f"naive_momentum {run.start.isoformat()}→{run.terminal.isoformat()}: "
        f"{run.sessions} sessions, {run.rebalances} rebalances, {run.runtime_seconds:.1f}s"
    )
    print(f"  final NAV {_rupees(run.final_nav)} from {_rupees(run.opening_cash)}")
    print(
        f"  XIRR portfolio {_pct(c.portfolio_xirr)} vs NIFTY-TRI proxy {_pct(c.benchmark_xirr)} "
        f"(excess {_pct(c.excess_over_benchmark)})"
    )
    print(f"  costs {_rupees(run.total_charges)}; journal {len(run.result.journal)} entries")


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.run",
        description="Run a backtest policy end-to-end over history and report it (M4.10).",
    )
    parser.add_argument(
        "--policy", required=True, choices=("naive_momentum",), help="the policy to replay"
    )
    parser.add_argument("--from", dest="start", required=True, help="start date, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="end date, YYYY-MM-DD")
    parser.add_argument(
        "--report", action="store_true", help=f"write the markdown report to {_REPORT_PATH}"
    )
    parser.add_argument(
        "--raw",
        dest="adjusted",
        action="store_false",
        help="source the momentum signal from raw L1 closes (pre-M9.2 baseline); default is the "
        "L2 back-adjusted signal read through the query layer",
    )
    parser.add_argument(
        "--delta-report",
        action="store_true",
        help=f"run the raw and adjusted backtests and write the M9.2 delta report to "
        f"{_DELTA_REPORT_PATH}",
    )
    parser.set_defaults(adjusted=True)
    parser.add_argument(
        "--opening-cash",
        type=Decimal,
        default=_DEFAULT_OPENING_CASH,
        help="opening capital in rupees (a-priori default: 1,000,000)",
    )
    parser.add_argument(
        "--top-n", type=int, default=None, help="override the number of names held (a-priori: 20)"
    )
    parser.add_argument(
        "--data-root", type=Path, default=None, help="override the L0/L1/L2 lake root"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m backtest.run``. Returns a process exit code."""
    args = _parse_args(argv)
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as error:
        print(f"error: bad date: {error}", file=sys.stderr)
        return 2
    if end < start:
        print(f"error: --to {end} is before --from {start}", file=sys.stderr)
        return 2

    params = (
        MomentumParameters(top_n=args.top_n) if args.top_n is not None else MomentumParameters()
    )

    if args.delta_report:
        try:
            report = run_delta_report(
                start=start,
                end=end,
                opening_cash=args.opening_cash,
                parameters=params,
                data_root=args.data_root,
            )
        except BacktestError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        _DELTA_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DELTA_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  delta report written to {_DELTA_REPORT_PATH}")
        return 0

    try:
        run = run_naive_momentum(
            start=start,
            end=end,
            opening_cash=args.opening_cash,
            parameters=params,
            data_root=args.data_root,
            adjusted=args.adjusted,
        )
    except BacktestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    _print_summary(run)
    if args.report:
        report = render_report(run)
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  report written to {_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
