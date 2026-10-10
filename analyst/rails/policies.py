"""A8: the vocabulary of the rails — the caps, the state they are checked against, and the verdict.

This module is data, not logic. It names the rails (`RailId`), the value objects the engine reads
(`Lot`, `Portfolio`, `ProposedOrder`, `HouseholdExposure`), and the two verdicts the engine
returns (`RailAssessment` for an order, `DrawdownStatus` for the daily monitor). The checking
itself lives in `engine.py`; keeping the shapes here means the property tests can build a portfolio
and assert on a breach without importing the journalling half.

The caps themselves are **not** redefined here. They are `analyst.cases.RiskRails` — §5.2 policy 4,
the object a human ratifies — and A8 reads that and nothing else (invariant #6). A second definition
of "max position %" is exactly the drift `RiskRails._position_cap_must_admit_min_holdings` exists to
prevent, so there is one and A8 imports it.

Two properties this module exists to guarantee:

* **A rail is a comparison, so its inputs are exact.** Every price, value and percentage is
  `Decimal` (CLAUDE.md: a rail that is off by a float epsilon is a rail that did not hold). A
  `float` reaching a value object is rejected at construction, not coerced.
* **Identity is the ISIN.** A `Lot`, an order and a household exposure all key on ISIN (invariant
  #2); nothing here carries a symbol a downstream query could join on.

Nothing here reads a clock, a database or the network, and nothing here can bypass a rail — there
is no override field on any of these objects, by construction (acceptance criterion 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from execution.broker import OrderRequest, Side

__all__ = [
    "BookOrderFacts",
    "BookRails",
    "DrawdownStatus",
    "HouseholdExposure",
    "Lot",
    "Portfolio",
    "ProposedOrder",
    "RailAssessment",
    "RailBreach",
    "RailId",
    "drawdown_of",
    "require_decimal",
]

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)


def require_decimal(name: str, value: object) -> Decimal:
    """Refuse a `float` where an exact decimal is required.

    The same rule the cost model and the policy set apply, applied at the rail boundary: a rail is
    a comparison, and a value that arrived as `0.1 + 0.2` blocks (or passes) an order it should not.
    Money and prices in this system are `Decimal` from the first character (CLAUDE.md).
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name} must be a Decimal, got float {value!r}; a rail that is off by a float epsilon "
            "is a rail that did not hold (CLAUDE.md)"
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    return value


class RailId(StrEnum):
    """The deterministic pre-trade and daily rails of §5.2 / §6 (invariant #6).

    A named value rather than a free-text reason so a `RAIL_BLOCK` journal line records *which*
    rail refused an order in a form the evidence pack can count (§5.7's rail-breach count), not a
    sentence a report has to parse.
    """

    MAX_POSITION = "MAX_POSITION"
    """A single holding exceeds `max_position_pct` of case value."""

    MAX_SECTOR = "MAX_SECTOR"
    """A single sector exceeds `max_sector_pct` of case value."""

    MIN_HOLDINGS = "MIN_HOLDINGS"
    """A sell would concentrate the book below `min_holdings` names."""

    MAX_ORDER_VALUE = "MAX_ORDER_VALUE"
    """One order's rupee value exceeds `max_order_value_inr` — the fat-finger guard."""

    MAX_ORDER_PCT = "MAX_ORDER_PCT"
    """One order's value exceeds `max_order_pct_of_case` of case value."""

    CROSS_CASE_CONCENTRATION = "CROSS_CASE_CONCENTRATION"
    """The household's total exposure to one instrument exceeds `max_position_pct` of household
    value — the concentration neither case's own rails can see (§8.1, decision #4)."""

    DRAWDOWN_REVIEW = "DRAWDOWN_REVIEW"
    """Peak-to-trough fall reached `drawdown_review_pct`. Not an order rail — it forces a review."""

    # ── the M17 book rails (pre-registration §4 step 5); `MAX_POSITION`/`MAX_SECTOR` are shared ──

    MAX_POSITIONS = "MAX_POSITIONS"
    """A buy of a new name would take the book above its mandate's `max_positions` names."""

    PARTICIPATION = "PARTICIPATION"
    """One order's notional exceeds `participation_max_pct` of the name's median traded value
    over the `participation_lookback_sessions` sessions ending at the decision session — or that
    median is not knowable then, which refuses the order rather than guessing a liquidity."""

    MIN_HOLD = "MIN_HOLD"
    """A sell decided fewer than `min_hold_sessions` sessions after the name's last buy fill."""

    NO_SHORT = "NO_SHORT"
    """A sell of more shares than the book holds — a short. Never allowed in an M17 book."""

    NO_FNO = "NO_FNO"
    """A buy of anything but a cash-equity series of the mandate's universe (a future, an option,
    or any other segment). Never allowed in an M17 book."""

    NO_MARGIN = "NO_MARGIN"
    """A buy whose notional exceeds the book's spendable cash — it could only fill on margin."""


