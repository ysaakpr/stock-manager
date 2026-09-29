"""X2 x A8 under settlement and corporate actions — the rail book is the book the broker holds.

The replay's rail book is rebuilt from the broker each session, after the session's corporate
actions have been applied (``_AccountingBroker``) and with bought lots still in settlement counted
as exposure. These tests walk the real stack the drivers use — ``ReplayEngine`` -> ``RailGate`` ->
``_AccountingBroker`` -> ``SimBroker`` + ``PortfolioBook`` — and each fails if the projection
reads a stale or partial book:

* after a 2:1 split on the ex-date, a SELL of the post-split count is allowed and filled, not
  escalated as unexecutable (a pre-split projection would see half the shares);
* in the T+2 era (2019) a name bought the day before yesterday is still pending, and it counts
  toward ``MAX_POSITION`` today (a projection over settled holdings alone would let the book
  double it).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar, RescaleKind, ShareRescale
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap, ratified_backtest_rail_policy
from backtest.replay import ReplayEngine, ReplayResult, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from dataplatform.clock import FrozenClock
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import marks_from

A = "INE001A01036"
B = "INE002A01018"
_CASH = Decimal("100000")


class _Market:
    """A ``SessionMarket`` over a price table: open = vwap = close = ``prices[(isin, session)]``."""

    def __init__(self, prices: Mapping[tuple[str, date], Decimal], sessions: tuple[date, ...]):
        self.prices = dict(prices)
        self._calendar = (*sessions, date(sessions[-1].year, 12, 31))

    def next_session(self, after: date) -> date:
        for session in self._calendar:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        price = self.prices.get((isin, session))
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


class _Scripted:
    """Places the scripted orders on their session, and records the broker's book it decided on."""

    def __init__(self, orders: Mapping[date, tuple[OrderRequest, ...]]) -> None:
        self._orders = orders
        self.settled: dict[date, dict[str, int]] = {}
        self.pending: dict[date, dict[str, int]] = {}

    def decide(self, ctx: SessionContext) -> SessionDecision:
        self.settled[ctx.session] = {h.isin: h.quantity for h in ctx.broker.holdings()}
        self.pending[ctx.session] = {p.isin: p.quantity for p in ctx.broker.positions()}
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(EvidenceItem(kind=EvidenceKind.PRICE, source="t", label="x", value=Decimal(1)),),
        )
        return SessionDecision(evidence=evidence, orders=self._orders.get(ctx.session, ()))


def _policy() -> BacktestRailPolicy:
    """The ratified caps; the test names are unlabelled, so they pool as UNKNOWN."""
    return BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=ratified_backtest_rail_policy().rails,
        sectors=SectorMap(source="test", sha256="test", by_isin={}),
        provenance="test",
    )


