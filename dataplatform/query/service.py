"""D4 query service — the one entrypoint every consumer reads market data through (M4.1).

§4.5 calls for an "internal API over DuckDB/Parquet" serving a fixed set of canonical shapes. This
module is that API for the first two shapes (§4.5 (a) and (b)); M4.2 adds (c) and (d) on top of it.
The rule the task states plainly — *one query entrypoint module — every consumer (A3, A5, A6, X2,
D6) goes through it* — is why this is a single `QueryService` object and not a scatter of helpers:
the analyst, the rails, the rotation engine and the backtest all read prices the same way, so a
point-in-time or dedup rule fixed here is fixed for all of them at once.

Two shapes, both returning primary-deduped adjusted bars (`shapes.AdjustedPoint`):

* **`adjusted_series`** — one ISIN's back-adjusted OHLCV across a date window (§4.5 (a)). Reads the
  ISIN's L2 `prices_adjusted` partition (materialized by M2.5), windows it, and dedups to the
  ISIN's primary exchange.

* **`cross_section`** — the whole market's adjusted bars for one session, both exchanges deduped to
  the primary listing (§4.5 (b)). Reads the L2 adjusted view for the date and collapses each ISIN's
  NSE/BSE bars to one, so a caller never double-counts a dual-listed security.

The dedup is not re-implemented here: it is exactly M3.2's `canonical_daily` + `select_primary`
(`dataplatform.identity.primary`), fed L2 bars instead of raw ones. Primary selection is by
liquidity over a rolling window ending at the query date, so it honours the same "no future data in
a decision" boundary (invariant #7): only sessions on or before the query date can name the primary.
Consumers that already hold the day's primary map (M3.2 computes it once per session) pass it in and
skip the liquidity scan entirely; otherwise the service derives it from L1.

Offline and clockless by construction: DuckDB reads local Parquet and nothing fetches; the query
date is an argument, never a wall clock (B10). Money is `Decimal` throughout — the adjusted values
come straight off L2's `decimal128` columns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import TracebackType

import duckdb

from dataplatform.identity.master import Exchange, IdentityError
from dataplatform.identity.primary import (
    Canonical,
    DailyLiquidity,
    NoLiquidityError,
    PrimaryRule,
    canonical_daily,
    select_primary,
    select_primary_map,
)
from dataplatform.logging import get_logger
from dataplatform.query.errors import QueryError
from dataplatform.query.screen import Filter, PitFundamentals, run_screen
from dataplatform.query.shapes import (
    AdjustedPoint,
    AdjustedSeries,
    AdjustedSeriesRequest,
    CrossSection,
    CrossSectionRequest,
)
from dataplatform.query.universe import (
    ListingCalendar,
    PitUniverse,
    index_membership_asof,
    pit_universe,
)
from dataplatform.store.l2 import (
    AdjustedBar,
    open_connection,
    read_adjusted,
    register_adjusted_view,
    register_raw_view,
)

__all__ = ["QueryError", "QueryService"]  # QueryError re-exported from errors (see that module)

_LOG = get_logger(__name__)

#: The DuckDB view names the service registers on its connection. Private to the service — a
#: consumer reads through the typed methods, never the raw relations.
_ADJUSTED_VIEW = "l2_prices_adjusted"
_RAW_VIEW = "l1_prices_raw"


@dataclass(frozen=True, slots=True)
class _Listing:
    """An L2 adjusted bar wearing the three fields M3.2's `canonical_daily` dedups on.

    `canonical_daily` is generic over anything with `isin`/`exchange`/`trade_date` (its
    `ListingKeyed` protocol) and carries the whole row through untouched. An `AdjustedBar` stores
    its exchange as a plain string, so this adapter parses it to the `Exchange` enum the dedup
    compares on and keeps the original bar in `bar` — so the dedup logic is reused, never copied.
    """

    isin: str
    trade_date: date
    exchange: Exchange
    bar: AdjustedBar


class QueryService:
    """The internal point-in-time query API over the L1/L2 Parquet lake (§4.5).

    What it does: answer the canonical query shapes (M4.1: adjusted series and cross-section) over
    DuckDB views of the lake, returning typed, primary-deduped results.
    What it assumes: L2 `prices_adjusted` has been materialized for the ISINs queried (M2.5), and
    L1 `prices_raw` is present when a primary must be derived (i.e. no primary was supplied). It
    reads a consistent snapshot; a materialize running concurrently is the caller's to serialize.
    What it never does: fetch, write, or adjust. It is a read layer; every value it returns is
    already derived and recomputable from L0 (invariant #1/#3).

    A single connection is registered with views over the whole lake once, then reused across calls,
    so a consumer that asks many questions pays the view-registration cost once. Use it as a context
    manager, or call `close()`, to release the connection it owns.
    """

    def __init__(
        self,
        *,
        data_root: Path | None = None,
        con: duckdb.DuckDBPyConnection | None = None,
        primary_rule: PrimaryRule | None = None,
    ) -> None:
        """Open (or adopt) a DuckDB connection and register views over L1 and L2.

        `data_root` overrides the configured lake root (tests point it at a scratch tree). `con`
        lets a caller share one connection across query and materialize; when omitted it owns a
        fresh in-memory connection and closes it on exit. `primary_rule` is the liquidity rule used
        when a primary must be derived (defaults to M3.2's `PrimaryRule()`).
        """
        self._data_root = data_root
        self._rule = primary_rule or PrimaryRule()
        self._owns_con = con is None
        self._con = open_connection() if con is None else con
        register_adjusted_view(self._con, view=_ADJUSTED_VIEW, data_root=data_root)
        register_raw_view(self._con, view=_RAW_VIEW, data_root=data_root)

    def __enter__(self) -> QueryService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection if this service opened it; a no-op for an adopted one."""
        if self._owns_con:
            self._con.close()

    # ── shape (a): adjusted OHLCV series per ISIN across years ─────────────────────────────────

    def adjusted_series(self, request: AdjustedSeriesRequest) -> AdjustedSeries:
        """One ISIN's back-adjusted OHLCV across a window, deduped to its primary exchange.

        Reads the ISIN's L2 partition, keeps the bars inside `[start, end]`, and — because an ISIN
        can trade on both venues — collapses each session to its primary exchange's bar, falling
        back to the other venue's bar (flagged) when the primary did not print. The primary is the
        request's if pinned, else derived from L1 liquidity as of the window's end (invariant #7:
        only sessions on or before that date can decide it). An ISIN with no materialized L2, or
        none in the window, yields an empty series — a gap for the caller to explain, not an error.
        """
        bars = self._read_series_bars(request)
        venues = {_exchange(bar.exchange) for bar in bars}
        if request.primary is not None:
            primary = request.primary
        elif len(venues) == 1:
            # One venue printed every bar in the window: there is nothing to choose between, so
            # no liquidity decision is needed — and none may be possible, because a survivor's
            # stitched L2 history sits under ISINs L1 files elsewhere (`_single_venue_primaries`).
            (primary,) = venues
        else:
            as_of = self._series_as_of(request, bars)
            primary = self._derive_primary_for_isin(request.isin, as_of=as_of)
        canon = self._dedup(bars, {request.isin: primary})
        points = tuple(self._to_point(c) for c in canon)
        first = points[0].trade_date if points else None
        last = points[-1].trade_date if points else None
        _LOG.info(
            "query.adjusted_series",
            isin=request.isin,
            primary=primary.value,
            points=len(points),
            first=first.isoformat() if first else None,
            last=last.isoformat() if last else None,
        )
        return AdjustedSeries(
            isin=request.isin, primary=primary, points=points, first=first, last=last
        )

    # ── shape (b): full-market cross-section for a date ────────────────────────────────────────

    def cross_section(self, request: CrossSectionRequest) -> CrossSection:
        """The whole market's adjusted bars for one session, both exchanges deduped to primary.

        Reads every ISIN's L2 bar for `trade_date`, then collapses each ISIN's NSE/BSE bars to one —
        the primary's, or the other venue's (flagged) when the primary was dark that session — so
        the result is exactly one row per ISIN that traded, never a double count. The primary map is
        `request`'s if supplied (the day's map M3.2 already builds — pass it to skip the scan), else
        derived from L1 liquidity as of `trade_date`.
        """
        bars = self._read_cross_section_bars(request.trade_date)
        if request.primary_by_isin is not None:
            primary_by_isin: Mapping[str, Exchange] = request.primary_by_isin
        else:
            derived = self._derive_primary_map(
                as_of=request.trade_date, isins=frozenset(b.isin for b in bars)
            )
            primary_by_isin = {**_single_venue_primaries(bars), **derived}
        canon = self._dedup(bars, primary_by_isin)
        rows = tuple(self._to_point(c) for c in canon)
        _LOG.info(
            "query.cross_section",
            trade_date=request.trade_date.isoformat(),
            isins=len(rows),
            fell_back=sum(1 for r in rows if r.fell_back),
        )
        return CrossSection(trade_date=request.trade_date, rows=rows)

    # ── shape (c): screen-style filters over a joined cross-section ─────────────────────────────

    def screen(
        self,
        trade_date: date,
        screen: Filter,
        *,
        primary_by_isin: Mapping[str, Exchange] | None = None,
        flows: Mapping[str, Mapping[str, Decimal]] | None = None,
        fundamentals: PitFundamentals | None = None,
        universe: frozenset[str] | None = None,
    ) -> frozenset[str]:
        """Screen the `trade_date` cross-section, joined to flows/fundamentals — shape (c) (§4.5).

        Fetches the day's cross-section (shape (b)) through this same service, joins the per-ISIN
        flow metrics (`flows`) and PIT fundamentals (`fundamentals`) onto it, optionally narrows to
        a `universe` (e.g. a shape-(d) PIT universe), and returns the ISINs the composed `screen`
        filter passes. `fundamentals` is a `PitFundamentals` — the join surface structurally cannot
        take a restated source (invariant #8; see `screen.PitFundamentals`).
        """
        cross = self.cross_section(
            CrossSectionRequest(
                trade_date=trade_date,
                primary_by_isin=dict(primary_by_isin) if primary_by_isin is not None else None,
            )
        )
        return run_screen(cross, screen, flows=flows, fundamentals=fundamentals, universe=universe)

    # ── shape (d): point-in-time universe as of a historical date ──────────────────────────────

    def pit_universe(
        self,
        as_of: date,
        calendar: ListingCalendar,
        *,
        index_slugs: Sequence[str] = (),
    ) -> PitUniverse:
        """The point-in-time universe as of `as_of` — shape (d) (§4.5); kills survivorship bias.

        Keeps every ISIN the injected `calendar` says was listed and not yet delisted on `as_of`
        (so later-delisted names stay in and not-yet-listed names stay out), intersected — when
        `index_slugs` is given — with the index membership in force then (read from M3.9's
        constituent history through this service's `data_root`, never today's list). The listing
        calendar is injected because listing status lives in the identity master, not the Parquet
        lake `QueryService` reads; `universe.store_listing_calendar` adapts the production store.
        """
        membership = (
            index_membership_asof(index_slugs, as_of, data_root=self._data_root)
            if index_slugs
            else None
        )
        return pit_universe(as_of, calendar, index_membership=membership, index_slugs=index_slugs)

    # ── internals: reads ───────────────────────────────────────────────────────────────────────

    def _read_series_bars(self, request: AdjustedSeriesRequest) -> tuple[AdjustedBar, ...]:
        """Read one ISIN's L2 bars inside the request window; empty tuple if it was never built."""
        try:
            bars = read_adjusted(request.isin, con=self._con, data_root=self._data_root)
        except FileNotFoundError:
            return ()
        return tuple(
            bar
            for bar in bars
            if (request.start is None or bar.trade_date >= request.start)
            and (request.end is None or bar.trade_date <= request.end)
        )

    def _read_cross_section_bars(self, trade_date: date) -> tuple[AdjustedBar, ...]:
        """Read every ISIN's L2 adjusted bar for one session, through the registered L2 view."""
        # _ADJUSTED_VIEW is a fixed private identifier, never caller input; trade_date is bound.
        rows = self._con.execute(
            f"SELECT isin, exchange, trade_date, adj_open, adj_high, adj_low, adj_close, "
            f"adj_volume, tr_close, cum_price_factor, cum_qty_factor "
            f"FROM {_ADJUSTED_VIEW} WHERE trade_date = $trade_date "
            f"ORDER BY isin, exchange",
            {"trade_date": trade_date},
        ).fetchall()
        return tuple(_bar_from_row(row) for row in rows)

    def _series_as_of(self, request: AdjustedSeriesRequest, bars: Sequence[AdjustedBar]) -> date:
        """The date the ISIN's primary is decided as of for a series query.

        The window's `end` when one is given (the caller asked "as this security stood then"); else
        the latest session actually in the series, so the primary reflects where it most recently
        traded. Falls back to the window start when the series is empty, and finally to today — but
        an empty series never reaches the primary derivation, so the last case is a total order.
        """
        if request.end is not None:
            return request.end
        if bars:
            return max(bar.trade_date for bar in bars)
        return request.start if request.start is not None else date.max

    # ── internals: primary derivation (reuses M3.2) ────────────────────────────────────────────

    def _derive_primary_for_isin(self, isin: str, *, as_of: date) -> Exchange:
        """Derive one ISIN's primary exchange from L1 liquidity as of `as_of` (reuses M3.2).

        Raises `QueryError` when L1 holds no session for the ISIN on or before `as_of`, because a
        primary cannot be invented (M3.2's `NoLiquidityError`) and the caller must know the request
        was unanswerable rather than silently defaulted.
        """
        observations = self._liquidity(as_of=as_of, isin=isin)
        try:
            decision = select_primary(isin, observations, as_of=as_of, rule=self._rule)
        except NoLiquidityError as error:
            raise QueryError(
                f"cannot derive a primary exchange for {isin!r} as of {as_of.isoformat()}: "
                f"no L1 liquidity in the window. Pin `primary=` or backfill L1. ({error})"
            ) from error
        return decision.primary

    def _derive_primary_map(self, *, as_of: date, isins: frozenset[str]) -> dict[str, Exchange]:
        """Derive the primary exchange for every ISIN in the cross-section from L1 liquidity.

        Reuses M3.2's `select_primary_map` over the liquidity window ending at `as_of`. Every ISIN
        with an L2 bar on `as_of` printed at least one session in the window, so it qualifies; one
        the map still cannot place (L1 gap) surfaces later as a `QueryError` from the dedup rather
        than being dropped from the cross-section silently.
        """
        observations = self._liquidity(as_of=as_of, isins=isins)
        decisions = select_primary_map(observations, as_of=as_of, rule=self._rule)
        return {isin: decision.primary for isin, decision in decisions.items()}

    def _liquidity(
        self,
        *,
        as_of: date,
        isin: str | None = None,
        isins: frozenset[str] | None = None,
    ) -> tuple[DailyLiquidity, ...]:
        """Read per-(isin, exchange, session) liquidity from L1 over the primary window to as_of.

        The window is the last `rule.lookback` observed trading dates on or before `as_of` — read
        here from L1 so only those dates' rows are scanned, not the whole history — and turnover
        (`total_traded_value`) is the metric M3.2 defaults to. Restricted to one `isin`, or a set of
        `isins`, or the whole market. Returns an empty tuple when L1 has no session in range.
        """
        window = self._window_dates(as_of)
        if not window:
            return ()
        params: dict[str, object] = {"dates": list(window)}
        where = ["trade_date IN (SELECT UNNEST($dates))"]
        if isin is not None:
            where.append("isin = $isin")
            params["isin"] = isin
        if isins is not None:
            where.append("isin IN (SELECT UNNEST($isins))")
            params["isins"] = sorted(isins)
        clause = " AND ".join(where)
        # _RAW_VIEW is a fixed private identifier; `clause` is built only from these bound params.
        rows = self._con.execute(
            f"SELECT isin, exchange, trade_date, total_traded_value, total_traded_qty, "
            f"total_trades FROM {_RAW_VIEW} WHERE {clause}",
            params,
        ).fetchall()
        return tuple(
            DailyLiquidity(
                isin=str(row[0]),
                exchange=_exchange(str(row[1])),
                trade_date=row[2],
                turnover=Decimal(row[3]),
                volume=int(row[4]),
                trades=int(row[5]),
            )
            for row in rows
        )

    def _window_dates(self, as_of: date) -> list[date]:
        """The last `rule.lookback` distinct L1 trading dates on/before `as_of` (invariant #7)."""
        # _RAW_VIEW is a fixed private identifier; as_of and lookback are bound parameters.
        rows = self._con.execute(
            f"SELECT DISTINCT trade_date FROM {_RAW_VIEW} "
            f"WHERE trade_date <= $as_of ORDER BY trade_date DESC LIMIT $lookback",
            {"as_of": as_of, "lookback": self._rule.lookback},
        ).fetchall()
        return [row[0] for row in rows]

    # ── internals: dedup (reuses M3.2 canonical_daily) ─────────────────────────────────────────

    def _dedup(
        self, bars: Iterable[AdjustedBar], primary_by_isin: Mapping[str, Exchange]
    ) -> tuple[Canonical[_Listing], ...]:
        """Collapse both exchanges' L2 bars to one per (isin, date) via M3.2's `canonical_daily`.

        Wraps each `AdjustedBar` in a `_Listing` (its exchange parsed to the enum), then hands the
        lot to `canonical_daily` — so the dedup, the fallback and the "keep both raw rows" contract
        are M3.2's, not a second implementation. A `ValueError` from `canonical_daily` (an ISIN with
        no primary) is re-raised as a `QueryError` naming the fix.
        """
        listings = [
            _Listing(
                isin=bar.isin,
                trade_date=bar.trade_date,
                exchange=_exchange(bar.exchange),
                bar=bar,
            )
            for bar in bars
        ]
        try:
            return canonical_daily(listings, primary_by_isin)
        except ValueError as error:
            raise QueryError(str(error)) from error

    @staticmethod
    def _to_point(canon: Canonical[_Listing]) -> AdjustedPoint:
        """Turn one deduped `Canonical` into the typed `AdjustedPoint` a consumer receives."""
        bar = canon.row.bar
        return AdjustedPoint(
            isin=canon.isin,
            trade_date=canon.trade_date,
            exchange=canon.exchange,
            primary=canon.primary,
            fell_back=canon.fell_back,
            adj_open=bar.adj_open,
            adj_high=bar.adj_high,
            adj_low=bar.adj_low,
            adj_close=bar.adj_close,
            adj_volume=bar.adj_volume,
            tr_close=bar.tr_close,
            cum_price_factor=bar.cum_price_factor,
            cum_qty_factor=bar.cum_qty_factor,
        )


def _single_venue_primaries(bars: Iterable[AdjustedBar]) -> dict[str, Exchange]:
    """The primary of every ISIN whose bars in this set come from exactly one exchange: that one.

    Why this exists: `_derive_primary_map` decides a primary from L1 liquidity *read by ISIN*, and a
    survivor's stitched L2 history (D2 lineage, M2.5) carries bars on sessions L1 files under the
    ISINs it retired. On such a session the liquidity scan finds nothing for the survivor, the map
    has no entry, and `canonical_daily` refuses the whole cross-section — measured 2026-09-07 on the
    server: GOLDIAM (`INE025B01025`) on 2017-10-03 took the ten-year backtest down with it. Where
    only one venue printed there is nothing to decide, so that venue is the primary; a liquidity
    decision, where one exists, still wins (the caller merges it over this map).
    """
    venues: dict[str, set[Exchange]] = {}
    for bar in bars:
        venues.setdefault(bar.isin, set()).add(_exchange(bar.exchange))
    return {isin: next(iter(seen)) for isin, seen in venues.items() if len(seen) == 1}


def _bar_from_row(row: Sequence[object]) -> AdjustedBar:
    """Build an `AdjustedBar` from an L2 adjusted-view SELECT row, in the column order queried."""
    return AdjustedBar(
        isin=str(row[0]),
        exchange=str(row[1]),
        trade_date=row[2],  # type: ignore[arg-type]
        adj_open=row[3],  # type: ignore[arg-type]
        adj_high=row[4],  # type: ignore[arg-type]
        adj_low=row[5],  # type: ignore[arg-type]
        adj_close=row[6],  # type: ignore[arg-type]
        adj_volume=row[7],  # type: ignore[arg-type]
        tr_close=row[8],  # type: ignore[arg-type]
        cum_price_factor=row[9],  # type: ignore[arg-type]
        cum_qty_factor=row[10],  # type: ignore[arg-type]
    )


def _exchange(value: str) -> Exchange:
    """Parse a stored exchange string to the `Exchange` enum, failing loud on an unknown venue."""
    try:
        return Exchange(value)
    except ValueError as error:
        raise IdentityError(  # a value that is not NSE/BSE cannot be a listing's exchange
            f"unknown exchange {value!r} in the lake; expected one of "
            f"{', '.join(e.value for e in Exchange)}"
        ) from error