@dataclass(frozen=True, slots=True)
class Lot:
    """One settled holding, valued at a reference price — the unit A8 measures concentration in.

    What it does: carry an instrument (by ISIN), its sector, a whole-share quantity and the price
    it is marked at, and expose the rupee `value` a cap is a percentage of.
    What it assumes: `price` is a current reference price (the same one the order carries), so a
    concentration check is against today's value, not a stale cost basis.
    What it never does: hold a fractional share, a negative quantity, or a symbol.
    """

    isin: str
    sector: str
    quantity: int
    price: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise TypeError(f"quantity must be a whole number of shares, got {self.quantity!r}")
        if self.quantity <= 0:
            raise ValueError(f"a lot holds a positive quantity, got {self.quantity}")
        if not self.sector.strip():
            raise ValueError("a lot must name its sector")
        require_decimal("price", self.price)
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")

    @property
    def value(self) -> Decimal:
        """Rupee value at the marked price — what a cap is a fraction of."""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class Portfolio:
    """A case's book at one instant: its lots and its idle cash (§5.5).

    What it does: hold one `Lot` per ISIN and the cash not yet deployed, and answer the questions
    the rails ask — total case value, a named lot, a sector's aggregate value, the holding count.
    What it assumes: at most one lot per ISIN (the identity master's job upstream) and cash that
    is not negative — a rail measures allocation, and a negative-cash book is a margin failure the
    broker refuses before A8 is asked.
    What it never does: value a lot at anything but its own marked price, or key on a symbol.
    """

    case_id: str
    lots: tuple[Lot, ...]
    cash: Decimal

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("a portfolio belongs to a case")
        seen: set[str] = set()
        for lot in self.lots:
            if lot.isin in seen:
                raise ValueError(
                    f"two lots for {lot.isin}: a portfolio holds one lot per ISIN (invariant #2), "
                    "merge them upstream"
                )
            seen.add(lot.isin)
        require_decimal("cash", self.cash)
        if self.cash < _ZERO:
            raise ValueError(f"cash must not be negative, got {self.cash}")

    @property
    def invested(self) -> Decimal:
        """Rupee value of every lot at its marked price."""
        return sum((lot.value for lot in self.lots), _ZERO)

    @property
    def total_value(self) -> Decimal:
        """Case value: deployed plus idle. What every percentage rail is a fraction of."""
        return self.invested + self.cash

    @property
    def holding_count(self) -> int:
        """How many distinct names are held — the number `min_holdings` is a floor on."""
        return len(self.lots)

    def lot(self, isin: str) -> Lot | None:
        """The lot for `isin`, or None if the case does not hold it."""
        for lot in self.lots:
            if lot.isin == isin:
                return lot
        return None

    def sector_value(self, sector: str) -> Decimal:
        """Aggregate rupee value of every lot in `sector`."""
        return sum((lot.value for lot in self.lots if lot.sector == sector), _ZERO)


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    """An order A8 is asked to clear, enriched with what a rail needs to value it.

    What it does: wrap the broker's already-validated `OrderRequest` (whole shares, ISIN, side)
    with the reference `price` the cap math values it at and the `sector` the sector rail groups it
    under. The enrichment lives here rather than on `OrderRequest` because price and sector are the
    rail layer's concern, not the broker's.
    What it assumes: `price` is the same reference price the resulting lot will be marked at, so the
    pre-trade check and the post-trade book agree.
    What it never does: carry an override, a "skip rails" flag or a symbol — there is no field that
    could bypass a check (acceptance criterion 3).
    """

    request: OrderRequest
    price: Decimal
    sector: str

    def __post_init__(self) -> None:
        require_decimal("price", self.price)
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")
        if not self.sector.strip():
            raise ValueError("an order must name the sector it concentrates into")

    @property
    def isin(self) -> str:
        """The instrument, from the wrapped request. ISIN only (invariant #2)."""
        return self.request.isin

    @property
    def side(self) -> Side:
        """BUY or SELL."""
        return self.request.side

    @property
    def quantity(self) -> int:
        """Whole shares, as the request validated."""
        return self.request.quantity

    @property
    def value(self) -> Decimal:
        """Rupee value at the reference price — what the per-order sanity caps bound."""
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class HouseholdExposure:
    """One instrument's total value across every case, and the household's total value.

    The input the cross-case concentration rail needs (§8.1): two cases each holding 12% of
    themselves in one stock is a 24% household exposure that neither case's own rails can see. A
    value-based view rather than `analyst.cases.CrossCaseExposure`'s quantity-based one, because a
    concentration rail is a percentage and a percentage needs prices — the daily loop assembles
    this from the case service's exposure plus current prices.
    """

    isin: str
    household_value_in_isin: Decimal
    household_total_value: Decimal

    def __post_init__(self) -> None:
        require_decimal("household_value_in_isin", self.household_value_in_isin)
        require_decimal("household_total_value", self.household_total_value)
        if self.household_value_in_isin < _ZERO:
            raise ValueError("household exposure in an instrument cannot be negative")
        if self.household_total_value < _ZERO:
            raise ValueError("household total value cannot be negative")