def _walk(
    prices: Mapping[tuple[str, date], Decimal],
    sessions: tuple[date, ...],
    policy: _Scripted,
    actions: BookActionCalendar,
) -> tuple[ReplayResult, SimBroker]:
    clock = FrozenClock(sessions[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(prices, sessions),
        opening_cash=_CASH,
    )
    book = PortfolioBook()
    book.deposit(sessions[0], _CASH)
    broker = _AccountingBroker(sim, book, corporate_actions=actions)
    result = ReplayEngine(
        policy=policy,
        broker=broker,
        clock=clock,
        sessions=sessions,
        rails=RailGate(_policy(), marks_from(prices)),
    ).run()
    return result, sim


def _buy(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.BUY, quantity=quantity)


def _sell(isin: str, quantity: int) -> OrderRequest:
    return OrderRequest(isin=isin, side=Side.SELL, quantity=quantity)


def _decisions(result: ReplayResult, day: date) -> list[JournalEntry]:
    return [e for e in result.journal if e.trading_date == day]


# ── a split on a held name: the projection sees the post-split count ────────────────────────────

D1, D2, D3, D4, D5 = (date(2024, 1, d) for d in (1, 2, 3, 4, 5))
T1_ERA = (D1, D2, D3, D4, D5)


def test_after_a_split_a_sell_of_the_post_split_count_is_allowed_not_escalated() -> None:
    # A: ₹100 -> ₹50 on its 2:1 ex-date D3. B is a flat second name so the exit is not a sell-out.
    prices = {(A, d): (Decimal("100") if d < D3 else Decimal("50")) for d in T1_ERA}
    prices |= {(B, d): Decimal("100") for d in T1_ERA}
    split = ShareRescale(A, D3, RescaleKind.SPLIT, Decimal("10"), Decimal("5"))
    policy = _Scripted({D1: (_buy(A, 100), _buy(B, 100)), D3: (_sell(A, 200),)})
    result, sim = _walk(prices, T1_ERA, policy, BookActionCalendar([split]))

    # On the ex-date the policy (and the rail book) saw the rescaled count.
    held_on_ex = policy.settled[D3].get(A, 0) + policy.pending[D3].get(A, 0)
    assert held_on_ex == 200
    on_ex = _decisions(result, D3)
    assert not [e for e in on_ex if e.decision in (Decision.ESCALATE, Decision.RAIL_BLOCK)]
    # The whole post-split position was sold, and only B is left.
    assert sim.held_quantity(A) == 0
    assert sim.held_quantity(B) == 100


def test_without_the_split_the_same_sell_is_escalated_as_unexecutable() -> None:
    """The contrast: a pre-split book holds 100, so a sell of 200 cannot be put to A8."""
    prices = {(A, d): Decimal("100") for d in T1_ERA} | {(B, d): Decimal("100") for d in T1_ERA}
    policy = _Scripted({D1: (_buy(A, 100), _buy(B, 100)), D3: (_sell(A, 200),)})
    result, sim = _walk(prices, T1_ERA, policy, BookActionCalendar())
    [escalation] = [e for e in _decisions(result, D3) if e.decision is Decision.ESCALATE]
    assert escalation.payload is not None and "exceeds the 100 held" in escalation.payload["reason"]
    assert sim.held_quantity(A) == 100


# ── T+2 (2019): a pending lot is exposure ────────────────────────────────────────────────────────

T1, T2, T3, T4, T5 = (date(2019, 3, d) for d in (4, 5, 6, 7, 8))
T2_ERA = (T1, T2, T3, T4, T5)


def test_t2_a_pending_lot_counts_toward_max_position() -> None:
    # Decided T1, filled T2, deliverable T4 (T+2): on T3 the 10% lot is still pending. A second
    # 10% buy on T3 takes the name to ~20% of the book — over the 15% cap — and must be refused.
    prices = {(A, d): Decimal("100") for d in T2_ERA}
    policy = _Scripted({T1: (_buy(A, 100),), T3: (_buy(A, 100),)})
    result, sim = _walk(prices, T2_ERA, policy, BookActionCalendar())

    assert policy.settled[T3] == {}  # nothing delivered yet: this is the T+2 era
    assert policy.pending[T3] == {A: 100}
    [block] = [e for e in _decisions(result, T3) if e.decision is Decision.RAIL_BLOCK]
    assert block.isin == A
    assert block.payload is not None and "MAX_POSITION" in block.payload["rails"].split(",")
    assert sim.held_quantity(A) == 100  # the second buy never reached the broker


def test_t2_a_second_name_bought_while_the_first_is_pending_is_allowed() -> None:
    """The control: the cap binds on the pending name's exposure, not on having pending lots."""
    prices = {(A, d): Decimal("100") for d in T2_ERA} | {(B, d): Decimal("100") for d in T2_ERA}
    policy = _Scripted({T1: (_buy(A, 100),), T3: (_buy(B, 100),)})
    result, sim = _walk(prices, T2_ERA, policy, BookActionCalendar())
    assert policy.pending[T3] == {A: 100}
    assert not [e for e in result.journal if e.decision is Decision.RAIL_BLOCK]
    assert sim.held_quantity(B) == 100
