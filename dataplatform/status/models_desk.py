"""D5: the wire models behind the console's Desk page (M18.3) that `/status/*` does not carry.

The paper books and the M17 desk are `PaperOut` and `ManagersOut` (`status.models`), served as
they are; this file holds only what the Desk shows beyond them — the paper books' positions, the
broker-session interlock's journaled state, the latest orders, the M17 mandates and the model
spend from `token_usage`.

Conventions are `status.models`': frozen, closed to undeclared fields, and `None` means unknown or
absent, never zero. Every money field is a *strict* `Decimal`: a float handed to one of these
models is a `ValidationError`, not a silently-rounded number (CLAUDE.md, money is Decimal).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BrokerSessionOut",
    "BrokerSessionState",
    "MandateOut",
    "ModelCostLineOut",
    "ModelCostOut",
    "OrderLineOut",
    "OrdersOut",
    "PaperPositionOut",
    "PaperPositionsOut",
]

#: Money on the Desk: a `Decimal` and nothing else — a float is refused at the model, not rounded.
Money = Annotated[Decimal, Field(strict=True)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── paper positions ─────────────────────────────────────────────────────────────────────────


class PaperPositionOut(_Frozen):
    """One holding in a paper book's own accounting book (`paper_session.expected_book`)."""

    isin: str
    quantity: int
    cost_basis: Money | None


class PaperPositionsOut(_Frozen):
    """A paper book's holdings after its newest session that recorded an accounting book."""

    book_id: str
    trading_date: date = Field(description="The session whose expected_book this is")
    cash: Money | None
    positions: list[PaperPositionOut]


# ── the broker-session interlock ────────────────────────────────────────────────────────────


class BrokerSessionState(StrEnum):
    """What the journal says about the broker session (M5.15's auth interlock)."""

    AUTH_REQUIRED = "AUTH_REQUIRED"
    """The newest journaled session carries an AUTH_REQUIRED entry: no order may be placed."""
    CLEAR = "CLEAR"
    """An AUTH_REQUIRED was journaled once, but not on the newest journaled session."""
    NO_LAPSE_RECORDED = "NO_LAPSE_RECORDED"
    """The journal holds no AUTH_REQUIRED entry at all."""


class BrokerSessionOut(_Frozen):
    """The broker-session interlock as the decision journal records it — display only.

    The interlock journals an `AUTH_REQUIRED` entry for every trading day the broker session is
    not authenticated and nothing on a clear day, so the journal is the one persisted record of
    the session; a live check would need the broker itself, which System 1 never imports.
    """

    state: BrokerSessionState
    newest_journaled_date: date | None = Field(description="The newest trading date journaled")
    last_auth_required_date: date | None
    last_auth_required_at: datetime | None
    reason: str | None = Field(description="The gate's reason on the last AUTH_REQUIRED, masked")
    reauth: str | None = Field(description="The re-authenticate instruction it journaled")
    streak_started: date | None
    deferred_count: int | None


# ── orders ──────────────────────────────────────────────────────────────────────────────────


class OrderLineOut(_Frozen):
    """One order, from the `order_` ledger or from a paper session's placed orders."""

    source: str = Field(description="order_ (the X1 ledger) or paper_session (a paper book)")
    book: str = Field(description="The order's case (order_) or paper book (paper_session)")
    reference: str | None = Field(description="order_uid, when the source has one")
    trading_date: date = Field(description="staged_for_date (order_) or the session (paper)")
    staged_at: datetime | None
    isin: str
    side: str
    quantity: int
    order_type: str
    limit_price: Money | None
    state: str = Field(description="The order_ lifecycle state, or PLACED for a paper order")
    tags: list[str] = Field(description="Broker, sleeve and the caller's own tag, as recorded")
    filled_quantity: int | None
    avg_fill_price_inr: Money | None
    costs_inr: Money | None
    executed_at: datetime | None
    rejection_reason: str | None


class OrdersOut(_Frozen):
    """The latest orders: the newest `order_` rows and each paper book's newest placed orders."""

    as_of: datetime
    limit: int = Field(description="At most this many order_ rows, newest first")
    ledger: list[OrderLineOut]
    paper: list[OrderLineOut]


# ── the M17 mandates ────────────────────────────────────────────────────────────────────────


class MandateOut(_Frozen):
    """One M17 roster book's mandate, summarised: what it may hold, never how it is prompted."""

    book_id: str
    kind: str = Field(description="MANAGER, CONTROL, STYLE or BENCH")
    manager: str | None = Field(description="The manager a book belongs to or controls for")
    role: str | None
    style: str | None
    opening_capital_inr: Money
    max_positions: int | None
    max_position_pct: Money | None
    max_sector_pct: Money | None
    decision_model: str | None


# ── model cost ──────────────────────────────────────────────────────────────────────────────


class ModelCostLineOut(_Frozen):
    """One (manager, provider, model, IST day)'s model spend, summed from `token_usage`."""

    case_id: str | None = Field(description="The manager book or case the calls were for")
    provider: str
    model: str
    day: date = Field(description="The calls' day in Asia/Kolkata")
    calls: int
    tokens_in: int
    tokens_out: int
    cost_inr: Money
    cost_usd: Money | None = Field(description="None when no call that day recorded USD")
    usd_calls: int = Field(description="How many of the calls recorded a USD figure")


class ModelCostOut(_Frozen):
    """`token_usage` over the last `days` IST days, per manager, model and day, with totals."""

    as_of: datetime
    days: int
    since: datetime = Field(description="The window's first instant (IST midnight)")
    lines: list[ModelCostLineOut]
    total_inr: Money
    total_usd: Money | None = Field(description="None when no call in the window recorded USD")