@dataclass(frozen=True, slots=True)
class BookRails:
    """The caps one M17 paper book trades under (pre-registration §4 step 5).

    What it does: carry, exactly, the per-book caps of the book's mandate (position %, sector %,
    number of names) and the rails every M17 book shares (participation, minimum hold, the
    cash-equity series a buy may be in). The numbers are *not* defined here: the fund-manager
    package builds this from its roster (`analyst.fundmanager.books.book_rails`), the one place
    they are written down, so A8 cannot disagree with the pre-registration.
    What it never does: carry a switch that turns shorting, F&O or margin on. Those three are
    prohibitions with no parameter, checked by `check_book_order` on every order.
    """

    max_position_pct: Decimal
    max_sector_pct: Decimal
    max_positions: int
    participation_max_pct: Decimal
    participation_lookback_sessions: int
    min_hold_sessions: int
    equity_series: frozenset[str]

    def __post_init__(self) -> None:
        for name in ("max_position_pct", "max_sector_pct", "participation_max_pct"):
            value = require_decimal(name, getattr(self, name))
            if not _ZERO < value <= _HUNDRED:
                raise ValueError(f"{name} must be in (0, 100] percentage points, got {value}")
        for name in ("max_positions", "participation_lookback_sessions", "min_hold_sessions"):
            count = getattr(self, name)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(f"{name} must be a whole count of at least 1, got {count!r}")
        if not self.equity_series or any(not s.strip() for s in self.equity_series):
            raise ValueError("a book must name the cash-equity series it may buy")


