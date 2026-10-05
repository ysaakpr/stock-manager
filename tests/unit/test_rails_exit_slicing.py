"""A8: an exit too large for the per-order caps is sliced into children, never trapped.

The fat-finger caps (``max_order_value_inr``, ``max_order_pct_of_case``) bound what one order may
carry. Before slicing they also bound whether a held position could be *left*: once a position
grew past ₹1.2L, its full-quantity exit — a trailing stop included — was refused session after
session. Each test here fails if the slicing is removed or turned the wrong way:

* a ₹2L position whose trailing stop fires is fully exited in one session as two capped children,
  through the real stack (``ReplayEngine`` -> ``RailGate`` -> ``_AccountingBroker`` ->
  ``SimBroker``), charged one DP fee, and journalled as one SELL naming both children;
* a ₹2L buy is still refused whole for ``MAX_ORDER_VALUE``;
* a sell larger than the held quantity is not sliced (and is not placed);
* every child is within both per-order caps, and the children sum to the parent;
* a child refused by another rail is journalled as a ``RAIL_BLOCK`` naming that rail, and no child
  of that exit is placed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from analyst.cases import RiskRails
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, RecordedEntry, Sleeve
from analyst.rails import (
    Lot,
    Portfolio,
    ProposedOrder,
    RailEngine,
    RailId,
    check_order,
    max_child_quantity,
    slice_exit,
)
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap, ratified_backtest_rail_policy
from backtest.replay import ReplayEngine, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from dataplatform.clock import FrozenClock
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import marks_from

A = "INE001A01036"
FILLERS = tuple(f"INE{index:03d}A01010" for index in range(2, 10))
CAP = ratified_backtest_rail_policy().rails.max_order_value_inr  # ₹1,20,000
CASE_ID = "CASE-SLICE"


def _rails(**caps: Decimal | int) -> RiskRails:
    return ratified_backtest_rail_policy().rails.model_copy(update=caps)


def _order(isin: str, side: Side, quantity: int, price: str = "100") -> ProposedOrder:
    return ProposedOrder(
        request=OrderRequest(isin=isin, side=side, quantity=quantity, exchange=Exchange.NSE),
        price=Decimal(price),
        sector="IT",
    )


def _book(lots: Mapping[str, int], cash: str, price: str = "100") -> Portfolio:
    return Portfolio(
        case_id=CASE_ID,
        lots=tuple(
            Lot(isin=isin, sector="IT", quantity=quantity, price=Decimal(price))
            for isin, quantity in lots.items()
        ),
        cash=Decimal(cash),
    )


class _ListJournal:
    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []

    def append(self, entry: JournalEntry) -> RecordedEntry:
        self.entries.append(entry)
        return RecordedEntry(**entry.model_dump(), id=len(self.entries), recorded_at=entry.ts)


def _engine() -> tuple[RailEngine, _ListJournal]:
    journal = _ListJournal()
    return RailEngine(journal, clock=FrozenClock(date(2024, 1, 2))), journal


# ── the pure slicer ────────────────────────────────────────────────────────────────────────────


def test_an_over_cap_exit_is_cut_into_the_fewest_children_each_within_both_caps() -> None:
    rails = _rails()
    book = _book({A: 2500}, cash="2000000")  # ₹2.5L in A, ₹22.5L case
    parent = _order(A, Side.SELL, 2500)
    assert not check_order(parent, book, rails).allowed  # the whole exit trips MAX_ORDER_VALUE

    children = slice_exit(parent, book, rails)

    assert max_child_quantity(parent, book, rails) == 1200  # ₹1,20,000 / ₹100, exactly
    assert [child.quantity for child in children] == [834, 833, 833]
    assert sum(child.quantity for child in children) == parent.quantity
    for child in children:
        assert child.side is Side.SELL and child.isin == A and child.price == parent.price
        assert child.value <= rails.max_order_value_inr
        assert child.value * 100 <= rails.max_order_pct_of_case * book.total_value


def test_the_pct_cap_sizes_the_children_when_it_binds_first() -> None:
    rails = _rails(max_order_pct_of_case=Decimal("5"))
    book = _book({A: 2000}, cash="800000")  # ₹10L case: 5% is ₹50k, under the ₹1.2L cap
    children = slice_exit(_order(A, Side.SELL, 2000), book, rails)
    assert [child.quantity for child in children] == [500, 500, 500, 500]
    assert all(check_order(child, book, rails).allowed for child in children)


def test_a_buy_is_never_sliced_and_a_2l_buy_is_still_refused() -> None:
    rails = _rails()
    # A already held, in a larger quantity than the buy: nothing but the side keeps it whole.
    book = _book({A: 3000}, cash="3000000")
    buy = _order(A, Side.BUY, 2000)  # ₹2L
    assert slice_exit(buy, book, rails) == (buy,)

    engine, journal = _engine()
    clearance = engine.guard_exit(buy, book, rails, trading_date=date(2024, 1, 2))
    assert clearance.allowed == ()
    assert clearance.children == (buy,)
    [block] = journal.entries
    assert block.decision is Decision.RAIL_BLOCK
    assert RailId.MAX_ORDER_VALUE.value in block.payload["rails"].split(",")
    assert not engine.guard_order(buy, book, rails, trading_date=date(2024, 1, 2)).allowed


def test_a_sell_larger_than_the_held_quantity_is_not_sliced() -> None:
    rails = _rails()
    book = _book({A: 1500}, cash="2000000")
    oversell = _order(A, Side.SELL, 2500)
    assert slice_exit(oversell, book, rails) == (oversell,)
    unheld = _order(FILLERS[0], Side.SELL, 2500)
    assert slice_exit(unheld, book, rails) == (unheld,)


def test_an_exit_within_the_caps_is_left_whole() -> None:
    rails = _rails()
    book = _book({A: 1000}, cash="2000000")
    sell = _order(A, Side.SELL, 1000)  # ₹1L
    assert slice_exit(sell, book, rails) == (sell,)


def test_a_share_priced_above_the_cap_cannot_be_sliced_and_is_refused_whole() -> None:
    rails = _rails()
    book = _book({A: 2}, cash="2000000", price="150000")
    sell = _order(A, Side.SELL, 2, price="150000")
    assert slice_exit(sell, book, rails) == (sell,)
    engine, _ = _engine()
    assert engine.guard_exit(sell, book, rails, trading_date=date(2024, 1, 2)).allowed == ()


def test_a_child_refused_by_another_rail_is_journalled_and_no_child_is_placed() -> None:
    rails = _rails()
    # Exactly at the eight-name floor: the exit's last child closes A and drops the book to seven.
    book = _book({A: 2000, **dict.fromkeys(FILLERS[:7], 100)}, cash="2000000")
    engine, journal = _engine()

    clearance = engine.guard_exit(
        _order(A, Side.SELL, 2000),
        book,
        rails,
        trading_date=date(2024, 1, 2),
        sleeve=Sleeve.TACTICAL,
    )

    assert clearance.sliced
    assert clearance.allowed == ()
    assert clearance.blocked is not None
    assert clearance.blocked.breached_rails == (RailId.MIN_HOLDINGS,)
    [block] = journal.entries
    assert block.decision is Decision.RAIL_BLOCK and block.actor is Actor.RAILS
    assert block.isin == A and block.sleeve is Sleeve.TACTICAL
    assert block.payload["rails"] == RailId.MIN_HOLDINGS.value
    assert block.payload["exit_child"] == "2/2"
    assert block.payload["exit_parent_quantity"] == "2000"


def test_a_sliced_exit_clears_every_child_and_journals_nothing() -> None:
    rails = _rails()
    book = _book({A: 2000}, cash="2000000")
    engine, journal = _engine()
    clearance = engine.guard_exit(
        _order(A, Side.SELL, 2000), book, rails, trading_date=date(2024, 1, 2)
    )
    assert [child.quantity for child in clearance.allowed] == [1000, 1000]
    assert journal.entries == []
    assert clearance.payload() == {
        "exit_parent_quantity": "2000",
        "exit_children": "1000,1000",
        "exit_children_allowed": "2",
        "exit_reference_price": "100",
    }


# ── the real stack: a trailing stop on a ₹2L position exits in one session ─────────────────────

D = tuple(date(2024, 1, day) for day in (1, 2, 3, 4, 5, 8, 9))
_CASH = Decimal("2000000")


class _Market:
    """open = vwap = close = ``prices[(isin, session)]``, deep enough that impact is ~base bps."""

    def __init__(self, prices: Mapping[tuple[str, date], Decimal], sessions: tuple[date, ...]):
        self._prices = dict(prices)
        self._calendar = (*sessions, date(2024, 12, 31))

    def next_session(self, after: date) -> date:
        return next(session for session in self._calendar if session > after)

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        price = self._prices.get((isin, session))
        if price is None:
            raise NoReferenceBarError(f"{isin} {session}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=price,
            vwap=price,
            traded_value=Decimal("1000000000"),
        )


class _TrailingStop:
    """Builds 2,000 A in two ₹1L buys, then exits the whole position 10% under its peak close."""

    def __init__(self, closes: Mapping[date, Decimal]) -> None:
        self._closes = closes
        self._peak = Decimal(0)

    def decide(self, ctx: SessionContext) -> SessionDecision:
        close = self._closes[ctx.session]
        held = sum(h.quantity for h in ctx.broker.holdings()) + sum(
            p.quantity for p in ctx.broker.positions()
        )
        orders: tuple[OrderRequest, ...] = ()
        if ctx.session in (D[0], D[1]):
            orders = (OrderRequest(isin=A, side=Side.BUY, quantity=1000),)
        elif held:
            self._peak = max(self._peak, close)
            if close < self._peak * Decimal("0.9"):
                orders = (OrderRequest(isin=A, side=Side.SELL, quantity=held),)
        entries = tuple(
            JournalEntry(
                ts=ctx.clock.now(),
                trading_date=ctx.session,
                actor=Actor.T0,
                decision=Decision.BUY if order.side is Side.BUY else Decision.SELL,
                isin=order.isin,
                sleeve=Sleeve.TACTICAL,
                rationale="trailing stop" if order.side is Side.SELL else "entry",
            )
            for order in orders
        )
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(EvidenceItem(kind=EvidenceKind.PRICE, source="t", label="A", value=close),),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)


def _stop_run() -> tuple[list[JournalEntry], _AccountingBroker]:
    closes = dict(
        zip(D, map(Decimal, ("100", "100", "100", "120", "105", "105", "105")), strict=True)
    )
    prices = {(A, day): close for day, close in closes.items()}
    clock = FrozenClock(D[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(prices, D),
        opening_cash=_CASH,
    )
    book = PortfolioBook()
    book.deposit(D[0], _CASH)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=ratified_backtest_rail_policy().rails,
        sectors=SectorMap(source="test", sha256="test", by_isin={}),
        provenance="test",
    )
    result = ReplayEngine(
        policy=_TrailingStop(closes),
        broker=broker,
        clock=clock,
        sessions=D,
        rails=RailGate(policy, marks_from(prices)),
    ).run()
    return list(result.journal), broker


def test_a_2l_position_whose_trailing_stop_fires_is_exited_in_one_session_by_two_children() -> None:
    journal, broker = _stop_run()

    # The stop fires on D[4] (105 < 0.9 x 120) for all 2,000 shares: ₹2.1L at the reference close,
    # over the ₹1.2L cap, so it goes out as two children of ₹1.05L each.
    [stop] = [e for e in journal if e.decision is Decision.SELL]
    assert stop.trading_date == D[4]
    assert stop.payload["exit_parent_quantity"] == "2000"
    assert stop.payload["exit_children"] == "1000,1000"
    assert stop.payload["exit_children_allowed"] == "2"
    assert not [e for e in journal if e.decision is Decision.RAIL_BLOCK]

    sells = [fill for fill in broker.fills if fill.side is Side.SELL]
    assert [fill.quantity for fill in sells] == [1000, 1000]
    assert {fill.session for fill in sells} == {D[5]}  # both children in the one next session
    assert all(fill.reference_price * fill.quantity <= CAP for fill in sells)
    assert broker.holdings() == () and broker.positions() == ()
    # The depository bills the scrip once for the day, however many children delivered it.
    dp = [fill.cost.depository_charge for fill in sells]
    assert dp[0] > 0 and dp[1] == 0


def test_the_children_pay_slippage_on_their_combined_participation() -> None:
    _, broker = _stop_run()
    first, second = (fill for fill in broker.fills if fill.side is Side.SELL)
    # Same bps for both, and it is the bps of the whole 2,000-share exit, not of 1,000 shares.
    assert first.slippage_bps == second.slippage_bps
    participation = Decimal(2000) * Decimal(105) / Decimal("1000000000")
    assert first.slippage_bps == Decimal(2) + Decimal(50) * participation
