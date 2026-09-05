"""M4.6 — the portfolio book and XIRR under generated histories.

`tests/unit/test_accounting.py` pins the arithmetic on hand-built cases. These properties hold for
any history and would catch the class of defect a hand case misses — a basis that leaks a paisa on
the hundredth partial sell, a corporate action that moves value, a rate that depends on how the
stream happened to be ordered:

* **The P&L identity.** ``realized + unrealized == NAV - net deposits`` after any sequence of
  deposits, buys and sells — a book that invents or loses money anywhere breaks it.
* **Cost basis is conserved by corporate actions.** A split or bonus changes the share count by
  exactly the stated multiple and leaves the total basis untouched; a merger moves the whole basis
  and a demerger splits it, so the summed basis across the affected ISINs is unchanged.
* **XIRR is a rate, not an artefact of the stream.** It is invariant to the order the cashflows are
  given in and to scaling every amount by the same positive factor; a single deposit that returns
  ``(1 + r)`` exactly one 365-day year later has XIRR ``r``.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from backtest.accounting import CorporateActionError, PortfolioBook
from backtest.xirr import Cashflow, xirr
from execution.broker import Exchange, Fill, Side
from execution.costs import CostModel, Trade, load_rate_card

_ZERO = Decimal("0")
_ONE = Decimal("1")
_ISINS = ("INE009A01021", "INE002A01018", "INE040A01034")
_START = date(2024, 1, 2)
_MODEL = CostModel(load_rate_card())


def _fill(isin: str, session: date, side: Side, quantity: int, price: Decimal) -> Fill:
    trade = Trade(isin=isin, trade_date=session, side=side, quantity=quantity, price=price)
    return Fill(
        isin=isin,
        session=session,
        side=side,
        quantity=quantity,
        exchange=Exchange.NSE,
        reference_price=price,
        slippage_bps=_ZERO,
        fill_price=price,
        cost=_MODEL.charge(trade),
    )


@st.composite
def _history(draw: st.DrawFn) -> list[tuple[str, ...]]:
    """A chronological list of events: ('deposit', amount) | ('buy'|'sell', isin, qty, price)."""
    events: list[tuple[str, ...]] = [("deposit", str(draw(st.integers(100_000, 50_000_000))))]
    n = draw(st.integers(min_value=0, max_value=25))
    for _ in range(n):
        kind = draw(st.sampled_from(("deposit", "buy", "sell", "sell")))
        if kind == "deposit":
            events.append(("deposit", str(draw(st.integers(1, 1_000_000)))))
        else:
            isin = draw(st.sampled_from(_ISINS))
            qty = draw(st.integers(1, 2000))
            price = Decimal(draw(st.integers(100, 1_000_000))) / 100
            events.append((kind, isin, str(qty), str(price)))
    return events


@given(history=_history())
@settings(max_examples=250, deadline=None)
def test_realized_plus_unrealized_is_nav_minus_deposits(history: list[tuple[str, ...]]) -> None:
    book = PortfolioBook()
    deposits = _ZERO
    last_price: dict[str, Decimal] = {}
    session = _START
    for event in history:
        session += timedelta(days=1)
        if event[0] == "deposit":
            amount = Decimal(event[1])
            book.deposit(session, amount)
            deposits += amount
            continue
        _, isin, qty_s, price_s = event
        qty, price = int(qty_s), Decimal(price_s)
        last_price[isin] = price
        if event[0] == "buy":
            fill = _fill(isin, session, Side.BUY, qty, price)
            if fill.cost.net_amount > book.cash:
                continue  # unaffordable — the broker would have rejected it
            book.record_fill(fill)
        else:
            held = book.position(isin)
            if held is None:
                continue
            fill = _fill(isin, session, Side.SELL, min(qty, held.quantity), price)
            book.record_fill(fill)
        # After every fill the identity must hold, marked at the last traded prices.
        marks = {p.isin: last_price[p.isin] for p in book.positions()}
        nav = book.net_asset_value(marks)
        lhs = book.realized_pnl + book.unrealized_pnl(marks)
        rhs = nav - deposits
        assert abs(lhs - rhs) <= Decimal("0.000001"), f"P&L identity broken: {lhs} vs {rhs}"
        assert book.cash >= _ZERO
        for position in book.positions():
            assert position.quantity > 0
            assert position.cost_basis >= _ZERO


@given(
    quantity=st.integers(min_value=1, max_value=100_000),
    price=st.integers(min_value=100, max_value=1_000_000).map(lambda p: Decimal(p) / 100),
    from_fv=st.sampled_from([Decimal(10), Decimal(5), Decimal(2), Decimal(1)]),
    to_fv=st.sampled_from([Decimal(10), Decimal(5), Decimal(2), Decimal(1)]),
    bonus=st.tuples(st.integers(1, 5), st.integers(1, 5)),
)
@settings(max_examples=200, deadline=None)
def test_split_and_bonus_scale_quantity_and_conserve_basis(
    quantity: int,
    price: Decimal,
    from_fv: Decimal,
    to_fv: Decimal,
    bonus: tuple[int, int],
) -> None:
    assume(from_fv >= to_fv)  # a split lowers the face value; a consolidation is the reverse case
    isin = _ISINS[0]
    book = PortfolioBook()
    book.deposit(_START, Decimal("100000000000"))
    book.record_fill(_fill(isin, _START, Side.BUY, quantity, price))
    before = book.position(isin)
    assert before is not None
    basis = before.cost_basis

    split_quantity = Decimal(quantity) * from_fv / to_fv  # multiply first: exact when whole
    if split_quantity % 1 != 0:
        with pytest.raises(CorporateActionError):
            book.apply_split(isin, from_face_value=from_fv, to_face_value=to_fv)
        return
    book.apply_split(isin, from_face_value=from_fv, to_face_value=to_fv)
    after_split = book.position(isin)
    assert after_split is not None
    assert Decimal(after_split.quantity) == split_quantity
    assert after_split.cost_basis == basis

    new, held = (Decimal(bonus[0]), Decimal(bonus[1]))
    bonus_quantity = Decimal(after_split.quantity) * (new + held) / held
    if bonus_quantity % 1 != 0:
        with pytest.raises(CorporateActionError):
            book.apply_bonus(isin, new_shares=new, held_shares=held)
        return
    # A 1:3 bonus on a multiple of three shares is the case the book used to refuse (it divided
    # before multiplying and saw 3.999…); the whole point of this branch is that it now applies.
    book.apply_bonus(isin, new_shares=new, held_shares=held)
    after_bonus = book.position(isin)
    assert after_bonus is not None
    assert Decimal(after_bonus.quantity) == bonus_quantity
    assert after_bonus.cost_basis == basis


@given(
    quantity=st.integers(min_value=1, max_value=50_000),
    price=st.integers(min_value=100, max_value=500_000).map(lambda p: Decimal(p) / 100),
    fraction=st.integers(min_value=0, max_value=1000).map(lambda f: Decimal(f) / 1000),
    ratio=st.tuples(st.integers(1, 10), st.integers(1, 10)),
)
@settings(max_examples=200, deadline=None)
def test_demerger_and_merger_conserve_total_basis(
    quantity: int, price: Decimal, fraction: Decimal, ratio: tuple[int, int]
) -> None:
    parent, child, survivor = _ISINS
    received, held = Decimal(ratio[0]), Decimal(ratio[1])
    assume((Decimal(quantity) * received / held) % 1 == 0)
    book = PortfolioBook()
    book.deposit(_START, Decimal("100000000000"))
    book.record_fill(_fill(parent, _START, Side.BUY, quantity, price))
    original = book.position(parent)
    assert original is not None
    basis = original.cost_basis

    book.apply_demerger(
        parent,
        resulting_isin=child,
        shares_received=received,
        shares_held=held,
        cost_fraction_to_resulting=fraction,
    )
    p, c = book.position(parent), book.position(child)
    assert p is not None
    if c is not None:
        assert p.cost_basis + c.cost_basis == basis
    else:
        assert (
            p.cost_basis == basis
        )  # zero shares received (ratio too small) leaves nothing to hold

    # Merge the parent into a survivor: the whole parent basis moves.
    book.apply_merger(parent, surviving_isin=survivor, shares_received=received, shares_held=held)
    assert book.position(parent) is None
    s = book.position(survivor)
    if s is not None:
        assert s.cost_basis == p.cost_basis
        assert Decimal(s.quantity) == Decimal(p.quantity) * received / held


@given(
    rate_bps=st.integers(min_value=-9000, max_value=30000),
    principal=st.integers(min_value=1000, max_value=10_000_000),
)
@settings(max_examples=200, deadline=None)
def test_one_year_round_trip_recovers_the_rate(rate_bps: int, principal: int) -> None:
    rate = Decimal(rate_bps) / 10_000
    start = date(2021, 1, 1)  # 2021 is not a leap year: 365 days is exactly one ACT/365F year
    terminal = Decimal(principal) * (_ONE + rate)
    stream = [Cashflow(start, -Decimal(principal)), Cashflow(start + timedelta(days=365), terminal)]
    assert abs(xirr(stream) - rate) <= Decimal("0.0000002")


@st.composite
def _cashflows(draw: st.DrawFn) -> list[Cashflow]:
    n = draw(st.integers(min_value=2, max_value=12))
    flows: list[Cashflow] = []
    day = date(2020, 1, 1)
    for i in range(n):
        if i < n - 1:
            day += timedelta(days=draw(st.integers(1, 120)))
            flows.append(Cashflow(day, -Decimal(draw(st.integers(1000, 100_000)))))
        else:
            # The terminal mark lands at least two months after the last instalment and returns
            # 90%..250% of the money paid in. A 40% loss one day after the last pay-in annualises
            # to -99.99999…%, below the solver's -100% bracket; that stream is refused loudly by
            # design (`XIRRError`) and is not what this property is about.
            day += timedelta(days=draw(st.integers(60, 400)))
            paid_in = -sum((f.amount for f in flows), _ZERO)
            pct = Decimal(draw(st.integers(90, 250))) / 100
            flows.append(Cashflow(day, (paid_in * pct).quantize(Decimal("0.01"))))
    return flows


@given(flows=_cashflows(), scale=st.integers(min_value=1, max_value=1000), seed=st.randoms())
@settings(max_examples=200, deadline=None)
def test_xirr_is_invariant_to_order_and_scale(
    flows: list[Cashflow], scale: int, seed: random.Random
) -> None:
    rate = xirr(flows)
    shuffled = list(flows)
    seed.shuffle(shuffled)
    assert xirr(shuffled) == rate
    scaled = [Cashflow(f.when, f.amount * scale) for f in flows]
    assert abs(xirr(scaled) - rate) <= Decimal("0.0000002")
