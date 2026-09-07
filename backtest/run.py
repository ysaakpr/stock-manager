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
import csv
import sys
import time
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

from analyst.journal.models import Decision, JournalEntry
from backtest.accounting import BenchmarkComparison, PortfolioBook
from backtest.policies.fundamentals_value import (
    FundamentalsRecord,
    FundamentalsSignal,
    FundamentalsValueParameters,
    FundamentalsValuePolicy,
)
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
from backtest.policies.sector_rotation import (
    SectorRotationParameters,
    SectorRotationPolicy,
    SectorRotationRecord,
)
from backtest.replay import BookSnapshot, Policy, ReplayEngine, ReplayResult
from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange as IdentityExchange
from dataplatform.identity.master import ListingStatus
from dataplatform.ingest.indices import (
    TriPoint,
    TriSeries,
    membership_asof,
    read_tri_series,
)
from dataplatform.ingest.xbrl import Nature
from dataplatform.logging import get_logger
from dataplatform.query.fundamentals_metrics import CONCEPTS_USED, compute_metrics
from dataplatform.query.pit import Dataset
from dataplatform.query.service import QueryService
from dataplatform.query.shapes import CrossSectionRequest
from dataplatform.query.universe import InMemoryListingCalendar, ListingWindow, pit_universe
from dataplatform.store.l2 import open_connection, register_raw_view
from dataplatform.store.paths import Layer, l1_partition_path, layer_root
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET
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
_SECTOR_ROTATION_REPORT_PATH = Path("ops/gates/M10-sector-rotation-report.md")

