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
on or around it, and later-delisted names stay in for earlier dates).

M9.4 change — the benchmark is now **M3.9's computed TRI**. The broad-market benchmark reads the
M3.9 pipeline's computed total-return series (:func:`~dataplatform.ingest.indices.read_tri_series` —
§4.1's fallback, seeded to the published index close and stamped ``computed_price_plus_div``) when
the store holds it, replacing the ad-hoc L1 proxy the earlier builds used. The licensed NSE
NIFTY-TRI feed is session-gated and FAILED at C.1, so the computed TRI is the benchmark and the
report says so plainly. When a store holds no computed TRI (this lake does — the close-all snapshot
that feeds ``compute_tri`` is a gated bulk fetch), the pre-M9.4 broad-market L1 proxy stands in and
the report records the fallback; the computed-TRI wiring itself is proven on the fixture in
``tests/integration/test_backtest_benchmark.py``. Either series flows through the *same*
:class:`~dataplatform.ingest.indices.TriSeries` and
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import BenchmarkComparison, PortfolioBook
from backtest.policies.momentum_v2 import (
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.policies.naive_momentum import (
    MomentumParameters,
    MomentumRecord,
    NaiveMomentumPolicy,
)
from backtest.replay import BookSnapshot, ReplayEngine, ReplayResult
from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange as IdentityExchange
from dataplatform.identity.master import ListingStatus
from dataplatform.ingest.indices import (
    TriPoint,
    TriSeries,
    membership_asof,
    read_tri_series,
)
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
_UNIVERSE_REPORT_PATH = Path("ops/gates/M9-universe-report.md")
_BENCHMARK_REPORT_PATH = Path("ops/gates/M9-benchmark-report.md")
_V2_REPORT_PATH = Path("ops/gates/M9-momentum-v2-report.md")

# ── M9.5 momentum v2 defaults (a priori, stated once, never tuned) ───────────────────────────────
#: The trailing window over which the regime index's moving average is struck. 200 sessions is the
#: standard trend filter. The regime index is the same broad-market L1 basket the benchmark proxy
#: uses (equal-weight price relative of the most-liquid names at the start), so the regime overlay
#: reads a proxy for the index, stated plainly in the report.
_REGIME_MA_DAYS = 200
#: The number of trailing monthly points the volatility estimate is struck over — a year of monthly
#: returns. Monthly (not daily) keeps the ten-year walk cheap and is ample for risk-parity sizing.
_VOL_MONTHS = 12
#: The volatility floor: when a name has too few monthly points for a sample stddev (or a degenerate
#: zero one), it is sized at this vol rather than dropped, so the candidate universe stays identical
#: across every v2 config and only the *use* of vol changes. A rare fallback, reported.
_VOL_FLOOR = Decimal("0.05")
_MONTH_DAYS = 30  # the calendar step between monthly volatility / skip-month reference points

# ── M9.4 benchmark defaults (a priori, stated once) ──────────────────────────────────────────────
#: The M3.9 index whose computed total-return series is the broad-market benchmark. NIFTY 50 is the
#: headline NSE index; its computed TRI — §4.1's fallback, seeded to the published index close and
#: stamped ``computed_price_plus_div`` (`dataplatform.ingest.indices.compute_tri`) — stands in for
#: the licensed niftyindices TRI, whose historical endpoint is session-gated and FAILED at C.1.
_BENCHMARK_TRI_SLUG = "nifty50"
#: The two benchmark provenances the run reports honestly. ``m3.9_computed_tri`` is the M9.4 wiring:
#: the M3.9 pipeline's computed TRI read out of L1. ``l1_proxy`` is the pre-M9.4 broad-market L1
#: proxy that stands in only when the store holds no M3.9 TRI (the close-all backfill is gated).
_BENCHMARK_COMPUTED_TRI = "m3.9_computed_tri"
_BENCHMARK_L1_PROXY = "l1_proxy"

# ── M9.3 investable-universe defaults (a priori, stated once, never tuned) ───────────────────────
#: The index whose as-of membership defines the investable set. NIFTY 500 is the broadest published
#: NSE index — a name outside it on a date is, by construction, off the investable map that day.
_DEFAULT_INVESTABLE_INDEX = "nifty500"
#: The liquidity floor: a name's *median daily traded value* over the look-back must clear this to
#: count as investable. ₹1 crore (₹10,000,000) is a deliberately conservative microcap cut — on the
#: real ten-year store it drops the illiquid ~35% tail (probed at build time) that a raw momentum
#: rank can otherwise pick, while keeping every tradeable name. Chosen once, reported verbatim.
_DEFAULT_TURNOVER_FLOOR = Decimal("10000000")
#: The window the median turnover is measured over — a trailing year, ending on the rebalance date
#: (so the whole measurement is point-in-time: no session after the decision enters it).
_DEFAULT_LIQUIDITY_LOOKBACK_DAYS = 365


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
        self._turnover: dict[tuple[date, date], dict[str, Decimal]] = {}

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

    def median_turnover_over(self, start: date, end: date) -> dict[str, Decimal]:
        """Each equity name's median daily traded value over ``[start, end]`` (cached per window).

        The liquidity statistic the M9.3 floor screens against: for every ISIN, the median of its
        ``total_traded_value`` across the sessions in the window on which it actually traded (a
        positive turnover). ``quantile_disc(..., 0.5)`` picks a real observed value rather than
        interpolating between two, so the result stays an exact ``Decimal`` off the lake's
        ``decimal128`` column and is deterministic (no float mid-point creeps into the screen).

        Point-in-time by contract: the caller passes a window that ends on or before the decision
        date, so no session after the decision enters the median. A name absent from the window
        (never traded in it) is simply absent from the result and fails the floor.
        """
        cached = self._turnover.get((start, end))
        if cached is not None:
            return cached
        rows = self._con.execute(
            f"SELECT isin, quantile_disc(total_traded_value, 0.5) FROM {self._VIEW} "
            "WHERE series = 'EQ' AND total_traded_value > 0 "
            "AND trade_date BETWEEN $start AND $end GROUP BY isin",
            {"start": start, "end": end},
        ).fetchall()
        medians = {str(isin): Decimal(median) for isin, median in rows}
        self._turnover[(start, end)] = medians
        return medians


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
    set for the map comes from L1's raw closes on the session.

    Raw is the base; L2 adjusted is overlaid where it exists. L2 is materialized only for names with
    a non-identity factor chain (a split/bonus/rights) — for every other name the adjusted close
    equals the raw close *exactly*, so the raw close IS its adjusted close, not an approximation.
    A factored name whose L2 failed to materialize (e.g. an unresolved percent-of-face-value
    dividend, see ops/BACKLOG.md) also falls back to raw and is therefore under-adjusted — a
    bounded, documented gap, not silent corruption. Closes are cached per session, so the walk
    pays once.
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
        raw = self._reader.closes_on(session)
        primary = dict.fromkeys(raw, IdentityExchange.NSE)
        cross = self._service.cross_section(
            CrossSectionRequest(trade_date=session, primary_by_isin=primary)
        )
        # Start from raw (exact for no-CA names); overlay the L2 adjusted close where it exists.
        closes = dict(raw)
        closes.update({row.isin: row.adj_close for row in cross.rows})
        self._closes[session] = closes
        return closes


# ── investable universe: as-of index membership ∩ a stated liquidity floor (M9.3) ────────────────


@dataclass(frozen=True, slots=True)
class UniverseParameters:
    """The a-priori investable-universe constraints (M9.3) — stated once, reported, never tuned.

    Two screens, both point-in-time:

    * ``index_slug`` — the index whose *as-of* membership defines the investable map. The screen
      reads the M3.9 constituents history through :func:`membership_asof`, which returns the
      snapshot in force on the decision date (never today's list — the survivorship-bias kill,
      invariant #7). When the store holds no snapshot on or before a date (``membership_asof`` →
      ``None``), the membership screen is a no-op for that date and only the liquidity floor
      applies; the run reports plainly whether membership data was present (see the report).

    * ``median_turnover_floor`` / ``liquidity_lookback_days`` — a name's median daily traded value
      over the trailing ``liquidity_lookback_days`` (ending on the decision date) must reach the
      floor. This is the microcap cut that kills the fake momentum a raw rank can pick off
      names that barely trade (ops/BACKLOG.md, M4.10).

    All three are fixed by construction and echoed verbatim in the report, so the "no tuning" line
    stays checkable.
    """

    index_slug: str = _DEFAULT_INVESTABLE_INDEX
    median_turnover_floor: Decimal = _DEFAULT_TURNOVER_FLOOR
    liquidity_lookback_days: int = _DEFAULT_LIQUIDITY_LOOKBACK_DAYS

    def __post_init__(self) -> None:
        if not isinstance(self.median_turnover_floor, Decimal):
            raise TypeError("median_turnover_floor must be a Decimal — money is never float")
        if self.median_turnover_floor < _ZERO:
            raise ValueError(
                f"median_turnover_floor must be >= 0, got {self.median_turnover_floor}"
            )
        if self.liquidity_lookback_days <= 0:
            raise ValueError(
                f"liquidity_lookback_days must be positive, got {self.liquidity_lookback_days}"
            )


class _InvestableUniverse:
    """Constrains a rebalance's candidate set to the investable, liquid names as-of a date (M9.3).

    The rebalance universe is the intersection of two point-in-time screens applied to the
    survivorship-safe PIT candidate set the momentum data already builds:

      1. **as-of index membership** — the M3.9 constituents snapshot in force on the date
         (:func:`membership_asof`); absent when the store carries no snapshot for the date, in which
         case this screen does not narrow the set (and the report says so);
      2. **a stated liquidity floor** — the name's median daily traded value over the trailing
         look-back reaches ``median_turnover_floor``.

    Both reads are scoped to sessions on or before the decision date, so the screen is PIT-safe:
    no future membership change and no post-decision turnover can enter it. Results are cached per
    date so the ten-year walk pays for each screen once. ``members_asof`` and ``liquid_asof`` are
    exposed so a test can assert either screen in isolation.
    """

    def __init__(
        self,
        reader: _L1Reader,
        params: UniverseParameters,
        *,
        data_root: Path | None = None,
    ) -> None:
        self._reader = reader
        self._params = params
        self._data_root = data_root
        self._members: dict[date, frozenset[str] | None] = {}
        self._liquid: dict[date, frozenset[str]] = {}

    def members_asof(self, as_of: date) -> frozenset[str] | None:
        """The as-of index membership on ``as_of`` — ``None`` when no snapshot is in force then."""
        if as_of in self._members:
            return self._members[as_of]
        snapshot = membership_asof(self._params.index_slug, as_of, data_root=self._data_root)
        members = None if snapshot is None else snapshot.members
        self._members[as_of] = members
        return members

    def liquid_asof(self, as_of: date) -> frozenset[str]:
        """The ISINs whose trailing median daily turnover reaches the floor as of ``as_of``."""
        if as_of in self._liquid:
            return self._liquid[as_of]
        start = as_of - timedelta(days=self._params.liquidity_lookback_days)
        medians = self._reader.median_turnover_over(start, as_of)
        floor = self._params.median_turnover_floor
        liquid = frozenset(isin for isin, median in medians.items() if median >= floor)
        self._liquid[as_of] = liquid
        return liquid

    def constrain(self, as_of: date, candidates: Iterable[str]) -> set[str]:
        """Candidates surviving both screens as of ``as_of`` (index membership ∩ liquidity)."""
        result = set(candidates) & self.liquid_asof(as_of)
        members = self.members_asof(as_of)
        if members is not None:
            result &= members
        return result


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
        universe_filter: _InvestableUniverse | None = None,
    ) -> None:
        self._reader = reader
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._universe_filter = universe_filter
        self._sessions = list(sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._signals: dict[date, tuple[MomentumRecord, ...]] = {}
        self._universe_sizes: dict[date, int] = {}
        for rebalance_date in sorted(self._rebalance):
            records = self._compute(rebalance_date)
            self._signals[rebalance_date] = records
            self._universe_sizes[rebalance_date] = len(records)

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

    @property
    def universe_sizes(self) -> Mapping[date, int]:
        """The number of ranked candidates at each rebalance — the investable universe size."""
        return dict(self._universe_sizes)

    @property
    def mean_universe_size(self) -> Decimal:
        """Mean investable-universe size across rebalances that had any candidate (0 if none)."""
        sizes = [n for n in self._universe_sizes.values() if n > 0]
        if not sizes:
            return _ZERO
        return (Decimal(sum(sizes)) / Decimal(len(sizes))).quantize(Decimal("0.1"))

    def _compute(self, as_of: date) -> tuple[MomentumRecord, ...]:
        reference = self._lookback_session(as_of)
        if reference is None:
            return ()  # no twelve-month history yet — nothing to rank
        universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
        # M9.3: narrow the survivorship-safe PIT universe to the investable, liquid set as of this
        # date — as-of index membership intersected with the median-turnover floor. Without a filter
        # this is the pre-M9.3 full universe (the M9.2 baseline the delta report is struck against).
        if self._universe_filter is not None:
            universe = frozenset(self._universe_filter.constrain(as_of, universe))
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


# ── M9.5: the regime index (a broad-market proxy) and its trailing moving average ────────────────


class _RegimeSource:
    """The regime overlay's index level and its trailing moving average, both point-in-time (M9.5).

    The regime index is the same broad-market L1 basket the benchmark proxy uses — an equal-weight
    price relative of the ``size`` most-liquid names on the first session, seeded to
    :data:`_TRI_SEED`. Its ``ma_days``-session simple moving average, struck over sessions on or
    before the decision date, is the trend filter: the momentum basket is held only while the level
    is at or above the average.

    Point-in-time by construction (invariant #7): both the level and the moving average read only
    closes on sessions ``<= as_of`` — no future close enters the average, so the regime a rebalance
    sees is exactly what was knowable that day. Levels are cached per session, so the ten-year walk
    computes each session's level once even though the moving-average windows overlap heavily.

    It is a *proxy* index, stated plainly in the report: the store holds no licensed index level, so
    the regime is read off the same L1 broad-market basket the benchmark proxy is built from.
    """

    def __init__(
        self,
        reader: _L1Reader,
        calendar: Sequence[date],
        *,
        first_session: date,
        size: int,
        ma_days: int,
    ) -> None:
        self._reader = reader
        self._calendar = list(calendar)
        self._ma_days = ma_days
        self._basket = reader.most_liquid_on(first_session, size)
        base = reader.closes_on(first_session)
        self._base = {isin: base[isin] for isin in self._basket if isin in base}
        if not self._base:
            raise BacktestError(
                f"cannot build a regime index: no basket closes on {first_session.isoformat()}"
            )
        self._levels: dict[date, Decimal | None] = {}

    def _level(self, session: date) -> Decimal | None:
        """The broad-market proxy level on ``session`` — ``None`` if no basket name printed."""
        if session in self._levels:
            return self._levels[session]
        closes = self._reader.closes_on(session)
        relatives = [closes[isin] / self._base[isin] for isin in self._base if isin in closes]
        level = _TRI_SEED * (sum(relatives, _ZERO) / Decimal(len(relatives))) if relatives else None
        self._levels[session] = level
        return level

    def reading(self, as_of: date) -> RegimeReading:
        """The regime reading as of ``as_of``: the current level and its trailing moving average."""
        cutoff = bisect_right(self._calendar, as_of)
        window = self._calendar[:cutoff][-self._ma_days :]
        levels = [level for s in window if (level := self._level(s)) is not None]
        if not levels:
            raise BacktestError(f"no regime index level on or before {as_of.isoformat()}")
        current = self._level(as_of)
        if current is None:
            current = levels[-1]  # no print on the date itself — carry the last real level
        moving_average = sum(levels, _ZERO) / Decimal(len(levels))
        return RegimeReading(
            index_level=current, moving_average=moving_average, knowable_date=as_of
        )


# ── M9.5: the v2 momentum signal — 0-12 and 12-1 returns, plus a trailing volatility ─────────────


def _sample_stdev(values: Sequence[Decimal]) -> Decimal | None:
    """Sample standard deviation of ``values`` (Decimal), or ``None`` for fewer than two points."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values, _ZERO) / Decimal(n)
    variance = sum(((v - mean) ** 2 for v in values), _ZERO) / Decimal(n - 1)
    return variance.sqrt()


class _L1MomentumV2Data:
    """The v2 policy's :class:`MomentumV2Data` (M9.5) — same PIT/universe wiring, richer signal.

    Built exactly like :class:`_L1MomentumData` — rebalance on the first session of each month, the
    survivorship-safe PIT universe narrowed by the same investable/liquidity screen (M9.3), the
    momentum ratio from the same L2-adjusted-or-raw close source (M9.2), the sizing price from raw
    L1 (invariant #3) — but every candidate carries what the four v2 toggles need:

    * ``momentum_0_12`` — the naive trailing return (``close_now / close_12m - 1``);
    * ``momentum_12_1`` — the return from twelve months ago to one month ago
      (``close_1m / close_12m - 1``), skipping the most recent month;
    * ``volatility`` — the sample standard deviation of the trailing twelve monthly returns, floored
      at :data:`_VOL_FLOOR` when too few points exist (so the universe stays identical across every
      v2 config and only the *use* of vol changes).

    The candidate set is identical to the naive/M9.3 set for the same parameters, so the increment
    report isolates each toggle rather than confounding it with a universe change. The regime
    reading is served through the injected :class:`_RegimeSource`.
    """

    def __init__(
        self,
        reader: _L1Reader,
        sessions: Sequence[date],
        regime_source: _RegimeSource,
        *,
        signal_closes: SignalCloses | None = None,
        universe_filter: _InvestableUniverse | None = None,
    ) -> None:
        self._reader = reader
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._universe_filter = universe_filter
        self._regime_source = regime_source
        self._sessions = list(sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._signals: dict[date, tuple[MomentumV2Record, ...]] = {}
        self._universe_sizes: dict[date, int] = {}
        for rebalance_date in sorted(self._rebalance):
            records = self._compute(rebalance_date)
            self._signals[rebalance_date] = records
            self._universe_sizes[rebalance_date] = len(records)

    def is_rebalance(self, session: date) -> bool:
        return session in self._rebalance

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        records = self._signals.get(as_of, ())
        return Dataset.declaring(
            f"momentum_v2@{as_of.isoformat()}",
            records,
            knowable_date=lambda record: record.knowable_date,
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = self._regime_source.reading(as_of)
        return Dataset.declaring(
            f"regime@{as_of.isoformat()}",
            (reading,),
            knowable_date=lambda r: r.knowable_date,
        )

    def rebalance_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._rebalance))

    @property
    def mean_universe_size(self) -> Decimal:
        sizes = [n for n in self._universe_sizes.values() if n > 0]
        if not sizes:
            return _ZERO
        return (Decimal(sum(sizes)) / Decimal(len(sizes))).quantize(Decimal("0.1"))

    def _session_on_or_before(self, target: date) -> date | None:
        index = bisect_right(self._sessions, target) - 1
        return self._sessions[index] if index >= 0 else None

    def _monthly_sessions(self, as_of: date) -> list[date]:
        """The trailing monthly reference sessions (as_of, -1m, ... -12m), deduped, ascending."""
        points: list[date] = []
        for k in range(_VOL_MONTHS + 1):
            session = self._session_on_or_before(as_of - timedelta(days=_MONTH_DAYS * k))
            if session is not None:
                points.append(session)
        return sorted(set(points))

    def _volatility(self, isin: str, monthly_maps: Sequence[Mapping[str, Decimal]]) -> Decimal:
        """The trailing monthly-return volatility for ``isin``, floored at :data:`_VOL_FLOOR`."""
        closes = [m[isin] for m in monthly_maps if isin in m and m[isin] > _ZERO]
        returns = [closes[i] / closes[i - 1] - _ONE for i in range(1, len(closes))]
        stdev = _sample_stdev(returns)
        if stdev is None or stdev <= _ZERO:
            return _VOL_FLOOR
        return stdev

    def _compute(self, as_of: date) -> tuple[MomentumV2Record, ...]:
        reference_12m = self._session_on_or_before(as_of - timedelta(days=_LOOKBACK_DAYS))
        reference_1m = self._session_on_or_before(as_of - timedelta(days=_MONTH_DAYS))
        if reference_12m is None or reference_1m is None:
            return ()  # not enough history to form either signal yet
        universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
        if self._universe_filter is not None:
            universe = frozenset(self._universe_filter.constrain(as_of, universe))
        signal_now = self._signal_closes(as_of)
        signal_12m = self._signal_closes(reference_12m)
        signal_1m = self._signal_closes(reference_1m)
        raw_now = self._reader.closes_on(as_of)
        monthly_sessions = self._monthly_sessions(as_of)
        monthly_maps = [self._signal_closes(session) for session in monthly_sessions]
        records: list[MomentumV2Record] = []
        for isin in universe:
            now = signal_now.get(isin)
            base_12 = signal_12m.get(isin)
            base_1 = signal_1m.get(isin)
            price = raw_now.get(isin)
            if (
                now is None
                or base_12 is None
                or base_1 is None
                or base_12 <= _ZERO
                or price is None
            ):
                continue
            records.append(
                MomentumV2Record(
                    isin=isin,
                    momentum_0_12=now / base_12 - _ONE,
                    momentum_12_1=base_1 / base_12 - _ONE,
                    price=price,
                    volatility=self._volatility(isin, monthly_maps),
                    knowable_date=as_of,
                )
            )
        return tuple(records)


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

    def __init__(
        self,
        sim: SimBroker,
        book: PortfolioBook,
        *,
        nav_sink: Callable[[date], None] | None = None,
    ) -> None:
        self._sim = sim
        self._book = book
        self.total_charges: Decimal = _ZERO
        # Optional per-session NAV sampler (M9.5): called after each session's fills are posted, so
        # a caller can build the NAV path a max-drawdown needs. ``None`` (the default) is the
        # pre-M9.5 behaviour exactly — no extra work, no change to the shared M9.2-M9.4 run.
        self._nav_sink = nav_sink

    @property
    def book(self) -> PortfolioBook:
        return self._book

    def execute_session(self, session: date) -> tuple[Order, ...]:
        filled = self._sim.execute_session(session)
        for order in filled:
            if order.status is OrderStatus.COMPLETE and order.fill is not None:
                self._book.record_fill(order.fill)
                self.total_charges += order.fill.cost.total
        if self._nav_sink is not None:
            self._nav_sink(session)
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


# ── M9.4: resolve the benchmark to M3.9's computed TRI, with the L1 proxy as the stated fallback ──


@dataclass(frozen=True, slots=True)
class _ResolvedBenchmark:
    """The benchmark series the run compared against, plus how it was sourced (for the report).

    ``source`` is ``_BENCHMARK_COMPUTED_TRI`` when the store held the M3.9 computed TRI M9.4 wires,
    and ``_BENCHMARK_L1_PROXY`` when the pre-M9.4 broad-market L1 proxy stood in because it did not.
    ``method`` is the ``TriSeries`` method (``computed_price_plus_div`` or ``published``) — carried
    so the report can say plainly the series is computed, never the licensed feed.
    """

    series: TriSeries
    source: str

    @property
    def is_computed_tri(self) -> bool:
        return self.source == _BENCHMARK_COMPUTED_TRI


def _resolve_benchmark(
    reader: _L1Reader,
    rebalance_dates: Sequence[date],
    first_session: date,
    terminal: date,
    *,
    slug: str,
    data_root: Path | None,
) -> _ResolvedBenchmark:
    """The broad-market benchmark: M3.9's computed TRI if the store holds it, else the L1 proxy.

    M9.4 wires the real NIFTY total-return series through the M3.9 pipeline. The licensed TRI
    endpoint is session-gated and FAILED at C.1, so §4.1's computed TRI (seeded to the published
    close, stamped ``computed_price_plus_div``) is the benchmark: this reads it out of L1 with
    :func:`~dataplatform.ingest.indices.read_tri_series` for ``slug`` through ``terminal`` — the
    point-in-time series knowable at the terminal valuation. When the store holds it *and* it covers
    the first cashflow date (so every deposit can be valued at an index level in force), that series
    is the benchmark and the source is ``_BENCHMARK_COMPUTED_TRI``.

    When it does not — the current lake holds only L1 raw prices; the close-all snapshot that feeds
    ``compute_tri`` is a gated bulk fetch (AGENTIC_CONTEXT B1) — the pre-M9.4 broad-market L1 proxy
    stands in and the source is ``_BENCHMARK_L1_PROXY``, which the report states plainly. Either way
    the series flows through the identical ``TriSeries`` / ``compare_to_benchmarks`` path
    (acceptance #3), so the published series slots in unchanged the day the gate opens.
    """
    series = read_tri_series(slug, terminal, data_root=data_root)
    if series is not None and series.points[0].as_of <= first_session:
        _LOG.info(
            "backtest.benchmark_source",
            source=_BENCHMARK_COMPUTED_TRI,
            index=series.index_slug,
            method=series.method,
            points=len(series.points),
            first_point=series.points[0].as_of.isoformat(),
        )
        return _ResolvedBenchmark(series=series, source=_BENCHMARK_COMPUTED_TRI)
    _LOG.info(
        "backtest.benchmark_source",
        source=_BENCHMARK_L1_PROXY,
        reason="no M3.9 computed TRI in the store covering the run window",
        slug=slug,
    )
    proxy = _build_benchmark_tri(reader, rebalance_dates, first_session, terminal)
    return _ResolvedBenchmark(series=proxy, source=_BENCHMARK_L1_PROXY)


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
    universe_filtered: bool
    mean_universe: Decimal
    benchmark_source: str
    benchmark_index_name: str
    benchmark_method: str
    #: Peak-to-trough max drawdown of the NAV path over the run, as a positive ratio (0.25 = -25%).
    #: Zero when no NAV path was sampled (the pre-M9.5 naive/adjusted/universe/benchmark runs).
    max_drawdown: Decimal = _ZERO

    @property
    def held_names(self) -> int:
        return len(self.book.holdings)

    @property
    def benchmark_is_computed_tri(self) -> bool:
        """True when the benchmark is M3.9's computed TRI (M9.4), not the pre-M9.4 L1 proxy."""
        return self.benchmark_source == _BENCHMARK_COMPUTED_TRI


def run_naive_momentum(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    parameters: MomentumParameters | None = None,
    data_root: Path | None = None,
    adjusted: bool = True,
    universe: UniverseParameters | None = None,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
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

    ``universe`` (M9.3) constrains the rebalance candidate set to the investable, liquid names as of
    each date: as-of index membership (M3.9 constituents) intersected with a median-turnover floor,
    both point-in-time (:class:`_InvestableUniverse`). ``None`` leaves the full survivorship-safe
    PIT universe in place — the pre-M9.3 baseline the universe delta report is struck against.

    ``benchmark_slug`` (M9.4) names the M3.9 index whose computed TRI is the broad-market benchmark.
    When the store holds that computed TRI (:func:`read_tri_series`) it is the benchmark; when it
    does not, the pre-M9.4 broad-market L1 proxy stands in and the result records which via
    ``benchmark_source`` (see :func:`_resolve_benchmark`). Either flows through the identical
    ``compare_to_benchmarks`` path, so the licensed series slots in unchanged once its gate opens.
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
        universe_filter = (
            _InvestableUniverse(reader, universe, data_root=data_root)
            if universe is not None
            else None
        )
        data = _L1MomentumData(
            reader, sessions, signal_closes=signal_closes, universe_filter=universe_filter
        )
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
        # M9.4: the broad-market benchmark is M3.9's computed TRI when the store holds it, else the
        # pre-M9.4 L1 proxy (the report states which). Either flows through the identical
        # compare_to_benchmarks path (acceptance #3). Theme stays the same series — the theme proxy
        # is an A-series concern M9.4 does not touch.
        resolved = _resolve_benchmark(
            reader,
            data.rebalance_dates(),
            first_session,
            terminal,
            slug=benchmark_slug,
            data_root=data_root,
        )
        benchmark = resolved.series
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
            universe_filtered=universe is not None,
            mean_universe=data.mean_universe_size,
            benchmark_source=resolved.source,
            benchmark_index_name=benchmark.index_name,
            benchmark_method=benchmark.method,
        )
    finally:
        if service is not None:
            service.close()
        reader.close()


def run_momentum_v2(
    *,
    start: date,
    end: date,
    v2_parameters: MomentumV2Parameters,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    adjusted: bool = False,
    universe: UniverseParameters | None = None,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
) -> BacktestResult:
    """Run the momentum v2 policy over ``[start, end]`` and return the report metrics (M9.5).

    Mirrors :func:`run_naive_momentum`'s wiring — the same L1 data, the one shared cost model behind
    ``SimBroker``, the M4.7 allocator inside the policy, M4.6 accounting mirrored off the fills,
    full journaling through the replay engine — but drives :class:`MomentumV2Policy` with
    ``v2_parameters`` and additionally samples the NAV path each session so the run reports a
    peak-to-trough max drawdown. With every toggle in ``v2_parameters`` off the run reproduces the
    naive result exactly (the increment report's baseline).

    ``adjusted`` defaults to ``False``: over this corporate-action-free store the L2 adjusted signal
    equals the raw one bar-for-bar (M9.2) and the ten-year L2 is not materialized, so the raw signal
    *is* the M9.2 signal here — the report says so. ``universe`` constrains the candidate set to the
    investable, liquid names (M9.3); the benchmark is M3.9's computed TRI when the store holds it,
    else the L1 proxy (M9.4). The regime overlay reads a broad-market L1 proxy index (stated).
    """
    reader = _L1Reader(data_root=data_root)
    service = QueryService(data_root=data_root) if adjusted else None
    try:
        sessions = reader.trading_sessions(start, end)
        if not sessions:
            raise BacktestError(f"no trading sessions in [{start.isoformat()}, {end.isoformat()}]")
        calendar = reader.all_sessions()
        sessions = _reserve_fill_headroom(sessions, calendar)
        first_session, terminal = sessions[0], sessions[-1]

        signal_closes = _AdjustedCloseSource(service, reader) if service is not None else None
        universe_filter = (
            _InvestableUniverse(reader, universe, data_root=data_root)
            if universe is not None
            else None
        )
        regime_source = _RegimeSource(
            reader,
            calendar,
            first_session=first_session,
            size=_BENCHMARK_BASKET,
            ma_days=v2_parameters.regime_ma_days,
        )
        data = _L1MomentumV2Data(
            reader,
            sessions,
            regime_source,
            signal_closes=signal_closes,
            universe_filter=universe_filter,
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

        # NAV path for the max-drawdown metric: after each session's fills, mark the whole book at
        # each held name's last-known close (a name that did not print that day is carried at its
        # previous close, never guessed or zeroed), so the path is a real point-in-time NAV series.
        last_close: dict[str, Decimal] = {}
        nav_path: list[Decimal] = []

        def sample_nav(session: date) -> None:
            last_close.update(reader.closes_on(session))
            positions = book.positions()
            if any(position.isin not in last_close for position in positions):
                return  # a held name with no close seen yet — skip this sample rather than guess
            nav_path.append(book.net_asset_value(last_close))

        broker = _AccountingBroker(sim, book, nav_sink=sample_nav)
        policy = MomentumV2Policy(data, v2_parameters)

        engine = ReplayEngine(policy=policy, broker=broker, clock=clock, sessions=sessions)
        started = time.perf_counter()
        result = engine.run()
        runtime = time.perf_counter() - started

        terminal_prices = _terminal_prices(reader, book, sessions)
        resolved = _resolve_benchmark(
            reader,
            data.rebalance_dates(),
            first_session,
            terminal,
            slug=benchmark_slug,
            data_root=data_root,
        )
        benchmark = resolved.series
        comparison = book.compare_to_benchmarks(
            terminal, terminal_prices, benchmark=benchmark, theme=benchmark
        )
        # A momentum parameter object for the shared BacktestResult fields (top_n, sleeve, budget);
        # the v2-specific toggles are reported separately by the caller from ``v2_parameters``.
        shared_params = MomentumParameters(
            top_n=v2_parameters.top_n,
            buy_budget_fraction=v2_parameters.buy_budget_fraction,
            sleeve=v2_parameters.sleeve,
        )
        return BacktestResult(
            policy="momentum_v2",
            adjusted=adjusted,
            start=first_session,
            terminal=terminal,
            sessions=len(sessions),
            rebalances=len(data.rebalance_dates()),
            parameters=shared_params,
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
            universe_filtered=universe is not None,
            mean_universe=data.mean_universe_size,
            benchmark_source=resolved.source,
            benchmark_index_name=benchmark.index_name,
            benchmark_method=benchmark.method,
            max_drawdown=_max_drawdown(nav_path),
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


def _max_drawdown(nav_path: Sequence[Decimal]) -> Decimal:
    """The largest peak-to-trough NAV decline over the path, as a positive ratio (0.25 = -25%).

    Walks the NAV series tracking the running peak; the drawdown at each point is
    ``(peak - nav) / peak`` and the result is the maximum of those. Zero for a non-declining path or
    an empty one. Deterministic and exact in ``Decimal`` — no float creeps into the risk metric.
    """
    peak = _ZERO
    worst = _ZERO
    for nav in nav_path:
        if nav > peak:
            peak = nav
        if peak > _ZERO:
            drawdown = (peak - nav) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


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


def _benchmark_label(run: BacktestResult) -> str:
    """The benchmark's row label in the returns table — states which series it actually is."""
    if run.benchmark_is_computed_tri:
        return f"{run.benchmark_index_name} — computed TRI (M3.9, `{run.benchmark_method}`)"
    return "NIFTY-TRI (broad-market TRI proxy from L1)"


def _benchmark_provenance_note(run: BacktestResult) -> str:
    """The provenance sentence under the returns table — computed vs licensed, plainly (M9.4)."""
    if run.benchmark_is_computed_tri:
        return (
            "> **Benchmark provenance (M9.4):** the benchmark is M3.9's **computed** total-return "
            f"index for `{run.benchmark_index_name}` — §4.1's fallback (`{run.benchmark_method}`), "
            "seeded to the published index close and chained off the price index plus an estimated "
            "dividend accrual. This is not the licensed niftyindices TRI, whose historical "
            "endpoint is session-gated and FAILED at C.1. So the excess over benchmark is measured "
            "against a dividend *estimate*, not the exchange's own TRI: it slightly understates "
            "the benchmark's true total return where realised dividends exceeded the accrual (and "
            "the reverse where they fell short). The series flows through the same `TriSeries` / "
            "`compare_to_benchmarks` path the licensed feed will, so it slots in unchanged once "
            "the gate opens. Do not read the excess as alpha."
        )
    return (
        "> **Benchmark provenance (M9.4):** this store holds no M3.9 computed TRI (the close-all "
        "snapshot that feeds `compute_tri` is a gated bulk fetch — AGENTIC_CONTEXT B1), so the "
        f"pre-M9.4 broad-market **L1 proxy** stands in (equal-weight average of the "
        f"{_BENCHMARK_BASKET} most-liquid names at the start, seeded to 1000). It is a price-"
        "return proxy: this is not the licensed NIFTY-TRI feed (session-gated, FAILED at C.1) and "
        "not "
        "even the computed TRI. The M9.4 wiring — the M3.9 computed TRI read through "
        "`read_tri_series` and "
        "flowed through the identical `compare_to_benchmarks` path — is proven on the fixture in "
        "`tests/integration/test_backtest_benchmark.py`. Do not read the excess as alpha."
    )


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
        f"| {_benchmark_label(run)} | {_pct(c.benchmark_xirr)} |",
        f"| **Excess over benchmark** | {_pct(c.excess_over_benchmark)} |",
        "",
        _benchmark_provenance_note(run),
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


def render_universe_report(
    baseline: BacktestResult,
    constrained: BacktestResult,
    *,
    universe: UniverseParameters,
    membership_present: bool,
) -> str:
    """The M9.3 report: the investable-universe run against the M9.2 (full-universe) baseline.

    States the a-priori thresholds, the universe-size move (mean candidates per rebalance, baseline
    vs constrained) and the XIRR / turnover / cost delta. ``membership_present`` records whether the
    store actually held any as-of index-membership snapshot the screen could apply — over a store
    with none, only the liquidity floor narrows the set, and the report says so plainly (the
    membership intersection itself is asserted on a controlled fixture in the test suite).
    """
    base_x, con_x = baseline.comparison.portfolio_xirr, constrained.comparison.portfolio_xirr
    base_t, con_t = _trades(baseline), _trades(constrained)
    base_c, con_c = baseline.total_charges, constrained.total_charges
    floor = universe.median_turnover_floor
    lines = [
        "# M9.3 — Investable universe + liquidity filter (10 years)",
        "",
        "*Generated by `python -m backtest.run --policy naive_momentum --universe-report`. The "
        "rebalance universe is now the as-of index membership (M3.9 constituents) intersected "
        "with a stated median-turnover floor, both point-in-time, replacing 'every name that "
        "traded'. This report strikes the constrained run against the M9.2 full-universe "
        "baseline; both read the same close source, so the delta isolates the universe effect.*",
        "",
        "## A-priori thresholds (stated once, not tuned)",
        "",
        f"- **Investable index:** `{universe.index_slug}` — as-of membership via "
        "`membership_asof` (the snapshot in force on the decision date, never today's list).",
        f"- **Liquidity floor:** median daily traded value ≥ {_rupees(floor)} over a trailing "
        f"{universe.liquidity_lookback_days}-day window ending on the rebalance date.",
        "",
        "## Data reality",
        "",
        (
            "This store holds **no** index-constituents snapshots (`index_constituents` is empty — "
            "M3.9's history accrues one snapshot per month from day one and the ten-year backfill "
            "of prior months does not exist to be fetched, AGENTIC_CONTEXT B1/§4.1). So over this "
            "run the as-of membership screen finds no snapshot in force on any date and does not "
            "narrow the set; **the liquidity floor is what moves the universe here.** The "
            "membership intersection — an as-of constituent list cutting the universe, and an "
            "illiquid name excluded on a date it would otherwise rank into the top-N — is asserted "
            "on a controlled fixture in `tests/integration/test_backtest_universe.py`, which loads "
            "real snapshots."
            if not membership_present
            else "This store holds index-constituents snapshots, so the run applies the full "
            "intersection: as-of membership ∩ the liquidity floor, both point-in-time."
        ),
        "",
        (
            "Both runs read the "
            + ("L2 back-adjusted" if constrained.adjusted else "raw L1")
            + " momentum signal. "
            + (
                ""
                if constrained.adjusted
                else "Over this corporate-action-free store the L2 adjusted signal equals the raw "
                "one bar-for-bar (M9.2), and the ten-year L2 is not materialized, so the raw "
                "signal *is* the M9.2 signal here — the baseline below is the effective M9.2 run."
            )
        ),
        "",
        "## Window",
        "",
        f"- {constrained.start.isoformat()} -> {constrained.terminal.isoformat()} "
        f"({constrained.sessions} sessions, {constrained.rebalances} monthly rebalances)",
        f"- Constrained run replay time: {constrained.runtime_seconds:.1f} s; "
        f"baseline run replay time: {baseline.runtime_seconds:.1f} s",
        "",
        "## Constrained vs M9.2 baseline",
        "",
        "| Metric | Full universe (M9.2) | Investable + liquid (M9.3) | Delta |",
        "| --- | --- | --- | --- |",
        f"| Mean universe size / rebalance | {baseline.mean_universe} | "
        f"{constrained.mean_universe} | {constrained.mean_universe - baseline.mean_universe} |",
        f"| Portfolio XIRR | {_pct(base_x)} | {_pct(con_x)} | {_pct(con_x - base_x)} |",
        f"| Turnover (BUY+SELL fills) | {base_t} | {con_t} | {con_t - base_t:+d} |",
        f"| Total costs | {_rupees(base_c)} | {_rupees(con_c)} | {_rupees(con_c - base_c)} |",
        f"| Final NAV | {_rupees(baseline.final_nav)} | {_rupees(constrained.final_nav)} | "
        f"{_rupees(constrained.final_nav - baseline.final_nav)} |",
        "",
        f"- **Run digest (baseline):** `{baseline.result.digest()}`",
        f"- **Run digest (constrained):** `{constrained.result.digest()}`",
        "",
        "## PIT",
        "",
        "- Both screens read only sessions on or before the decision date: `membership_asof` "
        "refuses a snapshot captured after the date, and the turnover median is measured over a "
        "window ending on it. No future membership change or post-decision turnover enters a "
        "past decision "
        "(invariant #7). Both runs completed with no `PitError` raised.",
        "",
    ]
    return "\n".join(lines)


def run_universe_report(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    parameters: MomentumParameters | None = None,
    universe: UniverseParameters | None = None,
    data_root: Path | None = None,
    adjusted: bool = False,
) -> str:
    """Run the M9.2 baseline and the M9.3 investable-universe backtest and render the delta report.

    Both runs read the same close source; they differ only in the universe screen, so the delta
    isolates the universe effect. ``adjusted`` defaults to ``False`` because over the real store
    the L2 adjusted signal equals the raw one bar-for-bar (no corporate actions — M9.2 established
    this and the ten-year L2 is not materialized), so the raw signal *is* the M9.2 signal here and
    the run completes without a materialized L2; pass ``adjusted=True`` on a store with a
    materialized L2 to strike the delta on the back-adjusted signal explicitly. ``membership_asof``
    is probed once over the rebalance window to record honestly whether the store held any snapshot
    the membership screen could apply.
    """
    uni = universe if universe is not None else UniverseParameters()
    baseline = run_naive_momentum(
        start=start,
        end=end,
        opening_cash=opening_cash,
        parameters=parameters,
        data_root=data_root,
        adjusted=adjusted,
        universe=None,
    )
    constrained = run_naive_momentum(
        start=start,
        end=end,
        opening_cash=opening_cash,
        parameters=parameters,
        data_root=data_root,
        adjusted=adjusted,
        universe=uni,
    )
    membership_present = any(
        membership_asof(uni.index_slug, rebalance, data_root=data_root) is not None
        for rebalance in (baseline.start, constrained.terminal)
    )
    return render_universe_report(
        baseline, constrained, universe=uni, membership_present=membership_present
    )


def render_benchmark_report(run: BacktestResult, *, benchmark_slug: str) -> str:
    """The M9.4 report: the backtest return against M3.9's computed TRI, provenance stated plainly.

    States which series actually stood as the benchmark (M3.9's computed TRI when the store held it,
    else the L1 proxy), that it is computed and not the licensed feed, and re-states the portfolio
    XIRR, the benchmark XIRR and the excess on that benchmark. The comparison is the one struck by
    :meth:`PortfolioBook.compare_to_benchmarks` inside the run — unchanged by M9.4 (acceptance #3).
    """
    c = run.comparison
    computed = run.benchmark_is_computed_tri
    lines = [
        "# M9.4 — NIFTY-TRI benchmark wired into the backtest (10 years)",
        "",
        "*Generated by `python -m backtest.run --policy naive_momentum --benchmark-report`. M9.4 "
        "replaces the ad-hoc L1 broad-market proxy with the real NIFTY total-return series flowed "
        "through the M3.9 pipeline: the benchmark is now M3.9's computed TRI "
        "(`read_tri_series`), passed to the same `compare_to_benchmarks` path as before.*",
        "",
        "## Benchmark provenance",
        "",
        (
            f"- **Series:** M3.9 **computed** TRI for `{run.benchmark_index_name}` "
            f"(slug `{benchmark_slug}`, method `{run.benchmark_method}`), read out of L1 with "
            "`read_tri_series`."
            if computed
            else "- **Series:** the pre-M9.4 broad-market **L1 proxy** — this store holds no M3.9 "
            f"computed TRI for slug `{benchmark_slug}` (the close-all snapshot that feeds "
            "`compute_tri` is a gated bulk fetch, AGENTIC_CONTEXT B1)."
        ),
        "- **Computed, not licensed.** The licensed niftyindices historical-TRI endpoint is "
        "session-gated and FAILED at C.1 (`nifty_tri_history`). The benchmark here is therefore an "
        "**estimate**: §4.1's computed fallback seeds to the published index close and chains the "
        "price return plus a dividend accrual estimated from the published yield — it is not the "
        "exchange's own TRI. "
        + (
            "Read the excess below against a dividend *estimate*: it understates the benchmark's "
            "true total return where realised dividends exceeded the constant-yield accrual, and "
            "overstates it where they fell short."
            if computed
            else "Over this store not even the computed TRI is available, so the L1 proxy (a "
            "price-return-only broad-market basket) stands in; the computed-TRI wiring is proven "
            "on the fixture in `tests/integration/test_backtest_benchmark.py`."
        ),
        "- **Path unchanged:** the series flows through the same `TriSeries` / "
        "`compare_to_benchmarks` machinery (acceptance #3), so the licensed feed slots in with no "
        "code change the day its gate opens.",
        "",
        "## Window",
        "",
        f"- {run.start.isoformat()} -> {run.terminal.isoformat()} "
        f"({run.sessions} sessions, {run.rebalances} monthly rebalances)",
        f"- Replay time: {run.runtime_seconds:.1f} s",
        "",
        "## Return vs benchmark (money-weighted XIRR, identical cashflows)",
        "",
        "| Series | XIRR |",
        "| --- | --- |",
        f"| Portfolio (naive momentum, **costs included**) | {_pct(c.portfolio_xirr)} |",
        f"| {_benchmark_label(run)} | {_pct(c.benchmark_xirr)} |",
        f"| **Excess over benchmark** | {_pct(c.excess_over_benchmark)} |",
        "",
        _benchmark_provenance_note(run),
        "",
        f"- **Run digest (sha256 of journal + book):** `{run.result.digest()}`",
        "",
    ]
    return "\n".join(lines)


def run_benchmark_report(
    *,
    start: date,
    end: date,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    parameters: MomentumParameters | None = None,
    data_root: Path | None = None,
    adjusted: bool = True,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
) -> str:
    """Run the backtest and render the M9.4 benchmark report against M3.9's computed TRI."""
    run = run_naive_momentum(
        start=start,
        end=end,
        opening_cash=opening_cash,
        parameters=parameters,
        data_root=data_root,
        adjusted=adjusted,
        benchmark_slug=benchmark_slug,
    )
    return render_benchmark_report(run, benchmark_slug=benchmark_slug)


# ── M9.5: momentum v2 increment report — naive vs each toggle vs all-on ───────────────────────────


@dataclass(frozen=True, slots=True)
class _V2Increment:
    """One row of the increment report: a human label, the toggles it turns on, and its run."""

    label: str
    parameters: MomentumV2Parameters
    run: BacktestResult


def _v2_configs(top_n: int, sell_band: int) -> list[tuple[str, MomentumV2Parameters]]:
    """Naive, each single-toggle increment, and all-on — the rows of the M9.5 report.

    Naive is every toggle off (which reproduces the naive policy). Each increment turns on exactly
    one change relative to naive, so the delta isolates it; all-on turns on all four. The banding
    increment widens the sell band from ``top_n`` to ``sell_band`` (the outer hysteresis band).
    """
    return [
        ("Naive (all off)", MomentumV2Parameters(top_n=top_n)),
        ("+ 12-1 momentum", MomentumV2Parameters(top_n=top_n, use_12_1=True)),
        ("+ Turnover banding", MomentumV2Parameters(top_n=top_n, sell_band=sell_band)),
        ("+ Regime filter", MomentumV2Parameters(top_n=top_n, regime_filter=True)),
        ("+ Vol-scaled weights", MomentumV2Parameters(top_n=top_n, vol_scaled=True)),
        (
            "All on",
            MomentumV2Parameters(
                top_n=top_n,
                use_12_1=True,
                sell_band=sell_band,
                regime_filter=True,
                vol_scaled=True,
            ),
        ),
    ]


def render_v2_report(increments: Sequence[_V2Increment], *, top_n: int, sell_band: int) -> str:
    """The M9.5 report: naive vs each increment vs all-on, on the M9.2-M9.4 inputs.

    One table with a row per configuration and columns for portfolio XIRR, max drawdown, turnover
    (BUY+SELL fills) and total cost — every figure struck on the same adjusted-signal, investable-
    universe, computed-TRI-benchmark stack, so each row differs from naive only by the toggle(s) it
    turns on. Costs are included in every fill (invariant #4), not deducted after the fact.
    """
    baseline = increments[0].run
    lines = [
        "# M9.5 — Momentum policy v2 (10 years)",
        "",
        "*Generated by `python -m backtest.run --policy naive_momentum --v2-report`. Four a-priori "
        "improvements to the naive momentum policy — 12-1 ranking, turnover banding, a regime "
        "filter and volatility-scaled weights — each behind its own toggle, each measured in "
        "isolation against the naive baseline and then all together.*",
        "",
        "## The four changes (each a stated, separately-toggleable parameter — no tuning)",
        "",
        "- **12-1 momentum** (`use_12_1`) — rank on the `t-12m .. t-1m` return, skipping the most "
        "recent month (the short-term-reversal month), instead of the raw `0..12m` return.",
        f"- **Turnover banding / hysteresis** (`sell_band`) — buy the top-{top_n} but only sell a "
        f"holding once it leaves the top-{sell_band} outer band, so a name drifting between rank "
        f"{top_n} and {sell_band} is held rather than churned.",
        "- **Regime filter** (`regime_filter`) — hold the basket only while the regime index is at "
        f"or above its {_REGIME_MA_DAYS}-session moving average; below it, sell the basket and "
        "park in the liquid sleeve (hold cash).",
        "- **Volatility-scaled weights** (`vol_scaled`) — size each name at `~ 1/vol` (risk "
        f"parity) over the trailing {_VOL_MONTHS} monthly returns, instead of pure equal weight.",
        "",
        "With all four off the policy is the naive top-N policy exactly (the parity is pinned in "
        "`tests/unit/test_momentum_v2.py`), so the first row below is the naive baseline.",
        "",
        "## Data reality (same as M9.2-M9.4)",
        "",
        "Every run reads the **raw** L1 momentum signal: this store holds no corporate actions, so "
        "the L2 back-adjusted signal equals the raw one bar-for-bar (M9.2) and the ten-year L2 is "
        "not materialized — the raw signal *is* the M9.2 signal here. The universe is the M9.3 "
        "investable/liquid set (as-of index membership ∩ a median-turnover floor; this store holds "
        "no membership snapshots, so the liquidity floor is what narrows it). The benchmark is the "
        + (
            "M3.9 computed TRI."
            if baseline.benchmark_is_computed_tri
            else "pre-M9.4 broad-market **L1 proxy** (the store holds no M3.9 computed TRI — the "
            "close-all backfill is gated, AGENTIC_CONTEXT B1)."
        )
        + " The regime overlay reads a **proxy** index — the same broad-market L1 basket the "
        "benchmark proxy is built from — because the store holds no licensed index level.",
        "",
        "## Window",
        "",
        f"- {baseline.start.isoformat()} -> {baseline.terminal.isoformat()} "
        f"({baseline.sessions} sessions, {baseline.rebalances} monthly rebalances)",
        f"- Mean investable universe / rebalance: {baseline.mean_universe}",
        f"- Benchmark XIRR (identical cashflows, all rows): "
        f"{_pct(baseline.comparison.benchmark_xirr)} "
        f"({_benchmark_label(baseline)})",
        "",
        "## Naive vs each increment vs all-on",
        "",
        "| Configuration | Portfolio XIRR | Max drawdown | Turnover (fills) | Total cost | "
        "Excess vs benchmark |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for increment in increments:
        run = increment.run
        lines.append(
            f"| {increment.label} | {_pct(run.comparison.portfolio_xirr)} | "
            f"{_pct(run.max_drawdown)} | {_trades(run)} | {_rupees(run.total_charges)} | "
            f"{_pct(run.comparison.excess_over_benchmark)} |"
        )
    naive = increments[0]
    lines += [
        "",
        "### Deltas vs naive (isolating each change)",
        "",
        "| Configuration | Δ XIRR | Δ Max drawdown | Δ Turnover | Δ Cost |",
        "| --- | --- | --- | --- | --- |",
    ]
    for increment in increments[1:]:
        run = increment.run
        base = naive.run
        lines.append(
            f"| {increment.label} | "
            f"{_pct(run.comparison.portfolio_xirr - base.comparison.portfolio_xirr)} | "
            f"{_pct(run.max_drawdown - base.max_drawdown)} | "
            f"{_trades(run) - _trades(base):+d} | "
            f"{_rupees(run.total_charges - base.total_charges)} |"
        )
    lines += [
        "",
        "## Reading it",
        "",
        "- **Turnover banding** is the change that most directly targets the naive run's churn — "
        f"the naive policy fired {_trades(naive.run)} fills over the decade "
        f"({naive.run.decision_counts[Decision.SELL.value]} of them sells); the banding row shows "
        "how much of that the hysteresis removes, and its cost delta is the saving.",
        "- **The regime filter** trades return for drawdown control: it sits in cash through the "
        "sessions the proxy index is below its moving average, so its max-drawdown column is the "
        "one to read against naive.",
        "- **Vol-scaling** and **12-1** reshape the basket rather than its size; read them in the "
        "XIRR and drawdown columns.",
        "- **Do not read any excess-vs-benchmark figure as alpha** — the benchmark here is a "
        "computed/proxy total-return series, not the licensed feed (M9.4). The point of this table "
        "is the *relative* effect of each toggle, all measured against the identical benchmark.",
        "",
        "## PIT (invariant #7, acceptance #3)",
        "",
        "- Every run completed with each session's queries scoped to that session; no `PitError` "
        "was raised. The regime reading and the volatility estimate read only closes on or before "
        "the decision date — the moving average is struck over a trailing window ending on the "
        "session, and the monthly volatility points are all on or before it — so no future data "
        "enters a past decision. The 12-1 and 0-12 ratios are PIT-safe by the same argument as "
        "M9.2 (a later corporate action scales both endpoints identically and cancels).",
        "",
        "## Run digests (determinism)",
        "",
    ]
    for increment in increments:
        lines.append(f"- **{increment.label}:** `{increment.run.result.digest()}`")
    lines.append("")
    return "\n".join(lines)


def run_v2_report(
    *,
    start: date,
    end: date,
    top_n: int = 20,
    sell_band: int = 30,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    universe: UniverseParameters | None = None,
) -> str:
    """Run naive, each single-toggle increment and all-on, and render the M9.5 increment report.

    Every configuration runs on the same M9.2-M9.4 stack (adjusted-or-raw signal, the M9.3
    investable universe, the M9.4 benchmark), so each row differs from naive only by the toggle(s)
    it turns on. Returns the rendered markdown.
    """
    uni = universe if universe is not None else UniverseParameters()
    increments: list[_V2Increment] = []
    for label, params in _v2_configs(top_n, sell_band):
        run = run_momentum_v2(
            start=start,
            end=end,
            v2_parameters=params,
            opening_cash=opening_cash,
            data_root=data_root,
            adjusted=False,
            universe=uni,
        )
        increments.append(_V2Increment(label=label, parameters=params, run=run))
    return render_v2_report(increments, top_n=top_n, sell_band=sell_band)


def _print_summary(run: BacktestResult) -> None:
    """A terse stdout summary; the full report is the markdown file when `--report` is passed."""
    c = run.comparison
    print(
        f"naive_momentum {run.start.isoformat()}→{run.terminal.isoformat()}: "
        f"{run.sessions} sessions, {run.rebalances} rebalances, {run.runtime_seconds:.1f}s"
    )
    print(f"  final NAV {_rupees(run.final_nav)} from {_rupees(run.opening_cash)}")
    bench = (
        f"{run.benchmark_index_name} computed TRI"
        if run.benchmark_is_computed_tri
        else "NIFTY-TRI L1 proxy"
    )
    print(
        f"  XIRR portfolio {_pct(c.portfolio_xirr)} vs {bench} {_pct(c.benchmark_xirr)} "
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
    parser.add_argument(
        "--universe-report",
        action="store_true",
        help=f"run the M9.2 full-universe baseline and the M9.3 investable-universe backtest and "
        f"write the universe delta report to {_UNIVERSE_REPORT_PATH}",
    )
    parser.add_argument(
        "--benchmark-report",
        action="store_true",
        help=f"run the backtest against M3.9's computed TRI and write the M9.4 benchmark report to "
        f"{_BENCHMARK_REPORT_PATH}",
    )
    parser.add_argument(
        "--v2-report",
        action="store_true",
        help=f"run naive vs each v2 increment vs all-on (M9.5) and write the momentum-v2 report to "
        f"{_V2_REPORT_PATH}",
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

    if args.universe_report:
        try:
            report = run_universe_report(
                start=start,
                end=end,
                opening_cash=args.opening_cash,
                parameters=params,
                data_root=args.data_root,
            )
        except BacktestError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        _UNIVERSE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _UNIVERSE_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  universe report written to {_UNIVERSE_REPORT_PATH}")
        return 0

    if args.benchmark_report:
        try:
            report = run_benchmark_report(
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
        _BENCHMARK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BENCHMARK_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  benchmark report written to {_BENCHMARK_REPORT_PATH}")
        return 0

    if args.v2_report:
        try:
            report = run_v2_report(
                start=start,
                end=end,
                top_n=args.top_n if args.top_n is not None else 20,
                opening_cash=args.opening_cash,
                data_root=args.data_root,
            )
        except BacktestError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        _V2_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _V2_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  v2 report written to {_V2_REPORT_PATH}")
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
