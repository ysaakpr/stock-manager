"""X1: the `Broker` interface — the one seam between the decision code and where orders go.

Invariant #5 (AGENTIC_CONTEXT §6, EXECUTION_PLAN §6): paper and real money run the *same*
decision code path; they differ only by which `Broker` implementation is injected. So `analyst/`
imports this protocol and never a concrete broker — no `if paper:` branch, no direct
`SimBroker`/`KiteBroker` reference anywhere upstream. `tests/unit/test_sim_broker.py` greps
`analyst/` to keep it that way.

What lives here: the `Broker` protocol (`place / modify / cancel / positions / holdings / ledger /
margins`, §6) and the typed value objects those methods speak in — orders and their lifecycle,
fills, positions, holdings, ledger lines and margins. What does *not* live here is any fill logic
or market data: `SimBroker` simulates fills (`sim_broker.py`) and `KiteBroker` reads them from the
exchange (M8). Both satisfy this identical surface.

Everything is `Decimal` for money and `int` for share counts — a fractional share is not a thing
the interface can express (invariant enforced in `OrderRequest`). Identity is the ISIN and only the
ISIN (invariant #2); an order keyed on a symbol would be a bug. These are frozen dataclasses because
they cross the module boundary between the decision layer and the broker (CLAUDE.md: no bare dicts
as an interface), and frozen because an order or fill, once made, is a record — not a mutable slot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from execution.costs import CostBreakdown, Exchange, Side

#: ISIN as the identity master (D2) issues it — the only join key an order may carry (invariant #2).
_ISIN_PATTERN = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"

_ZERO = Decimal("0")

__all__ = [
    "Broker",
    "BrokerError",
    "Exchange",
    "Fill",
    "FractionalQuantityError",
    "Holding",
    "LedgerEntry",
    "Margins",
    "Order",
    "OrderNotModifiableError",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "SessionExpired",
    "Side",
    "UnknownOrderError",
]


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class BrokerError(Exception):
    """Base for every broker refusal. Brokers fail loud; a bad order never passes silently."""


class FractionalQuantityError(BrokerError, ValueError):
    """An order quantity that is not a whole positive number of shares.

    Delivery equity trades in whole shares; a fractional or non-integral quantity is not something
    the market can fill, so it is rejected at construction rather than rounded into a different
    order than the caller asked for.
    """


class UnknownOrderError(BrokerError, KeyError):
    """`modify`/`cancel` named an order id this broker never issued."""


class OrderNotModifiableError(BrokerError):
    """`modify`/`cancel` reached an order that is no longer staged (filled, rejected, cancelled)."""


class SessionExpired(BrokerError):  # noqa: N818 - spec-named (M5.15); the class *is* the event
    """The broker's API session is no longer authenticated — the day's OAuth+2FA login has lapsed.

    Indian brokers force a daily API logout that only an interactive OAuth + 2FA login re-opens
    (NSE consolidated NNF circular INVG/73992 §8.3.2.1.8), so a session that was valid yesterday
    is dead at the next market open until a human re-authenticates. A concrete broker raises this
    from `session_valid()` (or from any order-path method) when it can positively determine the
    session is gone, rather than letting a request fail obscurely deep in the transport. The daily
    loop's auth interlock treats it exactly like a `False` from `session_valid()`: journal
    `AUTH_REQUIRED`, place no orders, defer the day's decisions. `SimBroker` never raises it — a
    paper session cannot expire (invariant #5: paper and real share the seam, not the failure).
    """


# ── vocabulary ───────────────────────────────────────────────────────────────────────────────


class OrderType(StrEnum):
    """How the order is priced.

    EOD rebalancing stages `MARKET` orders that fill at the next session's reference price
    (`sim_broker.py`); `LIMIT` is part of the interface because the real broker offers it, and a
    concrete broker that cannot honour a type raises rather than silently downgrading it.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    """Where an order is in its life. A staged order fills, is cancelled, or is rejected."""

    STAGED = "STAGED"
    """Placed EOD, awaiting the next session's fill (EXECUTION_PLAN §6 order staging)."""
    COMPLETE = "COMPLETE"
    """Filled. `Order.fill` carries the price, slippage and costs."""
    REJECTED = "REJECTED"
    """The broker declined to fill it (no liquidity, insufficient funds, nothing to sell).
    `Order.reason` says why."""
    CANCELLED = "CANCELLED"
    """Cancelled by the caller before it filled."""


def _require_whole_shares(value: object) -> int:
    """Return `value` as a positive whole share count, or raise `FractionalQuantityError`.

    `True`/`False` are `int` subclasses in Python and are rejected explicitly: a boolean is not a
    quantity. A `Decimal("1")` is rejected too — the interface takes `int`, so anything else is a
    caller passing money-shaped data where a share count belongs.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise FractionalQuantityError(
            f"quantity must be a whole number of shares (int), got {value!r}"
        )
    if value <= 0:
        raise FractionalQuantityError(f"quantity must be positive, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """What the decision layer asks the broker to do — one order, before it has an id or a fill.

    What it does: name the security (by ISIN), the side, a whole-share quantity, the exchange and
    the order type. `limit_price` is required for a `LIMIT` order and forbidden otherwise. `tag`
    lets the caller thread its own reference (a case id, a rebalance id) through to the fill.
    What it never does: express a fractional share, or key on a symbol.
    """

    isin: str
    side: Side
    quantity: int
    exchange: Exchange = Exchange.NSE
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    tag: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(_ISIN_PATTERN, self.isin):
            raise ValueError(f"not an ISIN: {self.isin!r}")
        _require_whole_shares(self.quantity)
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("a LIMIT order needs a limit_price")
            if not isinstance(self.limit_price, Decimal):
                raise TypeError("limit_price must be a Decimal — money is never float (CLAUDE.md)")
            if self.limit_price <= _ZERO:
                raise ValueError(f"limit_price must be positive, got {self.limit_price}")
        elif self.limit_price is not None:
            raise ValueError(f"a {self.order_type} order must not carry a limit_price")


@dataclass(frozen=True, slots=True)
class Fill:
    """The realized execution of an order — the price it got, why, and what it cost.

    `reference_price` is the next session's configured reference (open or VWAP band) before impact;
    `fill_price` is that price after `slippage_bps` of adverse slippage. `cost` is the full Indian
    charge breakdown from the one shared cost model (invariant #4). Keeping all three means a fill
    is auditable: the reference is a market fact, the slippage is the model's assumption, and the
    two together explain the fill price exactly.
    """

    isin: str
    session: date
    side: Side
    quantity: int
    exchange: Exchange
    reference_price: Decimal
    slippage_bps: Decimal
    fill_price: Decimal
    cost: CostBreakdown

    @property
    def gross(self) -> Decimal:
        """Turnover at the fill price, before costs."""
        return self.fill_price * self.quantity

    @property
    def net_cash(self) -> Decimal:
        """Signed cash effect: negative on a buy (cash out), positive on a sell (cash in).

        Costs always work against the account, so a buy pays turnover *plus* costs and a sell
        receives turnover *minus* costs — exactly `CostBreakdown.net_amount`, signed.
        """
        if self.side is Side.BUY:
            return -self.cost.net_amount
        return self.cost.net_amount


@dataclass(frozen=True, slots=True)
class Order:
    """A placed order and where it is in its life. Immutable — each transition is a new `Order`.

    `target_session` is the session the order is due to fill in (next session after the EOD
    decision, for `SimBroker`). `fill` is set only once `status` is `COMPLETE`; `reason` is set only
    on `REJECTED`/`CANCELLED`.
    """

    order_id: str
    request: OrderRequest
    status: OrderStatus
    decision_date: date
    target_session: date
    fill: Fill | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """An open, not-yet-settled position — a same-session buy before it becomes a holding.

    Indian delivery settles T+1: a buy filled today is a *position* today and a *holding* the next
    session. Keyed by ISIN (invariant #2). `average_price` excludes costs — it is the traded price,
    the way a broker's position book shows it.
    """

    isin: str
    exchange: Exchange
    quantity: int
    average_price: Decimal
    session: date


@dataclass(frozen=True, slots=True)
class Holding:
    """A settled delivery holding. Keyed by ISIN. `average_price` is the cost basis, ex-charges."""

    isin: str
    exchange: Exchange
    quantity: int
    average_price: Decimal


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One append-only line in the cash ledger — a debit or a credit and the balance after it.

    A fill posts one entry: a buy debits turnover plus costs, a sell credits turnover less costs.
    The ledger is the cash side of what reconciliation (§6) compares to the broker's statement,
    so it is append-only and never rewritten in place (invariant #12).
    """

    seq: int
    session: date
    isin: str
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class Margins:
    """Account funds: cash free to deploy, cash tied up in holdings, and their sum.

    Delivery equity is fully paid, so `utilised` is the cost basis of open positions and holdings,
    not a leveraged margin. Real brokers report more; this is the subset the decision layer needs.
    """

    available: Decimal
    utilised: Decimal
    total: Decimal = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", self.available + self.utilised)


# ── the interface ────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class Broker(Protocol):
    """The one seam between the decision code and execution (invariant #5).

    Every implementation — `SimBroker` for paper/backtest, `KiteBroker` for real money — offers
    exactly this surface, so switching between them is switching one injected object and nothing
    else. The decision layer (`analyst/`) depends on this protocol and never on a concrete broker;
    that is what makes "one decision code path" a structural fact rather than a discipline.

    What it never does: decide *whether* to trade. Rails (A8) and the policy layer decide that
    upstream; the broker only carries out — or refuses — the order it is handed.
    """

    def session_valid(self) -> bool:
        """Whether the broker API session is authenticated and usable for this trading day.

        The auth precondition every order path depends on — the counterpart, for dead auth, of the
        data-red interlock's `is_green` for bad data (EXECUTION_PLAN §4.4). The daily loop calls it
        before staging: a `False` means the day's OAuth+2FA login has lapsed and no order may be
        placed. An implementation that can positively detect an expired session may instead raise
        `SessionExpired`, which the interlock treats identically; returning `True` asserts the
        session is live. `SimBroker` always returns `True` (a paper session cannot expire), so
        paper mode is never blocked; `KiteBroker` (M8) checks the real token.
        """

    def place(self, request: OrderRequest) -> Order:
        """Place an order. Returns the resulting `Order` (staged for its target session)."""

    def modify(self, order_id: str, *, quantity: int) -> Order:
        """Change a still-staged order's quantity. Raises if the order is unknown or not staged."""

    def cancel(self, order_id: str) -> Order:
        """Cancel a still-staged order. Raises if the order is unknown or not staged."""

    def positions(self) -> tuple[Position, ...]:
        """Open, not-yet-settled positions (this session's fills), one per ISIN."""

    def holdings(self) -> tuple[Holding, ...]:
        """Settled delivery holdings, one per ISIN."""

    def ledger(self) -> tuple[LedgerEntry, ...]:
        """The cash ledger, in posting order — append-only."""

    def margins(self) -> Margins:
        """Available cash, cash tied up in positions/holdings, and their total."""
