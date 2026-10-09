"""A10 · M17.9 — what the screens, the dossier and the base-rate table read, every record dated.

The sheets (M17.1) and the shortlist (M17.2) read through :class:`~analyst.commons.sheets.
CommonsSource`. M17.9 reads more of the lake: adjusted OHLCV rather than closes alone, bulk and
block deals, the F&O aggregates, the price-band list, corporate actions due, and any XBRL concept
by name. :class:`ScreenSource` is that wider protocol. `analyst.commons.sources.LakeCommonsSource`
implements it over the lake; the tests implement it over a synthetic world.

Every answer is a :class:`~dataplatform.query.Dataset` that declares how each record's knowable
date is read, so the builders can push it through the PIT guard as of the session. A read with
nothing to return raises :class:`~analyst.commons.sheets.SourceUnavailableError`; it never
returns an empty dataset that stands for "missing".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from analyst.commons.digests import AnnouncementText
from analyst.commons.sheets import EquityBar, FilingFact, IndexLevel, SurveillanceEntry
from dataplatform.query import Dataset

__all__ = [
    "CorporateActionNotice",
    "DealRecord",
    "FoReading",
    "PriceBandEntry",
    "PriceBar",
    "ScreenSource",
]


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One NSE session of one name: split/bonus-adjusted OHLCV plus the raw facts beside it.

    ``high``, ``low``, ``close`` and ``volume`` are adjusted (L2 where the name has a factor
    chain, else the raw bar, which *is* its adjusted bar). ``deliv_qty`` is adjusted the same way
    as ``volume``. ``raw_close``, ``traded_value`` and ``size`` (raw close x raw quantity, the
    M13 size measure) are the exchange's own numbers, never adjusted.
    """

    isin: str
    trade_date: date
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    raw_close: Decimal
    traded_value: Decimal
    size: Decimal
    deliv_qty: Decimal | None
    deliv_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class DealRecord:
    """One NSE bulk or block deal from L1 ``deals``, knowable on its trade date."""

    isin: str
    deal_type: str  # BULK or BLOCK
    trade_date: date
    client_name: str
    side: str  # BUY or SELL
    quantity: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class FoReading:
    """One session's L2 ``fo_aggregates`` row for a stock underlier, keyed by its ISIN."""

    isin: str
    trade_date: date
    spot: Decimal
    total_oi: int
    total_oi_change: int
    pcr_oi: Decimal | None
    rollover_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class PriceBandEntry:
    """One ISIN's operative price band from the NSE ``sec_list`` dated ``knowable_date``.

    ``band_pct`` is ``None`` for "No Band" (the F&O names). The symbol in the file is resolved
    to its ISIN through that session's own bhavcopy statement, never joined on.
    """

    isin: str
    series: str
    band_pct: Decimal | None
    knowable_date: date


@dataclass(frozen=True, slots=True)
class CorporateActionNotice:
    """One corporate action the exchange broadcast, with the dates it falls due."""

    isin: str
    purpose: str
    ex_date: date | None
    record_date: date | None
    knowable_date: date


@runtime_checkable
class ScreenSource(Protocol):
    """Where the M17.9 builders read the lake. Every answer is a dated :class:`Dataset`."""

    def sessions(self, through: date, count: int) -> Dataset[date]:
        """The last ``count`` NSE sessions on or before ``through``, ascending."""

    def equity_bars(self, sessions: Sequence[date], series: str) -> Dataset[EquityBar]:
        """Every raw NSE bar in ``series`` on ``sessions`` (the sheets' read)."""

    def price_bars(
        self, isins: frozenset[str], sessions: Sequence[date], series: str
    ) -> Dataset[PriceBar]:
        """Adjusted OHLCV bars in ``series`` for ``isins`` on ``sessions``."""

    def index_levels(self, series_ids: Sequence[str], through: date) -> Dataset[IndexLevel]:
        """Published closes of ``series_ids`` for sessions on or before ``through``."""

    def filings(self, through: date) -> Dataset[FilingFact]:
        """Every company-level fact of the metrics' concepts filed on or before ``through``."""

    def concept_facts(self, concepts: frozenset[str], through: date) -> Dataset[FilingFact]:
        """Every company-level fact of ``concepts`` filed on or before ``through``."""

    def announcement_texts(self, start: date, through: date) -> Dataset[AnnouncementText]:
        """Every NSE announcement knowable in ``[start, through]``, with its text."""

    def surveillance(self, stage: str, through: date) -> Dataset[SurveillanceEntry]:
        """The latest ``stage`` list (ASM/GSM/ESM) dated on or before ``through``."""

    def price_bands(self, through: date) -> Dataset[PriceBandEntry]:
        """The newest NSE price-band list dated on or before ``through``."""

    def deals(self, start: date, through: date) -> Dataset[DealRecord]:
        """Every bulk and block deal traded in ``[start, through]``."""

    def fo_readings(self, sessions: Sequence[date]) -> Dataset[FoReading]:
        """The F&O aggregates of every stock underlier on ``sessions``."""

    def corporate_actions(self, start: date, through: date) -> Dataset[CorporateActionNotice]:
        """Every corporate-action broadcast knowable in ``[start, through]``."""
