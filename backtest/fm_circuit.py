"""M17.7 — the upper-circuit fill rule of pre-registration Amendment 1 (e).

A BUY whose fill session opens and stays locked at the upper price band — open = high = low = the
upper band — cannot be filled in practice: every share offered at the band is taken by queued
buyers, and a paper fill at the open would book a trade no real order could have got. Such a buy is
left unfilled (`M17PaperAccount` cancels it before the session's fills) and the book journals
``UNFILLED_UPPER_CIRCUIT``.

**The test** (`upper_circuit_lock`). The session's raw NSE bar must print open = high = low. Then:

* **band known** — the session's operative band from NSE's ``sec_list`` (read as M17.9's Commons
  reads it: `LakeCommonsSource.price_bands`, each row placed on its ISIN by the session's own
  bhavcopy statement). The upper band is ``prev_close * (1 + band / 100)``; NSE rounds band prices
  to the tick, so the open is at the band when it is within one tick (₹0.05) below that figure, or
  above it. A "No Band" name (the F&O names, with no static band) is never locked by this rule.
* **band missing** — no band list readable for the session, the newest list is older than the
  session before the fill session (a list dated the fill session or the one before it is the
  newest that can describe the fill session's band; M17.9's reader searches back 14 days, and a band
  revised inside that window must not be judged on the old one), or the list does not name the ISIN:
  the smallest NSE band (2 %) stands in, so a locked print whose open is at least 2 % above the
  previous close is treated as locked (``basis = SMALLEST_BAND``). That errs towards leaving a buy
  unfilled — the conservative side for a paper book's record — and the journal line says which
  basis was used.

A session with no bar for the name is not judged here: the paper broker rejects a fill it has no
reference bar for anyway.

What this module never does: read a wall clock, move a price, or decide anything about a sell —
an upper-circuit lock does not stop a seller, it is the buyer who cannot be filled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import duckdb

from analyst.commons.sheets import SourceUnavailableError
from analyst.commons.sources import LakeCommonsSource
from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from dataplatform.query.pit import PitContext
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "NSE_TICK",
    "SMALLEST_BAND_PCT",
    "BandReading",
    "CircuitBasis",
    "CircuitCheck",
    "CircuitMarket",
    "LakeCircuitMarket",
    "NoCircuitData",
    "SessionPrint",
    "upper_circuit_lock",
]

_LOG = get_logger(__name__)

#: NSE's equity tick: band prices are rounded to it, so the band test allows one tick below.
NSE_TICK: Final = Decimal("0.05")
#: The smallest NSE price band (per cent): the stand-in when no band is known for the session.
SMALLEST_BAND_PCT: Final = Decimal(2)
_HUNDRED: Final = Decimal(100)
_PRICES_RAW: Final = "prices_raw"


class CircuitBasis(StrEnum):
    """Which band the lock test used."""

    BAND = "BAND"
    SMALLEST_BAND = "SMALLEST_BAND"


@dataclass(frozen=True, slots=True)
class SessionPrint:
    """One name's raw NSE bar on its fill session: the prices the lock test reads."""

    isin: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    prev_close: Decimal

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "prev_close"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal — money is never float (CLAUDE.md)")


@dataclass(frozen=True, slots=True)
class BandReading:
    """The operative band for one name on one session, or that none is known.

    ``known`` False means no band data for the session (the smallest band stands in). ``known``
    True with ``band_pct`` None is NSE's "No Band".
    """

    known: bool
    band_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CircuitCheck:
    """The verdict on one buy's fill session."""

    locked: bool
    basis: CircuitBasis
    threshold: Decimal


class CircuitMarket(Protocol):
    """Where the account reads a fill session's raw bar and band."""

    def session_print(self, isin: str, session: date) -> SessionPrint | None:
        """The raw NSE bar of ``isin`` on ``session``, or None when it did not trade."""
        ...

    def price_band(self, isin: str, session: date) -> BandReading:
        """The operative band of ``isin`` on ``session``."""
        ...


