"""A8 exit slicing: no generated order stream yields a child above a cap or a buy above the cap.

Slicing lets an over-cap exit through as children; the claim that keeps it a rail rather than a
hole is universal, so it is generated rather than hand-picked. A random stream of buy and sell
intents is cleared through ``RailEngine.guard_exit`` against a book that moves with every allowed
order (prices fixed, so case value is invariant under trades, as in ``test_rails_property``), and
after every step:

* every order A8 allowed — a whole buy, a whole sell, or one child of a sliced exit — is within
  ``max_order_value_inr`` and ``max_order_pct_of_case``;
* every allowed buy is whole (never sliced) and within the rupee cap;
* an allowed exit's children are all sells of the parent's instrument and sum to the parent's
  quantity, which never exceeds what was held;
* an exit is cleared all or nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from analyst.cases import RiskRails
from analyst.journal.models import JournalEntry, RecordedEntry
from analyst.rails import Lot, Portfolio, ProposedOrder, RailEngine, apply_order
from dataplatform.clock import FrozenClock
from execution.broker import Exchange, OrderRequest, Side

_UNIVERSE: tuple[tuple[str, str, Decimal], ...] = (
    ("INE001A01001", "IT", Decimal("100")),
    ("INE002A01009", "IT", Decimal("2500")),
    ("INE003A01007", "PHARMA", Decimal("37")),
    ("INE004A01005", "PHARMA", Decimal("812.35")),
    ("INE005A01003", "ENERGY", Decimal("55000")),
    ("INE006A01001", "ENERGY", Decimal("1.05")),
)
_DAY = date(2024, 1, 2)


@dataclass(frozen=True, slots=True)
class _Intent:
    index: int
    buy: bool
    quantity: int


_intents = st.lists(
    st.builds(
        _Intent,
        index=st.integers(min_value=0, max_value=len(_UNIVERSE) - 1),
        buy=st.booleans(),
        quantity=st.integers(min_value=1, max_value=50_000),
    ),
    max_size=50,
)

# Position cap x min holdings must reach 100% (RiskRails refuses a contradictory pair).
_POSITION_AND_FLOOR = (
    (Decimal("15"), 8),
    (Decimal("30"), 4),
    (Decimal("50"), 2),
    (Decimal("100"), 1),
)


@st.composite
def _rails(draw: st.DrawFn) -> RiskRails:
    position, floor = draw(st.sampled_from(_POSITION_AND_FLOOR))
    return RiskRails(
        max_position_pct=position,
        max_sector_pct=draw(
            st.sampled_from([Decimal("35"), Decimal("60"), Decimal("100")]).filter(
                lambda sector: sector >= position
            )
        ),
        min_holdings=floor,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=draw(
            st.sampled_from(
                [Decimal("50000"), Decimal("120000"), Decimal("120000.50"), Decimal("1000000")]
            )
        ),
        max_order_pct_of_case=draw(st.sampled_from([Decimal("5"), Decimal("15"), Decimal("100")])),
    )


class _ListJournal:
    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []

    def append(self, entry: JournalEntry) -> RecordedEntry:
        self.entries.append(entry)
        return RecordedEntry(**entry.model_dump(), id=len(self.entries), recorded_at=entry.ts)


def _within_caps(order: ProposedOrder, book: Portfolio, rails: RiskRails) -> bool:
    return (
        order.value <= rails.max_order_value_inr
        and order.value * 100 <= rails.max_order_pct_of_case * book.total_value
    )


@given(
    rails=_rails(),
    intents=_intents,
    cash=st.sampled_from([Decimal("500000"), Decimal("3000000"), Decimal("20000000")]),
)
@settings(max_examples=300, deadline=None)
def test_no_order_stream_yields_a_child_or_a_buy_above_the_cap(
    rails: RiskRails, intents: list[_Intent], cash: Decimal
) -> None:
    engine = RailEngine(_ListJournal(), clock=FrozenClock(_DAY))
    book = Portfolio(case_id="CASE-P", lots=(), cash=cash)
    for intent in intents:
        isin, sector, price = _UNIVERSE[intent.index]
        side = Side.BUY if intent.buy else Side.SELL
        held_lot = book.lot(isin)
        held = 0 if held_lot is None else held_lot.quantity
        if side is Side.BUY:
            quantity = min(intent.quantity, int(book.cash // price))
        else:
            quantity = min(intent.quantity, held)
        if quantity <= 0:
            continue
        parent = ProposedOrder(
            request=OrderRequest(isin=isin, side=side, quantity=quantity, exchange=Exchange.NSE),
            price=price,
            sector=sector,
        )
        clearance = engine.guard_exit(parent, book, rails, trading_date=_DAY)
        allowed = clearance.allowed
        if not allowed:
            continue
        assert len(allowed) == len(clearance.children)  # all or nothing
        if side is Side.BUY:
            assert allowed == (parent,)
            assert parent.value <= rails.max_order_value_inr
        else:
            assert sum(child.quantity for child in allowed) == parent.quantity <= held
            assert all(child.isin == isin and child.side is Side.SELL for child in allowed)
        for child in allowed:
            assert _within_caps(child, book, rails), (child, rails)
            book = apply_order(book, child)


@given(
    rails=_rails(), quantity=st.integers(min_value=1, max_value=200_000), index=st.integers(0, 5)
)
@settings(max_examples=300, deadline=None)
def test_a_full_exit_of_a_held_long_always_clears_when_only_the_order_caps_stand_in_its_way(
    rails: RiskRails, quantity: int, index: int
) -> None:
    """The defect itself: with no other rail in play, any exit a share's price allows completes."""
    isin, sector, price = _UNIVERSE[index]
    # No floor in play: the book keeps a second name, and min_holdings is 1.
    open_rails = rails.model_copy(update={"max_position_pct": Decimal("100"), "min_holdings": 1})
    filler, filler_sector, filler_price = _UNIVERSE[(index + 1) % len(_UNIVERSE)]
    book = Portfolio(
        case_id="CASE-P",
        lots=(
            Lot(isin=filler, sector=filler_sector, quantity=1, price=filler_price),
            Lot(isin=isin, sector=sector, quantity=quantity, price=price),
        ),
        cash=Decimal("1000000"),
    )
    exit_ = ProposedOrder(
        request=OrderRequest(isin=isin, side=Side.SELL, quantity=quantity),
        price=price,
        sector=sector,
    )
    clearance = RailEngine(_ListJournal(), clock=FrozenClock(_DAY)).guard_exit(
        exit_, book, open_rails, trading_date=_DAY
    )
    one_share_fits = _within_caps(
        ProposedOrder(
            request=OrderRequest(isin=isin, side=Side.SELL, quantity=1), price=price, sector=sector
        ),
        book,
        open_rails,
    )
    assert bool(clearance.allowed) == one_share_fits
    if one_share_fits:
        assert sum(child.quantity for child in clearance.allowed) == quantity
