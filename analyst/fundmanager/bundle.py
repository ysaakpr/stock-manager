"""A10 · M17.4 — what one manager is shown: its own book, the fulfilled research, the cost hurdles.

These are the value types the runtime assembles and the renderer and the contract read.

**The book view has no cost basis and no P&L, by construction** (Amendment 1 (d)). A `Holding` has
no field an entry price or a profit could live in: weight, sessions held, the opening thesis, its
invalidation conditions and their status, the current stop, evidence since entry and any forced
review. The stop is held as a price (that is what M17.7 executes) but is only ever *rendered* as its
distance below today's close, because a stop price beside the stop percentage set at entry would let
the entry price be recomputed. ``ManagerBook.nav`` exists only to size a BUY's round trip; it is
never rendered either, since the book's value against its opening capital is the book's P&L.

**The round trip** of a name is the one shared cost model (`execution.costs`, invariant #4) on a
buy and a sell of the same whole-share quantity at the session's close, plus the SimBroker
participation-scaled slippage (`execution.sim_broker.SlippageModel`, its defaults) on each side
against the name's median traded value. That is what the paper book will actually pay, so it is
what a BUY's expected excess must clear.

What this module never does: read a clock, a price outside what it is handed, or anything about
another manager's book.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Final

from analyst.commons import Dossier, FilingDigest, Snapshot, UniverseRow
from analyst.fundmanager.schemas import QueryItem, ResearchItem
from execution.costs import CostModel, Side, Trade
from execution.sim_broker import SlippageModel

__all__ = [
    "UNRANKED_TIER",
    "CostHurdle",
    "Holding",
    "InvalidationStatus",
    "ManagerBook",
    "ResearchBundle",
    "RoundTrip",
    "Unfulfilled",
    "round_trip",
    "tier_hurdles",
]

#: The tier key of a name with no cap tier (past size rank 500), as the base-rate table names it.
UNRANKED_TIER: Final = "unranked"
_TIERS: Final = ("large", "mid", "small", UNRANKED_TIER)
_HUNDRED: Final = Decimal(100)
_BPS: Final = Decimal(10_000)
_Q4: Final = Decimal("0.0001")
_ZERO: Final = Decimal(0)


# ── the manager's own book ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InvalidationStatus:
    """One invalidation condition set at entry, and whether it has been hit."""

    condition: str
    status: str


@dataclass(frozen=True, slots=True)
class Holding:
    """One position as its manager sees it: no cost price, no profit or loss (module docstring).

    ``weight_pct`` is the position's share of the book at today's close, in percentage points.
    ``stop_price`` is the mechanical stop M17.7 executes, or None if none is set.
    """

    isin: str
    sector: str | None
    weight_pct: Decimal
    sessions_held: int
    opening_thesis: str
    invalidation: tuple[InvalidationStatus, ...]
    stop_price: Decimal | None
    evidence_since_entry: tuple[str, ...] = ()
    forced_review: str | None = None

    def __post_init__(self) -> None:
        if self.weight_pct < _ZERO or self.sessions_held < 0:
            raise ValueError(f"{self.isin}: a holding's weight and sessions held are non-negative")
        if self.stop_price is not None and self.stop_price <= _ZERO:
            raise ValueError(f"{self.isin}: a stop price must be positive")


@dataclass(frozen=True, slots=True)
class ManagerBook:
    """One manager's book as of the session close: its holdings, its cash share, its value.

    ``nav`` sizes a BUY's round trip and is never rendered (module docstring).
    """

    book_id: str
    nav: Decimal
    cash_pct: Decimal
    holdings: tuple[Holding, ...] = ()

    def __post_init__(self) -> None:
        if self.nav <= _ZERO:
            raise ValueError(f"{self.book_id}: a book's value must be positive")
        isins = [h.isin for h in self.holdings]
        if len(set(isins)) != len(isins):
            raise ValueError(f"{self.book_id}: a holding is listed twice")

    def holding(self, isin: str) -> Holding | None:
        return next((h for h in self.holdings if h.isin == isin), None)

    @property
    def isins(self) -> tuple[str, ...]:
        return tuple(h.isin for h in self.holdings)


# ── the fulfilled research ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Unfulfilled:
    """A research request the harness could not fulfil, and why. Shown to the manager."""

    what: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResearchBundle:
    """What one fulfilment round put in front of the manager."""

    round: int
    requests: tuple[ResearchItem, ...]
    queries: tuple[QueryItem, ...]
    dossiers: tuple[Dossier, ...]
    digests: tuple[FilingDigest, ...]
    snapshots: tuple[tuple[QueryItem, Snapshot], ...]
    unfulfilled: tuple[Unfulfilled, ...]


# ── the cost of a round trip ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """A buy and a sell of ``quantity`` shares at ``price``: charges, slippage, and their total.

    ``charges_pct``, ``slippage_pct`` and ``total_pct`` are percentage points of the turnover.
    """

    quantity: int
    turnover: Decimal
    charges_pct: Decimal
    slippage_bps_per_side: Decimal
    slippage_pct: Decimal
    total_pct: Decimal


def round_trip(
    *,
    isin: str,
    session: date,
    price: Decimal,
    notional: Decimal,
    median_traded_value: Decimal,
    cost_model: CostModel,
    slippage: SlippageModel,
) -> RoundTrip | None:
    """The round-trip cost of a ``notional`` order in ``isin`` at ``price`` on ``session``.

    What it does: floors ``notional / price`` to whole shares, charges a buy and a sell of them on
    the shared cost model, and adds the participation-scaled slippage on both sides.
    What it assumes: ``median_traded_value`` is the name's own 20-session median, the session's
    liquidity the slippage model scales against.
    What it never does: invent a cost. ``None`` when the order buys no whole share.
    """
    if price <= _ZERO or median_traded_value <= _ZERO:
        return None
    quantity = int((notional / price).to_integral_value(rounding=ROUND_FLOOR))
    if quantity < 1:
        return None
    buy = cost_model.charge(Trade(isin, session, Side.BUY, quantity, price))
    sell = cost_model.charge(Trade(isin, session, Side.SELL, quantity, price))
    turnover = buy.turnover
    bps = slippage.bps_for(order_turnover=turnover, traded_value=median_traded_value)
    charges_pct = (buy.total + sell.total) / turnover * _HUNDRED
    slippage_pct = 2 * bps / _BPS * _HUNDRED
    return RoundTrip(
        quantity=quantity,
        turnover=turnover,
        charges_pct=charges_pct.quantize(_Q4, rounding=ROUND_HALF_EVEN),
        slippage_bps_per_side=bps.quantize(_Q4, rounding=ROUND_HALF_EVEN),
        slippage_pct=slippage_pct.quantize(_Q4, rounding=ROUND_HALF_EVEN),
        total_pct=(charges_pct + slippage_pct).quantize(_Q4, rounding=ROUND_HALF_EVEN),
    )


@dataclass(frozen=True, slots=True)
class CostHurdle:
    """The round trip of a typical position in one liquidity tier, as the prompt shows it."""

    tier: str
    names: int
    median_traded_value: Decimal
    price: Decimal
    notional: Decimal
    trip: RoundTrip | None

    @property
    def field_id(self) -> str:
        return f"cost.{self.tier}"


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def tier_hurdles(
    rows: Sequence[UniverseRow],
    *,
    session: date,
    notional: Decimal,
    cost_model: CostModel,
    slippage: SlippageModel,
) -> tuple[CostHurdle, ...]:
    """Each tier's round trip for a ``notional`` order in its median name.

    The median name of a tier has the tier's median close and median traded value; tiers with no
    universe name are omitted. A typical position is the caller's choice (the runtime uses one
    equal-weight slot of the book: its value over its maximum positions).
    """
    out: list[CostHurdle] = []
    for tier in _TIERS:
        members = [r for r in rows if (r.cap_tier or UNRANKED_TIER) == tier]
        if not members:
            continue
        price = _median([r.close for r in members])
        traded = _median([r.median_traded_value for r in members])
        trip = round_trip(
            isin=members[0].isin,
            session=session,
            price=price,
            notional=notional,
            median_traded_value=traded,
            cost_model=cost_model,
            slippage=slippage,
        )
        out.append(
            CostHurdle(
                tier=tier,
                names=len(members),
                median_traded_value=traded,
                price=price,
                notional=notional,
                trip=trip,
            )
        )
    return tuple(out)