def upper_circuit_lock(bar: SessionPrint, band: BandReading) -> CircuitCheck | None:
    """Whether ``bar`` is locked at the upper band (module docstring); None when it cannot be.

    None for a bar that traded through a range (open, high and low not all equal), a "No Band"
    name, or a non-positive previous close.
    """
    if not (bar.open == bar.high == bar.low) or bar.prev_close <= 0:
        return None
    if band.known:
        if band.band_pct is None:
            return None
        threshold = bar.prev_close * (_HUNDRED + band.band_pct) / _HUNDRED
        return CircuitCheck(
            locked=bar.open >= threshold - NSE_TICK, basis=CircuitBasis.BAND, threshold=threshold
        )
    threshold = bar.prev_close * (_HUNDRED + SMALLEST_BAND_PCT) / _HUNDRED
    return CircuitCheck(
        locked=bar.open >= threshold, basis=CircuitBasis.SMALLEST_BAND, threshold=threshold
    )


class NoCircuitData:
    """A `CircuitMarket` that knows no bar: nothing is ever judged locked.

    For a book whose fills are driven from a market with no raw OHLC (a unit test that is not
    about circuits). Production wires `LakeCircuitMarket`.
    """

    def session_print(self, isin: str, session: date) -> SessionPrint | None:
        del isin, session
        return None

    def price_band(self, isin: str, session: date) -> BandReading:
        del isin, session
        return BandReading(known=False)


class LakeCircuitMarket:
    """`CircuitMarket` over the local lake: L1 ``prices_raw`` and M17.9's band list reader.

    The band list is the newest ``sec_list`` on or before the session (`price_bands`), and only if
    it is dated the session or the session before; a list that is older, or cannot be read, is
    "band missing" for every name that session (the smallest band stands in), logged once.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        data_root: Path | None = None,
        previous_session: Callable[[date], date] | None = None,
    ) -> None:
        self._data_root = data_root
        self._commons = LakeCommonsSource(clock=clock, data_root=data_root)
        self._bands: dict[date, dict[str, Decimal | None] | None] = {}
        self._previous = previous_session or _calendar_previous

    def session_print(self, isin: str, session: date) -> SessionPrint | None:
        path = (
            layer_root(Layer.L1, data_root=self._data_root)
            / _PRICES_RAW
            / f"date={session.isoformat()}"
            / "part.parquet"
        )
        if not path.is_file():
            return None
        rows = duckdb.execute(
            "SELECT open, high, low, prev_close FROM read_parquet($path) "
            "WHERE exchange = 'NSE' AND isin = $isin ORDER BY series = 'EQ' DESC, series",
            {"path": str(path), "isin": isin},
        ).fetchall()
        if not rows or any(value is None for value in rows[0]):
            return None
        open_, high, low, prev_close = (Decimal(value) for value in rows[0])
        return SessionPrint(isin, session, open_, high, low, prev_close)

    def _read_bands(self, session: date) -> dict[str, Decimal | None] | None:
        """``session``'s bands by ISIN, or None when no list fresh enough to describe it exists."""
        try:
            listed = PitContext(session).admit(self._commons.price_bands(session))
        except SourceUnavailableError as exc:
            _LOG.warning(
                "fm_circuit.band_missing",
                session=session.isoformat(),
                detail=str(exc),
                state="SMALLEST_BAND",
            )
            return None
        oldest = self._previous(session)
        if listed and listed[0].knowable_date < oldest:
            _LOG.warning(
                "fm_circuit.band_stale",
                session=session.isoformat(),
                listed=listed[0].knowable_date.isoformat(),
                state="SMALLEST_BAND",
            )
            return None
        bands: dict[str, Decimal | None] = {}
        for entry in listed:
            if entry.series == "EQ" or entry.isin not in bands:
                bands[entry.isin] = entry.band_pct
        return bands

    def price_band(self, isin: str, session: date) -> BandReading:
        if session not in self._bands:
            self._bands[session] = self._read_bands(session)
        bands_today = self._bands[session]
        if bands_today is None or isin not in bands_today:
            return BandReading(known=False)
        return BandReading(known=True, band_pct=bands_today[isin])


def _calendar_previous(day: date) -> date:
    """The NSE session before ``day`` on the checked-in holiday calendar."""
    from dataplatform.ingest.calendar import trading_calendar

    calendar = trading_calendar()
    probe = day
    for _ in range(15):
        probe = date.fromordinal(probe.toordinal() - 1)
        if calendar.is_session(probe):
            return probe
    raise ValueError(f"no NSE session in the 15 days before {day.isoformat()}")
