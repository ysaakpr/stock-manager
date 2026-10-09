"""Test support for M17.6: shortlists, a mini roster, an in-memory bench and a deterministic market.

Everything here is offline and deterministic: prices follow a fixed per-name drift, the shortlist
is a fixed rotation, and the bench is a fixed-rate index — so a run is a pure function of its
inputs and two runs produce the same journal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from analyst.commons.shortlist import (
    SHORTLIST_RULE_HASH,
    SHORTLIST_VERSION,
    Shortlist,
    ShortlistEntry,
)
from analyst.fundmanager import Roster, load_roster
from tests.fm_books_support import Bar, FmMarket

_HALF = Decimal("0.5")


def isin(n: int) -> str:
    return f"INE{n:05d}A010"


def shortlist_of(session: date, names: Sequence[str]) -> Shortlist:
    """A digest-consistent shortlist of ``names`` in that order (position 1 first)."""
    entries = tuple(
        ShortlistEntry(
            position=i + 1,
            isin=name,
            momentum_12_1=None,
            relative_strength_20=None,
            earnings_surprise=None,
            log_liquidity=None,
            rank_momentum_12_1=_HALF,
            rank_relative_strength_20=_HALF,
            rank_earnings_surprise=_HALF,
            rank_log_liquidity=_HALF,
            composite=_HALF,
        )
        for i, name in enumerate(names)
    )
    fields = {
        "trading_date": session,
        "build_digest": "0" * 64,
        "shortlist_version": SHORTLIST_VERSION,
        "rule_hash": SHORTLIST_RULE_HASH,
        "universe_size": len(names),
        "coverage": {},
        "entries": entries,
        "gaps": (),
    }
    return Shortlist(
        **fields,  # type: ignore[arg-type]
        shortlist_digest=Shortlist.digest_of(**fields),  # type: ignore[arg-type]
        built_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def mini_roster(manager_id: str = "FM-SWING-10L") -> Roster:
    """The real roster cut to one manager, its control and the bench (a consistent `Roster`)."""
    roster = load_roster()
    books = (roster.get(manager_id), roster.control_for(manager_id), *roster.benches)
    return Roster(preregistration=roster.preregistration, rails=roster.rails, books=books)


def drifting_market(
    sessions: Sequence[date],
    names: Sequence[str],
    *,
    drift: Mapping[str, Decimal],
    start: Decimal = Decimal("100"),
    traded_value: Decimal = Decimal("10000000000"),
    sectors: Mapping[str, str] | None = None,
) -> FmMarket:
    """Each name compounds its daily ``drift`` from ``start``; open = close, in paise."""
    bars: dict[tuple[str, date], Bar] = {}
    for name in names:
        price = start
        for session in sessions:
            bars[(name, session)] = Bar(price, price, traded_value)
            price = (price * (Decimal(1) + drift[name])).quantize(Decimal("0.01"))
    sector_map = (
        dict(sectors) if sectors is not None else {n: f"S{i % 5}" for i, n in enumerate(names)}
    )
    return FmMarket(sessions, bars, sector_map)


class DictBench:
    """A `BenchmarkLevels` over a ``session -> level`` table, from one method."""

    def __init__(self, levels: Mapping[date, Decimal], method: str = "published") -> None:
        self.levels = dict(levels)
        self._method = method

    @property
    def method(self) -> str:
        return self._method

    def level(self, session: date) -> Decimal | None:
        return self.levels.get(session)


def compounding(
    sessions: Sequence[date], rate: Decimal, start: Decimal = Decimal("1000")
) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    level = start
    for session in sessions:
        out[session] = level
        level = (level * (Decimal(1) + rate)).quantize(Decimal("0.0001"))
    return out


class MarketPrices:
    """`OutcomePrices` over an `FmMarket` (closes as adjusted closes) and a `DictBench`."""

    def __init__(self, market: FmMarket, bench: DictBench) -> None:
        self.market = market
        self.bench = bench

    def adjusted_close(self, isin: str, session: date) -> Decimal | None:
        return self.market.close(isin, session)

    def bench_level(self, session: date) -> Decimal | None:
        return self.bench.level(session)
