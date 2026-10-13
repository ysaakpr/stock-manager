"""D5: the wire models behind the console's Portfolio page (M18.7) — one M17 book at a time.

Every book the M17 desk records — a manager's 10 L primary and its 1 cr mirror, a control, a style
book, the bench — is a portfolio of its own and gets the same view: its holdings as of its latest
marked session, the order (if any) staged for each name on the next session, and its fund totals.

Conventions are `status.models`': frozen, closed to undeclared fields, and `None` means unknown or
absent, never zero. Every money and percentage field is a *strict* `Decimal`: a float handed to one
of these models is a `ValidationError`, not a silently-rounded number (CLAUDE.md, money is Decimal).
Values are carried exact; the page rounds to the paisa when it prints them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BookChoiceOut",
    "FundTotalsOut",
    "HoldingRowOut",
    "PortfolioOut",
    "PortfolioStream",
    "StagedAction",
]

#: Money and percentages on the Portfolio page: a `Decimal` and nothing else.
Money = Annotated[Decimal, Field(strict=True)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortfolioStream(StrEnum):
    """Which M17 journal stream the page is reading (`analyst.fundmanager.job`)."""

    LIVE = "m17"
    """The live stream: entries under each book's own id. Empty until S0."""
    DRY = "m17-dry"
    """The dry-run stream: entries under ``m17-dry:<book id>``. Nothing in it counts."""


class StagedAction(StrEnum):
    """What the next session will do with a name, as the book's staging lines say."""

    BUY = "BUY"
    SELL = "SELL"
    """A sell or a trim (a trim is a sell of part of the holding; its quantity says how much)."""


class BookChoiceOut(_Frozen):
    """One selectable book: its id, its kind and whom it belongs to (from the roster)."""

    book_id: str
    kind: str = Field(description="MANAGER, CONTROL, STYLE or BENCH")
    manager: str | None = Field(
        description="The manager (a manager or style book), or the book a control controls for"
    )
    opening_capital_inr: Money


class HoldingRowOut(_Frozen):
    """One instrument on the page: a holding, a name with an order staged, or both.

    Keyed by ``isin`` (invariant #2); ``symbol`` is display only. A name the book does not hold
    but has a BUY staged for carries no holding figures (``quantity`` and everything derived from
    cost are None). A suspended holding carries ``suspended_since`` and no CMP-derived figure;
    ``value`` is then the quantity at its last traded close — what the book is marked at.
    """

    isin: str
    symbol: str | None = Field(description="Display only — never a key")
    held: bool
    quantity: int | None
    cmp: Money | None = Field(description="The close the book was marked at on cmp_session")
    cmp_session: date | None
    previous_close: Money | None
    previous_session: date | None
    day_change: Money | None = Field(
        description="CMP minus the previous session's close, per share"
    )
    position_day_change: Money | None = Field(
        description="day_change x quantity held: the day's change on the whole position; None "
        "for a name not held (a staged BUY) and for a suspended holding"
    )
    day_change_pct: Money | None = Field(description="The same for a share and for the position")
    cost: Money | None = Field(description="Cost basis of the shares held, buy charges included")
    average_price: Money | None = Field(description="cost / quantity")
    value: Money | None = Field(description="quantity x CMP (a suspended name: x its last close)")
    profit: Money | None = Field(description="value - cost")
    profit_pct: Money | None = Field(description="profit over cost, in percent")
    suspended_since: date | None = Field(description="Held, not trading since this session")
    action: StagedAction | None = Field(description="The order staged for the next session")
    action_quantity: int | None
    note: str | None = Field(description="Why a figure is missing, when one is")


class FundTotalsOut(_Frozen):
    """The book's fund totals on its latest marked session.

    ``nav`` and ``cash`` are the book's own ``BOOK_MARK`` — the figures the digest and
    `/status/managers` report — so they are never recomputed here. ``invested`` and
    ``current_value`` are summed from the rows; ``unreconciled`` is ``current_value + cash - nav``
    when that is not zero (or cannot be computed), so a disagreement between the rows and the
    mark is shown, never hidden.
    """

    session: date
    opening_capital: Money
    invested: Money | None
    current_value: Money | None
    cash: Money
    nav: Money
    total_profit: Money = Field(description="nav - opening capital")
    total_profit_pct: Money
    previous_session: date | None
    previous_nav: Money | None
    daily_change: Money | None = Field(description="nav - the previous mark's nav")
    daily_change_pct: Money | None
    positions: int = Field(description="The mark's own position count")
    unreconciled: Money | None
    reconciled: bool


class PortfolioOut(_Frozen):
    """The Portfolio page for one book."""

    as_of: datetime
    stream: PortfolioStream
    desk_record: str | None = Field(description="The paper_session row the holdings came from")
    book: BookChoiceOut
    books: list[BookChoiceOut] = Field(description="Every selectable book, in roster order")
    session: date | None = Field(description="The book's latest marked session")
    next_session: date | None = Field(description="The session the staged orders fill on")
    rows: list[HoldingRowOut]
    totals: FundTotalsOut | None
    bench_note: str | None = Field(description="What the bench holds, for the bench book")
    missing: list[str] = Field(description="What is missing, said plainly")
    error: str | None = Field(description="A journal the page could not read, masked")
