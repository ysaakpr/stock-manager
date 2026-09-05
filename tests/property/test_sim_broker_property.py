"""M4.5 / invariant #5 — the paper broker's book under generated order streams.

`tests/unit/test_sim_broker.py` pins single fills. A broker's claims are about the *sequence*: no
stream of orders, however adversarial, may leave the book in a state a real account could not be
in. This file generates the streams and, after every session, asserts:

* **Cash is exactly the opening cash plus the signed cash of every completed fill**, and
  ``margins().available`` is that cash. It can dip below zero only by the amount a sell's charges
  exceed its proceeds (a ₹0.99 sell still owes the ₹15.93 DP charge, as a real ledger would show)
  — never by a buy, which is refused when the cash is not there.
* **Every completed fill is adverse** to its side — a buy fills at or above the reference, a sell at
  or below it — and is priced by the shared cost model (``fill.cost.net_amount`` is the ledger
  line; invariant #4).
* **The ledger reconciles**: opening cash plus the signed cash of every completed fill equals the
  cash on hand, and the last ledger balance is the cash.
* **Holdings are the fills**: the settled quantity of every ISIN equals the net of its completed
  buys and sells (T+1: a buy is a position on its fill day and a holding the next), and is never
  negative — a sell that would overdraw is rejected, never filled.
* **Determinism**: the identical stream through a fresh broker yields the identical ledger and
  book (§8.3.3), which is what the replay engine stands on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from dataplatform.clock import FrozenClock
from execution.broker import Exchange, Order, OrderRequest, OrderStatus, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import (
    DuplicateStagedOrderError,
    FillPolicy,
    NoReferenceBarError,
    ReferenceBar,
    ReferencePrice,
    SimBroker,
)

_ZERO = Decimal("0")
_ISINS = ("INE009A01021", "INE002A01018", "INE040A01034", "INE467B01029", "INE001A01036")
_FIRST = date(2024, 1, 1)  # uniform stamp-duty era: no account state needed


class _Market:
    def __init__(self, sessions: list[date], bars: dict[tuple[str, date], ReferenceBar]) -> None:
        self._sessions = sessions
        self._bars = bars

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        try:
            return self._bars[(isin, session)]
        except KeyError:
            raise NoReferenceBarError(f"no bar for {isin} on {session}") from None


@dataclass(frozen=True, slots=True)
class _Intent:
    isin_index: int
    buy: bool
    quantity: int


@dataclass(frozen=True, slots=True)
class _Stream:
    sessions: list[date]
    bars: dict[tuple[str, date], ReferenceBar]
    intents: list[list[_Intent]]  # per decision day, the orders staged that evening
    opening_cash: Decimal
    policy: FillPolicy


@st.composite
def _stream(draw: st.DrawFn) -> _Stream:
    n_sessions = draw(st.integers(min_value=2, max_value=8))
    sessions = [_FIRST + timedelta(days=i + 1) for i in range(n_sessions)]
    bars: dict[tuple[str, date], ReferenceBar] = {}
    for session in sessions:
        for isin in _ISINS:
            if draw(st.booleans()) or session == sessions[0]:
                open_ = Decimal(draw(st.integers(min_value=100, max_value=500_000))) / 100
                vwap = open_ * (Decimal("1") + Decimal(draw(st.integers(-300, 300))) / 10_000)
                traded = Decimal(draw(st.integers(min_value=100_000, max_value=10**10)))
                bars[(isin, session)] = ReferenceBar(
                    isin=isin,
                    session=session,
                    exchange=Exchange.NSE,
                    open=open_,
                    vwap=vwap,
                    traded_value=traded,
                )
    intents = [
        draw(
            st.lists(
                st.builds(
                    _Intent,
                    isin_index=st.integers(min_value=0, max_value=len(_ISINS) - 1),
                    buy=st.booleans(),
                    quantity=st.integers(min_value=1, max_value=3000),
                ),
                max_size=6,
            )
        )
        for _ in sessions  # one decision evening before each fill session
    ]
    reference = draw(st.sampled_from(list(ReferencePrice)))
    return _Stream(
        sessions=sessions,
        bars=bars,
        intents=intents,
        opening_cash=Decimal(draw(st.integers(min_value=0, max_value=50_000_000))),
        policy=FillPolicy(reference=reference),
    )


def _drive(stream: _Stream) -> tuple[SimBroker, list[Order], list[date]]:
    """Run the stream: decide on session k (clock frozen there), fill on session k+1."""
    clock = FrozenClock(_FIRST)
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=_Market(stream.sessions, stream.bars),
        opening_cash=stream.opening_cash,
        policy=stream.policy,
    )
    filled: list[Order] = []
    decision_days = [_FIRST, *stream.sessions[:-1]]
    for day, session, intents in zip(decision_days, stream.sessions, stream.intents, strict=True):
        clock.freeze_at(day)
        staged: set[str] = set()
        for intent in intents:
            isin = _ISINS[intent.isin_index]
            request = OrderRequest(
                isin=isin,
                side=Side.BUY if intent.buy else Side.SELL,
                quantity=intent.quantity,
                exchange=Exchange.NSE,
            )
            if isin in staged:
                try:
                    broker.place(request)
                except DuplicateStagedOrderError:
                    continue
                raise AssertionError("a second staged order for one scrip and session was accepted")
            broker.place(request)
            staged.add(isin)
        filled.extend(broker.execute_session(session))
        _assert_book_invariants(broker, stream.opening_cash, filled)
    return broker, filled, stream.sessions


def _assert_book_invariants(broker: SimBroker, opening: Decimal, filled: list[Order]) -> None:
    completed = [o for o in filled if o.status is OrderStatus.COMPLETE and o.fill is not None]
    signed = sum((o.fill.net_cash for o in completed if o.fill is not None), _ZERO)
    # A sell whose charges exceed its proceeds (a flat DP charge on a tiny sale) is the only way
    # cash can go below zero: the broker debits the charge regardless, as a real ledger does.
    overdraft_allowance = sum(
        (
            max(_ZERO, o.fill.cost.total - o.fill.cost.turnover)
            for o in completed
            if o.fill is not None and o.fill.side is Side.SELL
        ),
        _ZERO,
    )
    assert broker.cash >= -overdraft_allowance
    assert broker.margins().available == broker.cash
    assert opening + signed == broker.cash
    ledger = broker.ledger()
    if ledger:
        assert ledger[-1].balance == broker.cash
        assert len(ledger) == len(completed)
    net: dict[str, int] = {}
    for order in completed:
        fill = order.fill
        assert fill is not None
        bar_reference = fill.reference_price
        if fill.side is Side.BUY:
            assert fill.fill_price >= bar_reference
            net[fill.isin] = net.get(fill.isin, 0) + fill.quantity
        else:
            assert fill.fill_price <= bar_reference
            net[fill.isin] = net.get(fill.isin, 0) - fill.quantity
        # Signed exactly as the cost model says: a buy pays net_amount, a sell receives it (which
        # is negative when the charges exceed the proceeds — still the same number, still signed).
        expected = -fill.cost.net_amount if fill.side is Side.BUY else fill.cost.net_amount
        assert fill.net_cash == expected
    on_book = {h.isin: h.quantity for h in broker.holdings()}
    for p in broker.positions():
        on_book[p.isin] = on_book.get(p.isin, 0) + p.quantity
    for isin, quantity in net.items():
        assert quantity >= 0, f"{isin} went net short: {quantity}"
        assert on_book.get(isin, 0) == quantity
    for isin, quantity in on_book.items():
        assert quantity > 0
        assert net.get(isin, 0) == quantity


@given(stream=_stream())
@settings(max_examples=150, deadline=None)
def test_no_order_stream_breaks_the_book(stream: _Stream) -> None:
    _drive(stream)


@given(stream=_stream())
@settings(max_examples=60, deadline=None)
def test_the_same_stream_replays_byte_identically(stream: _Stream) -> None:
    first, first_orders, _ = _drive(stream)
    second, second_orders, _ = _drive(stream)
    assert first.ledger() == second.ledger()
    assert first.holdings() == second.holdings()
    assert first.positions() == second.positions()
    assert first.cash == second.cash
    assert [o.status for o in first_orders] == [o.status for o in second_orders]
