"""D4 query shapes — the typed request/response models the query service speaks in (M4.1).

§4.5 names four canonical query shapes the internal API must serve efficiently; this module is the
vocabulary for the first two, which M4.1 owns:

* **(a) adjusted OHLCV series per ISIN across years** — `AdjustedSeriesRequest` → `AdjustedSeries`.
  One security's back-adjusted daily bars over a date window, deduped to its primary exchange.
* **(b) full-market cross-section for a date** — `CrossSectionRequest` → `CrossSection`. One
  adjusted bar per ISIN for a single session, both exchanges deduped to the primary listing.

Both answer in `AdjustedPoint`s: a canonical, primary-deduped adjusted bar. It carries the L2
adjusted values M2.5 materialized (`raw x cumulative factor`, invariant #3 — never a raw price in
disguise), plus the two facts a caller needs to trust the dedup: `primary` (the ISIN's primary
exchange for the query), and `fell_back` (the primary did not print that session, so the other
exchange's bar is shown instead — a visible fact, never a silent swap, exactly as M3.2's
`canonical_daily` records it).

These are plain frozen pydantic models — no I/O, no DuckDB — because they cross the module boundary
between the query service and its consumers (A3, A5, A6, X2, D6), and the "no bare dicts as an
interface" rule (CLAUDE.md) makes that boundary a typed contract. Money is `Decimal` throughout; a
`float` in an adjusted price would be a bug. The service in `service.py` is the only thing that
builds them, from Parquet through DuckDB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import ISIN_PATTERN

__all__ = [
    "AdjustedPoint",
    "AdjustedSeries",
    "AdjustedSeriesRequest",
    "CrossSection",
    "CrossSectionRequest",
]


class AdjustedPoint(BaseModel):
    """One canonical, primary-deduped adjusted bar — the atom both query shapes return.

    What it does: carry an L2 `prices_adjusted` row (the adjusted OHLC, adjusted volume, the
    total-return close and the two cumulative factors M2.5 wrote) with which `exchange`'s row it is,
    the ISIN's `primary` exchange for the query, and whether the primary was absent that session so
    the other exchange's bar `fell_back` in.
    What it assumes: the values came from L2, so they are recomputable from L1 + the M2.4 factor
    chain and nothing here is primary data.
    What it never does: hold a raw (unadjusted) price. Every price column is an adjusted one — this
    is a view onto L2, where invariant #3 says adjusted series belong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    trade_date: date
    exchange: Exchange = Field(description="the exchange whose bar this row carries")
    primary: Exchange = Field(description="the ISIN's primary exchange for this query")
    fell_back: bool = Field(
        description="the primary did not print this session; the other exchange's bar is shown"
    )
    adj_open: Decimal = Field(gt=0)
    adj_high: Decimal = Field(gt=0)
    adj_low: Decimal = Field(gt=0)
    adj_close: Decimal = Field(gt=0, description="split/bonus-adjusted close; no dividend effect")
    adj_volume: Decimal = Field(ge=0, description="total_traded_qty x cumulative qty factor")
    tr_close: Decimal = Field(gt=0, description="total-return close: adj_close with dividends in")
    cum_price_factor: Decimal = Field(gt=0)
    cum_qty_factor: Decimal = Field(gt=0)


class AdjustedSeriesRequest(BaseModel):
    """Ask for one ISIN's adjusted OHLCV series across a date window — query shape (a) (§4.5).

    What it does: name the `isin` and an optional inclusive `[start, end]` window (omit either bound
    to run open-ended). `primary` optionally pins the exchange to dedup to; when omitted the service
    derives it from L1 liquidity as of the window's end, reusing M3.2's `select_primary`.
    What it never does: take a symbol. The join key is the ISIN (invariant #2); a series request
    keyed on a ticker would be a bug.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    start: date | None = Field(default=None, description="inclusive lower bound; None = open")
    end: date | None = Field(default=None, description="inclusive upper bound; None = open")
    primary: Exchange | None = Field(
        default=None, description="pin the primary exchange; None = derive from L1 liquidity"
    )

    @model_validator(mode="after")
    def _window_non_empty(self) -> AdjustedSeriesRequest:
        """A window whose start is after its end names no session — reject it at construction."""
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(
                f"empty window: start {self.start.isoformat()} > end {self.end.isoformat()}"
            )
        return self


class AdjustedSeries(BaseModel):
    """One ISIN's adjusted series over a window — the response to shape (a).

    `points` are in `trade_date` order, one per session in the window (deduped to `primary`).
    `first`/`last` bound the returned series and are `None` when it is empty (the ISIN had no L2 bar
    in the window — a gap for the caller to explain, not an error).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    primary: Exchange
    points: tuple[AdjustedPoint, ...]
    first: date | None = Field(default=None)
    last: date | None = Field(default=None)


class CrossSectionRequest(BaseModel):
    """Ask for the whole market's adjusted bars on one session — query shape (b) (§4.5).

    What it does: name the `trade_date`. `primary_by_isin` optionally supplies the primary exchange
    per ISIN (the day's map M3.2 already computes once per session — pass it to skip re-deriving);
    when omitted the service derives every ISIN's primary from L1 liquidity as of `trade_date`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    primary_by_isin: dict[str, Exchange] | None = Field(
        default=None,
        description="primary exchange per ISIN; None = derive from L1 liquidity as of trade_date",
    )


class CrossSection(BaseModel):
    """One session's full-market cross-section, deduped to primary — the response to shape (b).

    `rows` are in `isin` order, exactly one per ISIN that traded on `trade_date` (on either
    exchange), so the set is complete and free of the double-count a naive union of NSE and BSE
    would carry. `fell_back` on a row flags the ISINs whose primary was dark that session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date
    rows: tuple[AdjustedPoint, ...]

    @property
    def isins(self) -> frozenset[str]:
        """The distinct ISINs in the cross-section — one per row, so also the row count."""
        return frozenset(row.isin for row in self.rows)