# ── M10.3 sector-rotation defaults (a priori, stated once, never tuned) ───────────────────────────
#: Sectors to stay invested in. A NIFTY-500-shaped universe spans ~15-20 industries, so the top 5 is
#: roughly the top quartile of sectors — the "be in the right industries" bet at a round setting.
_SECTOR_TOP_K = 5
#: Names to hold, drawn from the members of the top-K sectors. 20 matches the plain-momentum basket
#: size exactly, so the sector-rotation-vs-plain-momentum comparison isolates the sector gate.
_SECTOR_TOP_N = 20
#: The offline static sector map: the checked-in constituent CSVs (M3.9/M10.1 five-column lists).
#: Used only when the L1 store holds no constituent snapshots — a static current-day classification
#: applied backward, which is survivorship-biased and stated as such in the report (M10.2 fixes it).
_STATIC_SECTOR_MAP_DIR = Path("tests/fixtures/nifty_indices/constituents")

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
        lookback_sessions: Sequence[date] | None = None,
    ) -> None:
        self._reader = reader
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._universe_filter = universe_filter
        # Look-backs (12m/1m reference closes, volatility points, the follow-up session) walk
        # the *calendar*, not the replay window: a 12-month return on the first rebalance of a
        # window needs the year before the window, which is knowable history, not look-ahead.
        # Defaults to the replay sessions so a caller that passes nothing keeps its old digests.
        self._sessions = list(lookback_sessions if lookback_sessions is not None else sessions)
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

    def level_on(self, session: date) -> Decimal | None:
        """The broad-market proxy level on ``session`` — the market's own return path (M10.3)."""
        return self._level(session)

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
        lookback_sessions: Sequence[date] | None = None,
    ) -> None:
        self._reader = reader
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._universe_filter = universe_filter
        self._regime_source = regime_source
        # Look-backs (12m/1m reference closes, volatility points, the follow-up session) walk
        # the *calendar*, not the replay window: a 12-month return on the first rebalance of a
        # window needs the year before the window, which is knowable history, not look-ahead.
        # Defaults to the replay sessions so a caller that passes nothing keeps its old digests.
        self._sessions = list(lookback_sessions if lookback_sessions is not None else sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._signals: dict[date, tuple[MomentumV2Record, ...]] = {}
        self._universe_sizes: dict[date, int] = {}
        for rebalance_date in sorted(self._rebalance):
            records = self._compute(rebalance_date)
            self._signals[rebalance_date] = records
            self._universe_sizes[rebalance_date] = len(records)
        # The session after each rebalance is served too: the `redeploy_next_session` toggle
        # prices yesterday's basket at today's close there. Computed lazily on first read, so runs
        # without the toggle pay nothing for it and the rebalance-date universe sizes stay the
        # report's universe figure.
        self._followups: set[date] = {
            self._sessions[i + 1]
            for i, session in enumerate(self._sessions[:-1])
            if session in self._rebalance
        }

    def is_rebalance(self, session: date) -> bool:
        return session in self._rebalance

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        if as_of not in self._signals and as_of in self._followups:
            self._signals[as_of] = self._compute(as_of)
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
            reader,
            sessions,
            signal_closes=signal_closes,
            universe_filter=universe_filter,
            lookback_sessions=calendar,
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
            lookback_sessions=calendar,
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
    between them, plus the run digests. What the "data reality" section says depends on the runs:
    equal digests mean the store's factor chains were all the identity (the M9.2-era lake, with no
    corporate actions yet), so the run proved the plumbing and nothing else; different digests mean
    the adjusted signal actually diverged from the raw one, and the delta is the measured cost of
    the fake post-split momentum the raw signal was buying. ``flipped`` lists names whose
    twelve-month signal flipped across a known split between the two runs, when the caller computed
    them; this report does not compute them itself.
    """
    raw_x, adj_x = raw.comparison.portfolio_xirr, adjusted.comparison.portfolio_xirr
    raw_t, adj_t = _trades(raw), _trades(adjusted)
    raw_c, adj_c = raw.total_charges, adjusted.total_charges
    identical = raw.result.digest() == adjusted.result.digest()
    if identical:
        data_reality = (
            "The two run digests match: every factor chain in this store was the identity, so "
            "the materialized L2 equals L1 bar-for-bar and the adjusted signal equals the raw one "
            "**over this store** — the lake before M9.1's corporate-action backfill landed. The "
            "delta below is zero by construction; what this run proves is the plumbing "
            "(materialize L2 -> read adjusted through the query layer -> replay) end-to-end. The "
            "de-corruption itself — an adjusted signal that removes a split's fake ~-50% momentum "
            "— is asserted on a controlled known-split fixture in "
            "`tests/integration/test_backtest_adjusted.py`."
        )
        digest_note = "(adjusted equals raw: no factor in the store moved a close)."
    else:
        data_reality = (
            "The two run digests differ: the store carries corporate actions and adjustment "
            "factors, so the adjusted signal ranks on back-adjusted closes and the raw one on the "
            "prices as traded. Where they disagree, the raw signal was reading a split or bonus as "
            "a price move — the delta below is the measured cost of that, over this store's "
            "factors as they stood when the run was made (`adjustment_factors`, D3)."
        )
        digest_note = "(the adjusted signal diverged from the raw one)."
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
        data_reality,
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
        f"- **Digests identical:** {identical} {digest_note}",
        "",
        "## Signal flips across a known split",
        "",
        (
            "- Not enumerated by this report. The flip is demonstrated on the fixture split in "
            "`tests/integration/test_backtest_adjusted.py`, where the raw signal shows a fake "
            "~-50% twelve-month momentum across the ex-date and the adjusted signal does not."
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
            "+ Redeploy proceeds next session",
            MomentumV2Parameters(top_n=top_n, redeploy_next_session=True),
        ),
        (
            "All on (four M9.5 toggles)",
            MomentumV2Parameters(
                top_n=top_n,
                use_12_1=True,
                sell_band=sell_band,
                regime_filter=True,
                vol_scaled=True,
            ),
        ),
        (
            "All on + redeploy",
            MomentumV2Parameters(
                top_n=top_n,
                use_12_1=True,
                sell_band=sell_band,
                regime_filter=True,
                vol_scaled=True,
                redeploy_next_session=True,
            ),
        ),
        (
            "+ Vol target 15%",
            MomentumV2Parameters(top_n=top_n, vol_target_annual=Decimal("0.15")),
        ),
        (
            "All on + redeploy + vol target 15%",
            MomentumV2Parameters(
                top_n=top_n,
                use_12_1=True,
                sell_band=sell_band,
                regime_filter=True,
                vol_scaled=True,
                redeploy_next_session=True,
                vol_target_annual=Decimal("0.15"),
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
        "*Generated by `python -m backtest.run --policy naive_momentum --v2-report`. Six a-priori "
        "improvements to the naive momentum policy — 12-1 ranking, turnover banding, a regime "
        "filter, volatility-scaled weights (M9.5), then next-session redeployment of sale proceeds "
        "and a portfolio-level volatility target (2026-09-06) — each behind its own toggle, each "
        "measured in isolation against the naive baseline and then together.*",
        "",
        "## The six changes (each a stated, separately-toggleable parameter — no tuning)",
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
        "- **Redeploy proceeds next session** (`redeploy_next_session`) — a rebalance's sells fill "
        "T+1 and their cash used to wait for the next monthly rebalance; the toggle puts it into "
        "the basket already chosen, at the next session's prices, through the same allocator.",
        "- **Volatility target** (`vol_target_annual`, 15%, `assumed_correlation` 0.3) — estimate "
        "the basket's annualised volatility from the names' monthly volatilities and one stated "
        "correlation, hold `min(1, target / estimate)` of capital in it, trim pro-rata when over.",
        "",
        "With all six off the policy is the naive top-N policy exactly (the parity is pinned in "
        "`tests/unit/test_momentum_v2.py`), so the first row below is the naive baseline.",
        "",
        "## Data reality",
        "",
        "Every run reads the **raw** L1 momentum signal (`adjusted=False`, the M9.2 baseline; the "
        "adjusted-vs-raw delta is the M9.2 report's subject, not this one's). The universe is the "
        "M9.3 investable/liquid set (as-of index membership ∩ a median-turnover floor; the store "
        "holds no historical membership snapshots, so the liquidity floor is what narrows it). The "
        "benchmark is the broad-market **L1 proxy** (the store holds no M3.9 computed TRI — the "
        "close-all backfill is gated, AGENTIC_CONTEXT B1), and the regime overlay reads the same "
        "proxy index. Look-backs (the 12-month and 1-month reference closes, the volatility "
        "points) walk the full L1 calendar, so the first rebalance of the window already has a "
        "signal — earlier editions of this report held cash for the window's first year for want "
        "of one.",
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


# ── M10.3: sector-rotation report ────────────────────────────────────────────────────────────────


def _load_static_sector_map(map_dir: Path) -> dict[str, str]:
    """Build an ISIN -> industry map from the checked-in five-column constituent CSVs (M10.3).

    Reads every ``ind_<slug>list_<date>.csv`` in ``map_dir`` (the M3.9/M10.1 fixture format:
    ``Company Name,Industry,Symbol,Series,ISIN Code``) and folds the rows into one ISIN -> industry
    map, later files overwriting earlier so the most recent classification wins. This is the offline
    fallback the report uses **only because the L1 store holds no constituent snapshots yet**: it is
    a static *current-day* map applied backward, which is survivorship-biased (a name assumed to
    have always been in the industry, and the index, it is in now). The report states that limit
    plainly; M10.2's forward snapshot history replaces it with real point-in-time membership.
    """
    sector_by_isin: dict[str, str] = {}
    for path in sorted(map_dir.glob("ind_*list_*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                isin = (row.get("ISIN Code") or "").strip()
                industry = (row.get("Industry") or "").strip()
                if isin and industry:
                    sector_by_isin[isin] = industry
    if not sector_by_isin:
        raise BacktestError(
            f"no static sector map: {map_dir} holds no readable ind_*list_*.csv constituent files"
        )
    return sector_by_isin


class _L1SectorRotationData:
    """The sector-rotation policy's :class:`SectorRotationData` over L1 (M10.3).

    Built exactly like :class:`_L1MomentumData` — rebalance on the first session of each month, the
    survivorship-safe PIT price universe narrowed by the same M9.3 investable/liquidity screen, the
    momentum ratio from the same L2-adjusted-or-raw close source (M9.2), the sizing price from raw
    (invariant #3) — but each candidate is additionally tagged with the ``sector`` it belonged to,
    and the candidate set is narrowed to names that *have* a resolvable sector.

    Sector resolution is point-in-time by contract. When the L1 store holds constituent snapshots
    (post M10.1 live fetch / M10.2 accrual) the sector is read through ``membership_asof`` — the
    snapshot in force on the decision date — and stamped with that snapshot's own capture date, so a
    future map cannot leak (invariant #7). When the store holds no snapshots (as this lake does), a
    static current-day map (:func:`_load_static_sector_map`) stands in, stamped as-of the decision
    date; that is survivorship-biased and the report says so. Either way the record carries a
    ``knowable_date`` the policy's PIT guard checks.

    The same instance serves both report arms — sector rotation and plain momentum on the identical
    universe — because the arms differ only in the policy's ``top_k`` (the sector gate), never in
    the candidate set. The schedule is precomputed once, so the replay walk is dict lookups.
    """

    def __init__(
        self,
        reader: _L1Reader,
        sessions: Sequence[date],
        sector_by_isin: Mapping[str, str],
        *,
        signal_closes: SignalCloses | None = None,
        universe_filter: _InvestableUniverse | None = None,
        lookback_sessions: Sequence[date] | None = None,
    ) -> None:
        self._reader = reader
        self._sector_by_isin = dict(sector_by_isin)
        self._signal_closes: SignalCloses = (
            signal_closes if signal_closes is not None else reader.closes_on
        )
        self._universe_filter = universe_filter
        # Look-backs (12m/1m reference closes, volatility points, the follow-up session) walk
        # the *calendar*, not the replay window: a 12-month return on the first rebalance of a
        # window needs the year before the window, which is knowable history, not look-ahead.
        # Defaults to the replay sessions so a caller that passes nothing keeps its old digests.
        self._sessions = list(lookback_sessions if lookback_sessions is not None else sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._signals: dict[date, tuple[SectorRotationRecord, ...]] = {}
        self._universe_sizes: dict[date, int] = {}
        for rebalance_date in sorted(self._rebalance):
            records = self._compute(rebalance_date)
            self._signals[rebalance_date] = records
            self._universe_sizes[rebalance_date] = len(records)

    def is_rebalance(self, session: date) -> bool:
        return session in self._rebalance

    def signal(self, as_of: date) -> Dataset[SectorRotationRecord]:
        records = self._signals.get(as_of, ())
        return Dataset.declaring(
            f"sector_rotation@{as_of.isoformat()}",
            records,
            knowable_date=lambda record: record.knowable_date,
        )

    def rebalance_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._rebalance))

    @property
    def mean_universe_size(self) -> Decimal:
        sizes = [n for n in self._universe_sizes.values() if n > 0]
        if not sizes:
            return _ZERO
        return (Decimal(sum(sizes)) / Decimal(len(sizes))).quantize(Decimal("0.1"))

    @property
    def sector_count(self) -> int:
        """How many distinct industries the mapped universe spans — for the top-K context."""
        return len(set(self._sector_by_isin.values()))

    def _lookback_session(self, as_of: date) -> date | None:
        target = as_of - timedelta(days=_LOOKBACK_DAYS)
        index = bisect_right(self._sessions, target) - 1
        return self._sessions[index] if index >= 0 else None

    def _compute(self, as_of: date) -> tuple[SectorRotationRecord, ...]:
        reference = self._lookback_session(as_of)
        if reference is None:
            return ()
        universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
        if self._universe_filter is not None:
            universe = frozenset(self._universe_filter.constrain(as_of, universe))
        # Only names with a resolvable sector are sector-rotation candidates (never guessed).
        universe = frozenset(isin for isin in universe if isin in self._sector_by_isin)
        signal_now = self._signal_closes(as_of)
        signal_then = self._signal_closes(reference)
        raw_now = self._reader.closes_on(as_of)
        records: list[SectorRotationRecord] = []
        for isin in universe:
            now = signal_now.get(isin)
            then = signal_then.get(isin)
            price = raw_now.get(isin)
            if now is None or then is None or then <= _ZERO or price is None:
                continue
            records.append(
                SectorRotationRecord(
                    isin=isin,
                    momentum=now / then - _ONE,
                    price=price,
                    sector=self._sector_by_isin[isin],
                    knowable_date=as_of,
                )
            )
        return tuple(records)


@dataclass(frozen=True, slots=True)
class _RegimeReturns:
    """One strategy's return split by market regime — the per-regime comparison unit (M10.3).

    ``risk_on`` / ``risk_off`` are the geometrically-linked cumulative returns over the sessions the
    proxy index spent at/above vs below its moving average, as plain ratios (0.20 = +20 %). They are
    computed off the strategy's own per-session NAV path, so costs are already embedded.
    """

    risk_on: Decimal
    risk_off: Decimal


def _split_returns_by_regime(
    nav_path: Sequence[tuple[date, Decimal]], risk_on_by_session: Mapping[date, bool]
) -> _RegimeReturns:
    """Geometrically link a NAV path's per-session returns into risk-on and risk-off buckets.

    Each step ``nav[t-1] -> nav[t]`` is a gross return credited to the regime in force at the start
    of the step (``nav[t-1]``'s session), because that is the state the money was invested through.
    Returns each bucket's cumulative ratio minus one; an empty bucket contributes zero. Exact in
    ``Decimal`` and deterministic — no float enters the risk metric.
    """
    on_growth = _ONE
    off_growth = _ONE
    for (earlier, prior_nav), (_, nav) in pairwise(nav_path):
        if prior_nav <= _ZERO:
            continue
        gross = nav / prior_nav
        if risk_on_by_session.get(earlier, True):
            on_growth *= gross
        else:
            off_growth *= gross
    return _RegimeReturns(risk_on=on_growth - _ONE, risk_off=off_growth - _ONE)


@dataclass(frozen=True, slots=True)
class _SectorArm:
    """One report row: a strategy's full-period metrics plus its per-regime returns (M10.3)."""

    label: str
    comparison: BenchmarkComparison
    max_drawdown: Decimal
    trades: int
    total_charges: Decimal
    regime: _RegimeReturns


def _run_sector_arm(
    *,
    label: str,
    data: _L1SectorRotationData,
    params: SectorRotationParameters,
    reader: _L1Reader,
    calendar: Sequence[date],
    sessions: Sequence[date],
    opening_cash: Decimal,
    benchmark_slug: str,
    data_root: Path | None,
    risk_on_by_session: Mapping[date, bool],
) -> _SectorArm:
    """Replay one sector-rotation arm and derive its full-period + per-regime metrics.

    Wires the same stack as :func:`run_momentum_v2` — L1 fills through ``SimBroker`` and the one
    shared cost model (invariant #4), M4.6 accounting mirrored off the fills, journaling through the
    replay engine — samples a complete per-session NAV path, and returns the benchmark comparison,
    max drawdown, turnover, cost and the risk-on/risk-off return split. The ``data`` source is used
    across arms unchanged; only ``params`` (the ``top_k`` gate) differs.
    """
    first_session, terminal = sessions[0], sessions[-1]
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
    nav_path: list[tuple[date, Decimal]] = []

    def sample_nav(session: date) -> None:
        last_close.update(reader.closes_on(session))
        # Mark every held name at its last-known close, falling back to its average cost for a name
        # that has not printed yet, so the path has a value on every session (never guessed high).
        prices = {pos.isin: last_close.get(pos.isin, pos.average_price) for pos in book.positions()}
        nav_path.append((session, book.net_asset_value(prices)))

    broker = _AccountingBroker(sim, book, nav_sink=sample_nav)
    engine = ReplayEngine(
        policy=SectorRotationPolicy(data, params),
        broker=broker,
        clock=clock,
        sessions=sessions,
    )
    result = engine.run()

    terminal_prices = _terminal_prices(reader, book, sessions)
    resolved = _resolve_benchmark(
        reader,
        data.rebalance_dates(),
        first_session,
        terminal,
        slug=benchmark_slug,
        data_root=data_root,
    )
    comparison = book.compare_to_benchmarks(
        terminal, terminal_prices, benchmark=resolved.series, theme=resolved.series
    )
    trades = sum(1 for entry in result.journal if entry.decision in (Decision.BUY, Decision.SELL))
    return _SectorArm(
        label=label,
        comparison=comparison,
        max_drawdown=_max_drawdown([nav for _, nav in nav_path]),
        trades=trades,
        total_charges=broker.total_charges,
        regime=_split_returns_by_regime(nav_path, risk_on_by_session),
    )


def _market_regime_returns(
    regime_source: _RegimeSource, sessions: Sequence[date], risk_on_by_session: Mapping[date, bool]
) -> _RegimeReturns:
    """The market (proxy index) return split by regime — the same buckets the strategies use."""
    path: list[tuple[date, Decimal]] = []
    for session in sessions:
        level = regime_source.level_on(session)
        if level is not None:
            path.append((session, level))
    return _split_returns_by_regime(path, risk_on_by_session)


def run_sector_rotation_report(
    *,
    start: date,
    end: date,
    top_k: int = _SECTOR_TOP_K,
    top_n: int = _SECTOR_TOP_N,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    sector_map_dir: Path = _STATIC_SECTOR_MAP_DIR,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
) -> str:
    """Run sector rotation vs plain momentum (same universe) vs market and render the M10.3 report.

    Both strategy arms read the *identical* candidate set — the investable, sector-mapped PIT
    universe — from one :class:`_L1SectorRotationData`, and are the *same* policy at two ``top_k``
    settings: the sector-rotation arm keeps the top ``top_k`` momentum sectors; the plain-momentum
    arm sets ``top_k`` above the sector count so the gate is a no-op and it holds the top ``top_n``
    names across the whole universe. So the only thing that differs between the two is the sector
    gate — the sector effect, isolated (M10.3). The market is the M9.4 benchmark (computed TRI when
    the store holds it, else the L1 proxy). Costs are in every fill (invariant #4). Metrics are
    reported full-period and split by market regime (proxy index at/above vs below its moving
    average). Returns the rendered markdown.
    """
    reader = _L1Reader(data_root=data_root)
    try:
        sessions = reader.trading_sessions(start, end)
        if not sessions:
            raise BacktestError(f"no trading sessions in [{start.isoformat()}, {end.isoformat()}]")
        calendar = reader.all_sessions()
        sessions = _reserve_fill_headroom(sessions, calendar)
        first_session = sessions[0]

        sector_by_isin = _load_static_sector_map(sector_map_dir)
        universe_filter = _InvestableUniverse(reader, UniverseParameters(), data_root=data_root)
        data = _L1SectorRotationData(
            reader,
            sessions,
            sector_by_isin,
            universe_filter=universe_filter,
            lookback_sessions=calendar,
        )

        regime_source = _RegimeSource(
            reader,
            calendar,
            first_session=first_session,
            size=_BENCHMARK_BASKET,
            ma_days=_REGIME_MA_DAYS,
        )
        risk_on_by_session = {
            session: regime_source.reading(session).risk_on for session in sessions
        }

        # Plain momentum on the same universe: top_k above the sector count neutralises the gate.
        plain_top_k = data.sector_count + 1
        arms = [
            _run_sector_arm(
                label="Sector rotation",
                data=data,
                params=SectorRotationParameters(top_k=top_k, top_n=top_n),
                reader=reader,
                calendar=calendar,
                sessions=sessions,
                opening_cash=opening_cash,
                benchmark_slug=benchmark_slug,
                data_root=data_root,
                risk_on_by_session=risk_on_by_session,
            ),
            _run_sector_arm(
                label="Plain momentum (same universe)",
                data=data,
                params=SectorRotationParameters(top_k=plain_top_k, top_n=top_n),
                reader=reader,
                calendar=calendar,
                sessions=sessions,
                opening_cash=opening_cash,
                benchmark_slug=benchmark_slug,
                data_root=data_root,
                risk_on_by_session=risk_on_by_session,
            ),
        ]
        market_regime = _market_regime_returns(regime_source, sessions, risk_on_by_session)
        risk_on_sessions = sum(1 for on in risk_on_by_session.values() if on)
        resolved = _resolve_benchmark(
            reader,
            data.rebalance_dates(),
            first_session,
            sessions[-1],
            slug=benchmark_slug,
            data_root=data_root,
        )
        return render_sector_rotation_report(
            arms=arms,
            market_regime=market_regime,
            top_k=top_k,
            top_n=top_n,
            first_session=first_session,
            terminal=sessions[-1],
            sessions=len(sessions),
            rebalances=len(data.rebalance_dates()),
            mean_universe=data.mean_universe_size,
            sector_count=data.sector_count,
            mapped_names=len(sector_by_isin),
            risk_on_sessions=risk_on_sessions,
            benchmark_computed_tri=resolved.is_computed_tri,
            benchmark_xirr=arms[0].comparison.benchmark_xirr,
        )
    finally:
        reader.close()


def render_sector_rotation_report(
    *,
    arms: Sequence[_SectorArm],
    market_regime: _RegimeReturns,
    top_k: int,
    top_n: int,
    first_session: date,
    terminal: date,
    sessions: int,
    rebalances: int,
    mean_universe: Decimal,
    sector_count: int,
    mapped_names: int,
    risk_on_sessions: int,
    benchmark_computed_tri: bool,
    benchmark_xirr: Decimal,
) -> str:
    """The M10.3 report: sector rotation vs plain momentum (same universe) vs market, per regime."""
    rotation, plain = arms[0], arms[1]

    def regime_row(label: str, r: _RegimeReturns) -> str:
        return f"| {label} | {_pct(r.risk_on)} | {_pct(r.risk_off)} |"

    lines = [
        "# M10.3 — Sector-rotation backtest policy",
        "",
        "*Generated by `python -m backtest.run --policy sector_rotation --sector-rotation-report`. "
        "Ranks sectors by their members' aggregate (mean) momentum, keeps the top-K sectors, and "
        "holds the top-N momentum names within them — the 'be in the right industries' bet — "
        "measured against plain momentum on the identical universe (to isolate the sector effect) "
        "and against the market, per regime, costs included.*",
        "",
        "## The policy (a-priori parameters — stated once, never tuned)",
        "",
        f"- **top-K sectors = {top_k}** — rank every sector by the *mean* momentum of its members "
        "in the universe (mean, not sum, so a broad sector is not favoured for its size), keep the "
        f"top {top_k}.",
        f"- **top-N names = {top_n}** — hold the top {top_n} momentum names drawn from the members "
        f"of those {top_k} sectors, equal-weighted. {top_n} matches the plain-momentum basket size "
        "exactly, so the only difference between the two arms is the sector gate.",
        "- **Plain momentum (same universe)** is the *same policy* with the sector gate off "
        f"(top-K set above the {sector_count} sectors), so it holds the top-{top_n} momentum "
        "names across the whole universe — an exact like-for-like against which the sector gate is "
        "the sole variable.",
        "",
        "## Survivorship / static-map limitation (read this before the numbers)",
        "",
        "**This run applies a static, current-day sector map backward over the history, which "
        "is survivorship-biased.** niftyindices publishes constituents *as of today only*; the L1 "
        "store here holds **no** constituent snapshots yet (M10.1's fetch is a live operation; "
        "M10.2 accrues forward point-in-time history week by week), so the sector of each name is "
        f"taken from the checked-in current-day classification ({mapped_names} names across "
        f"{sector_count} industries) and stamped as-of each decision date. That silently assumes a "
        "name was always in the industry — and in the index — it sits in now, which flatters any "
        "result. The policy is point-in-time by construction: it resolves membership through "
        "`membership_asof` (the snapshot in force on the decision date) and its guard refuses a "
        "record whose sector became knowable after the session, so **once M10.2's forward history "
        "matures the policy runs survivorship-free with no code change** — only the map source "
        "flips from the static fallback to the accrued snapshots. Then read the numbers below "
        "as a mechanism demonstration on a small mapped universe, not as an estimate of live edge.",
        "",
        "## Data reality (same M9 stack)",
        "",
        "Prices, the investable/liquidity screen and the PIT universe are the M9.2-M9.4 machinery "
        "unchanged: raw L1 closes for the signal (`adjusted=False`, the M9.2 baseline), the M9.3 "
        "investable set (as-of index membership ∩ a median-turnover floor; no historical "
        "membership snapshots in the store, so the liquidity floor is what narrows it), look-backs "
        "walking the full L1 calendar so the first rebalance already has a signal, and the "
        + (
            "M3.9 computed TRI"
            if benchmark_computed_tri
            else "pre-M9.4 broad-market **L1 proxy** (the store holds no M3.9 computed TRI — the "
            "close-all backfill is gated, AGENTIC_CONTEXT B1)"
        )
        + " as the market. The regime overlay reads the same broad-market L1 proxy index.",
        "",
        "## Window",
        "",
        f"- {first_session.isoformat()} -> {terminal.isoformat()} "
        f"({sessions} sessions, {rebalances} monthly rebalances)",
        f"- Mean sector-mapped investable universe / rebalance: {mean_universe}",
        f"- Regime split: {risk_on_sessions} of {sessions} sessions risk-on "
        "(proxy index at/above its "
        f"{_REGIME_MA_DAYS}-session moving average), the rest risk-off",
        f"- Market XIRR (identical cashflows): {_pct(benchmark_xirr)}",
        "",
        "## Sector rotation vs plain momentum vs market",
        "",
        "| Strategy | Portfolio XIRR | Max drawdown | Turnover (fills) | Total cost | "
        "Excess vs market |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in arms:
        c = arm.comparison
        lines.append(
            f"| {arm.label} | {_pct(c.portfolio_xirr)} | {_pct(arm.max_drawdown)} | "
            f"{arm.trades} | {_rupees(arm.total_charges)} | {_pct(c.excess_over_benchmark)} |"
        )
    lines += [
        f"| Market ({'computed TRI' if benchmark_computed_tri else 'L1 proxy'}) | "
        f"{_pct(benchmark_xirr)} | — | — | — | 0.00% |",
        "",
        "### Per-regime return (cumulative, costs embedded)",
        "",
        "Each strategy's NAV path split by the regime in force each session — the geometrically-"
        "linked return earned while the proxy index was at/above its moving average (risk-on) vs "
        "below it (risk-off). This is where a sector tilt tends to differ from plain momentum: in "
        "the turns.",
        "",
        "| Strategy | Risk-on cumulative | Risk-off cumulative |",
        "| --- | --- | --- |",
        regime_row(rotation.label, rotation.regime),
        regime_row(plain.label, plain.regime),
        regime_row("Market", market_regime),
        "",
        "## Reading it",
        "",
        "- **The sector-rotation vs plain-momentum row is the whole point**: same universe, same "
        f"basket size ({top_n}), same costs — the only difference is whether names are gated to "
        f"top-{top_k} momentum sectors first. A positive excess over plain momentum is the sector "
        "effect; a negative one says the gate cost more than it gained on this (small, static-map) "
        "universe.",
        "- **Do not read the excess-vs-market figure as alpha** — the market here is a "
        "computed/proxy total-return series, not the licensed feed (M9.4), and the universe is a "
        "small static-map sample. The comparison that is meaningful is rotation vs plain momentum.",
        "- **The per-regime split** is included because a sector tilt's value, if any, shows up "
        "unevenly across regimes rather than in the full-period average — the research that "
        "promoted M10 found static-map rotation a full-period wash but a clear winner in one "
        "multi-year window (survivorship-caveated).",
        "",
        "## PIT (invariant #7, acceptance #1)",
        "",
        "- Every arm completed with each session's queries scoped to that session; no `PitError` "
        "was raised. Sector membership is read through the PIT seam and the policy's guard "
        "refuses any record whose sector is not yet knowable on the session — the structural "
        "defence against a future sector map. The static current-day map used here is the one "
        "documented exception (stated above), stamped as-of the decision date; the "
        "`membership_asof`-backed path that stamps the in-force snapshot's own date is pinned in "
        "`tests/unit/test_sector_rotation.py` (a future snapshot trips the guard, an in-force one "
        "admits).",
        "",
    ]
    return "\n".join(lines)


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
        "--policy",
        required=True,
        choices=("naive_momentum", "sector_rotation", "fundamentals_value"),
        help="the policy to replay",
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
    parser.add_argument(
        "--sector-rotation-report",
        action="store_true",
        help=f"run sector rotation vs plain momentum (same universe) vs market (M10.3) and write "
        f"the report to {_SECTOR_ROTATION_REPORT_PATH}",
    )
    parser.add_argument(
        "--fundamentals-report",
        action="store_true",
        help=f"run the M10.6 fundamentals arms vs momentum vs market per regime and write the "
        f"report to {_FUNDAMENTALS_REPORT_PATH}",
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

    if args.sector_rotation_report:
        try:
            report = run_sector_rotation_report(
                start=start,
                end=end,
                opening_cash=args.opening_cash,
                data_root=args.data_root,
            )
        except BacktestError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        _SECTOR_ROTATION_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECTOR_ROTATION_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  sector-rotation report written to {_SECTOR_ROTATION_REPORT_PATH}")
        return 0

    if args.fundamentals_report:
        try:
            report = run_fundamentals_report(
                start=start,
                end=end,
                top_n=args.top_n if args.top_n is not None else 20,
                opening_cash=args.opening_cash,
                data_root=args.data_root,
            )
        except BacktestError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        _FUNDAMENTALS_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FUNDAMENTALS_REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"  fundamentals report written to {_FUNDAMENTALS_REPORT_PATH}")
        return 0

    if args.policy in ("sector_rotation", "fundamentals_value"):
        print(
            f"error: --policy {args.policy} requires its report flag "
            "(--sector-rotation-report / --fundamentals-report); no bare single-run summary is "
            "defined for it",
            file=sys.stderr,
        )
        return 2

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


# ── M10.6: the fundamentals-signal policy on the PIT store ───────────────────────────────────────

_FUNDAMENTALS_REPORT_PATH = Path("ops/gates/M10-fundamentals-signal-report.md")
#: A name whose newest filing is older than this on the rebalance date is not rankable (M10.6 a
#: priori: two missed quarterly deadlines).
_FUNDAMENTALS_MAX_STALENESS_DAYS = 200


class _PitFact(NamedTuple):
    """A `FactRow` read straight off the PIT parquet — the metrics module's input shape."""

    isin: str
    period_start: date | None
    period_end: date
    filing_date: date
    filing_id: str
    nature: Nature
    concept: str
    segment: str | None
    value: Decimal


class _L1FundamentalsData:
    """The M10.6 policy's :class:`FundamentalsSignalData` over the PIT store and L1 closes.

    Loads every company-level fact the metrics module reads (`CONCEPTS_USED`) once, sorted by
    filing date, and for each monthly rebalance hands `compute_metrics` exactly the prefix knowable
    on that date — so the point-in-time cut is a slice of a sorted list, and `compute_metrics`'s own
    refusal of a later filing is the backstop. Prices are the session's raw L1 closes (the sizing
    price, invariant #3); the universe is the survivorship-safe PIT universe narrowed by the same
    M9.3 investable/liquidity screen the momentum arms use, so the two families rank the same names.
    """

    def __init__(
        self,
        reader: _L1Reader,
        sessions: Sequence[date],
        *,
        data_root: Path | None,
        universe_filter: _InvestableUniverse | None,
        max_staleness_days: int = _FUNDAMENTALS_MAX_STALENESS_DAYS,
        lookback_sessions: Sequence[date] | None = None,
    ) -> None:
        self._reader = reader
        self._universe_filter = universe_filter
        self._max_staleness_days = max_staleness_days
        # The calendar the 12-1 momentum look-back walks (the MOMENTUM_VALUE arm's second signal);
        # the replay sessions alone would leave the first year without one (see _L1MomentumV2Data).
        self._calendar = list(lookback_sessions if lookback_sessions is not None else sessions)
        self._sessions = list(sessions)
        self._rebalance = set(_first_session_of_each_month(sessions))
        self._windows = reader.listing_windows()
        self._facts = self._load_facts(data_root)
        self._filing_dates = [fact.filing_date for fact in self._facts]
        self._signals: dict[date, tuple[FundamentalsRecord, ...]] = {}
        self._universe_sizes: dict[date, int] = {}
        self._excluded_scale = 0
        for rebalance_date in sorted(self._rebalance):
            records = self._compute(rebalance_date)
            self._signals[rebalance_date] = records
            self._universe_sizes[rebalance_date] = len(records)

    @staticmethod
    def _load_facts(data_root: Path | None) -> list[_PitFact]:
        root = layer_root(Layer.L1, data_root=data_root) / PIT_FUNDAMENTALS_DATASET
        if not root.is_dir():
            raise BacktestError(
                f"no PIT fundamentals store at {root}; run the M10.4 backfill first"
            )
        con = open_connection()
        try:
            rows = con.execute(
                "SELECT isin, period_start, period_end, filing_date, filing_id, nature, concept, "
                "value FROM read_parquet($glob) WHERE segment IS NULL AND concept IN $concepts "
                "ORDER BY filing_date, isin, concept",
                {"glob": str(root / "*" / "*.parquet"), "concepts": sorted(CONCEPTS_USED)},
            ).fetchall()
        finally:
            con.close()
        return [
            _PitFact(
                isin=str(isin),
                period_start=period_start,
                period_end=period_end,
                filing_date=filing_date,
                filing_id=str(filing_id),
                nature=Nature(nature),
                concept=str(concept),
                segment=None,
                value=Decimal(value),
            )
            for (
                isin,
                period_start,
                period_end,
                filing_date,
                filing_id,
                nature,
                concept,
                value,
            ) in rows
        ]

    def is_rebalance(self, session: date) -> bool:
        return session in self._rebalance

    def signal(self, as_of: date) -> Dataset[FundamentalsRecord]:
        records = self._signals.get(as_of, ())
        return Dataset.declaring(
            f"fundamentals@{as_of.isoformat()}",
            records,
            knowable_date=lambda record: record.knowable_date,
        )

    def rebalance_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._rebalance))

    @property
    def mean_universe_size(self) -> Decimal:
        sizes = [n for n in self._universe_sizes.values() if n > 0]
        if not sizes:
            return _ZERO
        return (Decimal(sum(sizes)) / Decimal(len(sizes))).quantize(Decimal("0.1"))

    @property
    def filings_excluded_scale(self) -> int:
        """How many (ISIN, rebalance) metric computations dropped a mis-scaled filing."""
        return self._excluded_scale

    @property
    def universe_sizes(self) -> Mapping[date, int]:
        """Rankable names per rebalance date — the store's staleness curve, read off the run."""
        return dict(self._universe_sizes)

    @property
    def latest_filing_date(self) -> date:
        """The newest filing date in the store — where the fundamentals stop being current."""
        return self._filing_dates[-1] if self._filing_dates else date.min

    def fresh_isins_on(self, session: date) -> int:
        """How many ISINs had a filing within the staleness limit on ``session`` — the real
        currency of the store on that date, which a handful of late filers' newest dates hide."""
        cutoff = bisect_right(self._filing_dates, session)
        floor = session - timedelta(days=self._max_staleness_days)
        return len({f.isin for f in self._facts[:cutoff] if f.filing_date >= floor})

    def _compute(self, as_of: date) -> tuple[FundamentalsRecord, ...]:
        cutoff = bisect_right(self._filing_dates, as_of)
        knowable = self._facts[:cutoff]
        universe = pit_universe(as_of, InMemoryListingCalendar(self._windows)).isins
        if self._universe_filter is not None:
            universe = frozenset(self._universe_filter.constrain(as_of, universe))
        closes = self._reader.closes_on(as_of)
        prices = {isin: closes[isin] for isin in universe if isin in closes}
        metrics = compute_metrics(
            (fact for fact in knowable if fact.isin in prices), as_of=as_of, prices=prices
        )
        momentum = self._momentum_12_1(as_of)
        records: list[FundamentalsRecord] = []
        for isin, m in metrics.items():
            self._excluded_scale += m.filings_excluded_scale
            if not isinstance(m.earnings_yield, Decimal):
                continue  # no share count, no price, or no four consecutive quarters: unrankable
            records.append(
                FundamentalsRecord(
                    isin=isin,
                    earnings_yield=m.earnings_yield,
                    earnings_growth=(
                        m.earnings_ttm_yoy if isinstance(m.earnings_ttm_yoy, Decimal) else None
                    ),
                    roe=m.roe if isinstance(m.roe, Decimal) else None,
                    price=prices[isin],
                    knowable_date=m.knowable_date,
                    momentum_12_1=momentum.get(isin),
                )
            )
        return tuple(records)

    def _momentum_12_1(self, as_of: date) -> dict[str, Decimal]:
        """The raw 12-1 return per ISIN as of ``as_of`` — the momentum v2 signal, PIT by reads."""
        cutoff = bisect_right(self._calendar, as_of - timedelta(days=_LOOKBACK_DAYS)) - 1
        one_month = bisect_right(self._calendar, as_of - timedelta(days=_MONTH_DAYS)) - 1
        if cutoff < 0 or one_month < 0:
            return {}
        base = self._reader.closes_on(self._calendar[cutoff])
        recent = self._reader.closes_on(self._calendar[one_month])
        return {
            isin: recent[isin] / base[isin] - _ONE
            for isin in base
            if isin in recent and base[isin] > _ZERO
        }


@dataclass(frozen=True, slots=True)
class _FundamentalsArm:
    """One report row: label, full-period metrics, per-regime split, and the a-priori parameters."""

    label: str
    parameters: str
    comparison: BenchmarkComparison
    max_drawdown: Decimal
    trades: int
    total_charges: Decimal
    regime: _RegimeReturns
    mean_universe: Decimal
    digest: str


def _run_policy_arm(
    *,
    label: str,
    parameters: str,
    policy: Policy,
    rebalance_dates: Sequence[date],
    mean_universe: Decimal,
    reader: _L1Reader,
    calendar: Sequence[date],
    sessions: Sequence[date],
    opening_cash: Decimal,
    benchmark_slug: str,
    data_root: Path | None,
    risk_on_by_session: Mapping[date, bool],
) -> _FundamentalsArm:
    """Replay any policy on the shared stack and derive full-period plus per-regime metrics.

    The same wiring as :func:`_run_sector_arm`, generalised over the policy so the fundamentals arms
    and the momentum arms in one report differ *only* in the policy object (invariant #4/#5: one
    broker, one cost model, one accounting book).
    """
    first_session, terminal = sessions[0], sessions[-1]
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
    nav_path: list[tuple[date, Decimal]] = []

    def sample_nav(session: date) -> None:
        last_close.update(reader.closes_on(session))
        prices = {pos.isin: last_close.get(pos.isin, pos.average_price) for pos in book.positions()}
        nav_path.append((session, book.net_asset_value(prices)))

    broker = _AccountingBroker(sim, book, nav_sink=sample_nav)
    engine = ReplayEngine(policy=policy, broker=broker, clock=clock, sessions=sessions)
    result = engine.run()
    terminal_prices = _terminal_prices(reader, book, sessions)
    resolved = _resolve_benchmark(
        reader, rebalance_dates, first_session, terminal, slug=benchmark_slug, data_root=data_root
    )
    comparison = book.compare_to_benchmarks(
        terminal, terminal_prices, benchmark=resolved.series, theme=resolved.series
    )
    trades = sum(1 for entry in result.journal if entry.decision in (Decision.BUY, Decision.SELL))
    return _FundamentalsArm(
        label=label,
        parameters=parameters,
        comparison=comparison,
        max_drawdown=_max_drawdown([nav for _, nav in nav_path]),
        trades=trades,
        total_charges=broker.total_charges,
        regime=_split_returns_by_regime(nav_path, risk_on_by_session),
        mean_universe=mean_universe,
        digest=result.digest(),
    )


def run_fundamentals_report(
    *,
    start: date,
    end: date,
    top_n: int = 20,
    opening_cash: Decimal = _DEFAULT_OPENING_CASH,
    data_root: Path | None = None,
    universe: UniverseParameters | None = None,
    benchmark_slug: str = _BENCHMARK_TRI_SLUG,
) -> str:
    """Run the three fundamentals arms beside naive and all-on momentum, per regime (M10.6).

    Every arm replays the same sessions on the same universe through the same broker, book and
    cost model; the market row is the proxy index's own path split by the same regime buckets.
    Returns the rendered markdown.
    """
    uni = universe if universe is not None else UniverseParameters()
    reader = _L1Reader(data_root=data_root)
    try:
        sessions = reader.trading_sessions(start, end)
        if not sessions:
            raise BacktestError(f"no trading sessions in [{start.isoformat()}, {end.isoformat()}]")
        calendar = reader.all_sessions()
        sessions = _reserve_fill_headroom(sessions, calendar)
        first_session = sessions[0]
        universe_filter = _InvestableUniverse(reader, uni, data_root=data_root)
        regime_source = _RegimeSource(
            reader,
            calendar,
            first_session=first_session,
            size=_BENCHMARK_BASKET,
            ma_days=_REGIME_MA_DAYS,
        )
        risk_on_by_session = {s: regime_source.reading(s).risk_on for s in sessions}
        fundamentals = _L1FundamentalsData(
            reader,
            sessions,
            data_root=data_root,
            universe_filter=universe_filter,
            lookback_sessions=calendar,
        )
        momentum = _L1MomentumV2Data(
            reader,
            sessions,
            regime_source,
            universe_filter=universe_filter,
            lookback_sessions=calendar,
        )
        common = {
            "reader": reader,
            "calendar": calendar,
            "sessions": sessions,
            "opening_cash": opening_cash,
            "benchmark_slug": benchmark_slug,
            "data_root": data_root,
            "risk_on_by_session": risk_on_by_session,
        }
        arms: list[_FundamentalsArm] = []
        for signal in FundamentalsSignal:
            params = FundamentalsValueParameters(signal=signal, top_n=top_n)
            arms.append(
                _run_policy_arm(
                    label=f"Fundamentals: {signal.value}",
                    parameters=(
                        f"signal={signal.value}, top_n={top_n}, sell_band=None, equal weight, "
                        f"max_staleness_days={params.max_staleness_days}, monthly"
                    ),
                    policy=FundamentalsValuePolicy(fundamentals, params),
                    rebalance_dates=fundamentals.rebalance_dates(),
                    mean_universe=fundamentals.mean_universe_size,
                    **common,  # type: ignore[arg-type]
                )
            )
        naive = MomentumV2Parameters(top_n=top_n)
        all_on = MomentumV2Parameters(
            top_n=top_n, use_12_1=True, sell_band=30, regime_filter=True, vol_scaled=True
        )
        for label, m_params in (
            ("Momentum: naive (all off)", naive),
            ("Momentum: v2 all-on", all_on),
        ):
            arms.append(
                _run_policy_arm(
                    label=label,
                    parameters=repr(m_params),
                    policy=MomentumV2Policy(momentum, m_params),
                    rebalance_dates=momentum.rebalance_dates(),
                    mean_universe=momentum.mean_universe_size,
                    **common,  # type: ignore[arg-type]
                )
            )
        market = _market_regime_returns(regime_source, sessions, risk_on_by_session)
        market_xirr = arms[0].comparison.benchmark_xirr
        risk_on_sessions = sum(1 for on in risk_on_by_session.values() if on)
        sizes = fundamentals.universe_sizes
        ordered = sorted(sizes)
        return render_fundamentals_report(
            arms,
            market=market,
            market_xirr=market_xirr,
            start=first_session,
            terminal=sessions[-1],
            sessions=len(sessions),
            rebalances=len(fundamentals.rebalance_dates()),
            risk_on_sessions=risk_on_sessions,
            excluded_scale=fundamentals.filings_excluded_scale,
            top_n=top_n,
            universe_first=(ordered[0], sizes[ordered[0]]) if ordered else None,
            universe_last=(ordered[-1], sizes[ordered[-1]]) if ordered else None,
            latest_filing=fundamentals.latest_filing_date,
            fresh_at_terminal=fundamentals.fresh_isins_on(sessions[-1]),
        )
    finally:
        reader.close()


def render_fundamentals_report(
    arms: Sequence[_FundamentalsArm],
    *,
    market: _RegimeReturns,
    market_xirr: Decimal,
    start: date,
    terminal: date,
    sessions: int,
    rebalances: int,
    risk_on_sessions: int,
    excluded_scale: int,
    top_n: int,
    universe_first: tuple[date, int] | None = None,
    universe_last: tuple[date, int] | None = None,
    latest_filing: date | None = None,
    fresh_at_terminal: int | None = None,
) -> str:
    """The M10.6 report: fundamentals arms vs momentum vs market, full period and per regime."""
    lines = [
        "# M10.6 — Fundamentals-signal backtest policy (value / growth) vs momentum vs market",
        "",
        "*Generated by `python -m backtest.run --policy fundamentals_value --fundamentals-report`. "
        "Three a-priori fundamentals signals read point-in-time off the M10.4/M10.5 PIT store, run "
        "through the identical M9 stack (adjusted-or-raw L1 closes, the M9.3 investable universe, "
        "SimBroker with the one shared cost model, M4.6 accounting) as the momentum arms they are "
        "compared with. Costs included everywhere.*",
        "",
        "## The a-priori construction (stated once, unchanged across every run below)",
        "",
        f"- **Basket:** top-{top_n}, equal weight, whole shares via the M4.7 allocator, sized from "
        "free cash (98% execution margin), monthly rebalance on the first session; no hysteresis "
        "band on the fundamentals arms so the signal alone drives turnover.",
        "- **VALUE:** rank on trailing earnings yield (TTM earnings / market cap, market cap = raw "
        "close x parser-derived shares outstanding); loss-makers are unrankable.",
        "- **GROWTH:** rank on TTM-on-TTM earnings growth; needs eight knowable quarters and a "
        "positive base.",
        "- **QUALITY_VALUE:** mean of the earnings-yield rank and the ROE rank; ROE needs the "
        "annual "
        "equity figure, which only the filings with a filled reserves tag carry, so this arm's "
        "universe is smaller (stated in the table).",
        "- **MOMENTUM_VALUE:** mean of the earnings-yield rank and the 12-1 momentum rank (the raw "
        "signal the momentum v2 arm ranks on), over names with positive trailing earnings.",
        "- **Staleness:** a name whose newest filing is older than 200 days on the rebalance date "
        "is "
        "not rankable.",
        "- **Point-in-time:** every metric is computed from filings with `filing_date <= session`; "
        "`compute_metrics` raises on anything later, and the policy admits records through the "
        "PIT guard. Restatements are invisible until published (invariants #7, #8).",
        f"- **Scale guard:** {excluded_scale} (ISIN, rebalance) computations dropped a filing "
        "whose "
        "paid-up capital sat a clean power of ten off the company's median (M10.5).",
        "",
        "## Window",
        "",
        f"- {start.isoformat()} -> {terminal.isoformat()} ({sessions} sessions, {rebalances} "
        "monthly rebalances)",
        f"- Regime split: {risk_on_sessions} of {sessions} sessions risk-on (proxy index at/above "
        f"its {_REGIME_MA_DAYS}-session moving average)",
        f"- Market XIRR (identical cashflows): {_pct(market_xirr)}",
    ]
    if latest_filing is not None:
        fresh = (
            f" On the terminal session {fresh_at_terminal} ISINs had a filing under 200 days old."
            if fresh_at_terminal is not None
            else ""
        )
        lines.append(
            f"- Newest filing in the PIT store: {latest_filing.isoformat()}.{fresh} A name is "
            "rankable only while its newest filing is under 200 days old, so once the store stops "
            "being current the fundamentals arms hold no names — end the window there or read the "
            "tail as cash."
        )
    if universe_first is not None and universe_last is not None:
        lines.append(
            f"- Rankable fundamentals universe: {universe_first[1]} names on the first rebalance "
            f"({universe_first[0].isoformat()}), {universe_last[1]} on the last "
            f"({universe_last[0].isoformat()})."
        )
    lines += [
        "",
        "## Full period",
        "",
        "| Strategy | Mean rankable universe | Portfolio XIRR | Max drawdown | Trades | Total cost "
        "| Excess vs market |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in arms:
        lines.append(
            f"| {arm.label} | {arm.mean_universe} | {_pct(arm.comparison.portfolio_xirr)} | "
            f"{_pct(arm.max_drawdown)} | {arm.trades} | {_rupees(arm.total_charges)} | "
            f"{_pct(arm.comparison.excess_over_benchmark)} |"
        )
    lines.append(f"| Market (L1 proxy) | — | {_pct(market_xirr)} | — | — | — | 0.00% |")
    lines += [
        "",
        "## Per-regime return (cumulative, costs embedded)",
        "",
        "| Strategy | Risk-on cumulative | Risk-off cumulative |",
        "| --- | --- | --- |",
    ]
    for arm in arms:
        lines.append(f"| {arm.label} | {_pct(arm.regime.risk_on)} | {_pct(arm.regime.risk_off)} |")
    lines.append(f"| Market | {_pct(market.risk_on)} | {_pct(market.risk_off)} |")
    lines += [
        "",
        "## Reading it",
        "",
        "- The question is diversification, not victory: does a fundamentals arm earn its return "
        "in "
        "a different regime bucket from momentum? Read the risk-off column against momentum's.",
        "- Do not read excess-vs-market as alpha — the market here is the L1 proxy (M9.4), and the "
        "fundamentals window starts where the PIT store does (2018-05), so it is shorter than "
        "M9's.",
        "- The rankable universe differs by arm (a loss-maker has no earnings yield; growth needs "
        "eight quarters; ROE needs a balance sheet), and it is stated per row for that reason.",
        "",
        "## Parameters and digests",
        "",
    ]
    for arm in arms:
        lines.append(f"- **{arm.label}:** `{arm.parameters}` — digest `{arm.digest}`")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