@dataclass(frozen=True, slots=True)
class BookOrderFacts:
    """What the M17 rails know about one order beyond the book, all as of its decision session.

    What it does: carry the instrument's series on the decision session (``None`` when it is not
    a listed cash-equity line that day), the median traded value over the participation lookback
    *ending at* the decision session and how many sessions it spans (``None`` when not enough
    history is knowable), the sessions elapsed from the name's last buy fill to the decision
    session (``None`` when the book has no buy fill of it), and the cash the book can still spend
    this session after the buys already cleared.
    What it assumes: the caller computed every figure from data knowable at the decision session
    (invariant #7) — the fund-manager book does, and refuses a market answer dated after it.
    What it never does: carry an override. Every field is a fact a rail compares; none is a vote.
    """

    decision_session: date
    series: str | None
    median_traded_value: Decimal | None
    median_sessions: int
    sessions_since_buy_fill: int | None
    spendable_cash: Decimal

    def __post_init__(self) -> None:
        if self.median_traded_value is not None:
            require_decimal("median_traded_value", self.median_traded_value)
        require_decimal("spendable_cash", self.spendable_cash)
        if self.median_sessions < 0:
            raise ValueError(f"median_sessions cannot be negative, got {self.median_sessions}")
        if self.sessions_since_buy_fill is not None and self.sessions_since_buy_fill < 0:
            raise ValueError("sessions_since_buy_fill cannot be negative")


@dataclass(frozen=True, slots=True)
class RailBreach:
    """A single rail refusing an order, with the number that broke it.

    What it does: name the rail, the limit it enforces and the value that exceeded it, so a
    `RAIL_BLOCK` line says not just *that* an order was refused but by how much — a breach of 15.1%
    against a 15% cap reads differently from one of 40%, and the evidence pack shows both.
    """

    rail: RailId
    limit: Decimal
    observed: Decimal
    detail: str

    def message(self) -> str:
        """One line naming the rail and the two numbers, for a rationale or a log."""
        return f"{self.rail.value}: {self.detail} (observed {self.observed}, limit {self.limit})"


@dataclass(frozen=True, slots=True)
class RailAssessment:
    """The verdict on one order: the breaches it caused, or none.

    What it does: carry the assessed order and every rail it breached. An order is `allowed` only
    when there are no breaches; a blocked order names *all* the rails it broke, not just the first,
    because a proposal that fixes one and re-submits should not discover the next one order later.
    """

    isin: str
    side: Side
    breaches: tuple[RailBreach, ...]

    @property
    def allowed(self) -> bool:
        """True only when no rail was breached. This is the whole authority A8 grants."""
        return not self.breaches

    @property
    def breached_rails(self) -> tuple[RailId, ...]:
        """The rails that refused the order, in the order they were checked."""
        return tuple(breach.rail for breach in self.breaches)

    def rationale(self) -> str:
        """The human-readable reason, naming every breached rail — a `RAIL_BLOCK` rationale."""
        if self.allowed:
            return "no rail breached"
        return "; ".join(breach.message() for breach in self.breaches)


@dataclass(frozen=True, slots=True)
class DrawdownStatus:
    """The daily drawdown monitor's verdict (§5.2 policy 4, §6).

    What it does: carry the peak, the trough that followed it, the peak-to-trough fall as a
    magnitude in percent, and the ratified limit — so `review_forced` is a comparison anyone can
    re-check, and the payload of the forced-review journal line is exactly these numbers.
    What it never does: express the fall as a negative number. §5.2's "-25% peak-to-trough" is a
    magnitude of 25 here, the direction carried by the name, like `RiskRails.drawdown_review_pct`.
    """

    peak: Decimal
    trough: Decimal
    drawdown_pct: Decimal
    limit_pct: Decimal

    @property
    def review_forced(self) -> bool:
        """Whether the fall reached the ratified limit — the trigger for a forced review."""
        return self.drawdown_pct >= self.limit_pct


def drawdown_of(peak: Decimal, trough: Decimal) -> Decimal:
    """Peak-to-trough fall as a percent magnitude: `(peak - trough) / peak * 100`.

    Zero when the peak is not positive (a case with no value has no drawdown) or when the trough is
    at or above the peak (no fall). Never negative — a rise is a drawdown of zero, not a negative
    one, because the rail compares a magnitude to a magnitude.
    """
    peak = require_decimal("peak", peak)
    trough = require_decimal("trough", trough)
    if peak <= _ZERO or trough >= peak:
        return _ZERO
    return (peak - trough) / peak * _HUNDRED
