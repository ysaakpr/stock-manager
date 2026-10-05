"""Buys are sized to A8's per-order ceiling, so refused buys cannot compound into an all-cash book.

The measured failure (swing composite 2016-09 -> 2026-08 with exit slicing): exits completed, the
freed cash came back as equal-weight buys of ``free cash / top_n`` per name, every one above the
₹1.2L ``MAX_ORDER_VALUE`` cap. A8 refused them, the cash stayed idle, the next rebalance spread the
larger idle balance over the same names, and from 2023 every buy of every rebalance was refused
(1,638 blocks) while the book drifted to cash. Each test here fails if the ceiling is removed:

* A8 derives the ceiling from the same two caps ``check_order`` enforces;
* the shared SIP allocator never sizes an order past it, and leaves the rest as cash;
* through the real stack (``SwingCompositePolicy`` -> ``ReplayEngine`` -> ``RailGate`` ->
  ``SimBroker``), a ₹30L book whose equal weight is ₹2.94L a name is deployed across rebalances in
  orders of at most ₹1.2L, with no ``MAX_ORDER_VALUE`` block — and A8 still refuses an over-cap buy;
* the backtest driver hands the policy the very rails the gate enforces.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

from analyst.journal.models import Decision, JournalEntry
from analyst.rails import RailId, order_value_ceiling
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.swing_composite import SwingCompositeParameters, SwingCompositePolicy
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap, ratified_backtest_rail_policy
from backtest.replay import ReplayEngine
from backtest.run import _AccountingBroker
from backtest.sip import simulate_sip_instalment
from dataplatform.clock import FrozenClock
from execution.broker import Exchange, Holding, Position, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import marks_from
from tests.unit.test_swing_composite import _Data, _rec

RAILS = ratified_backtest_rail_policy().rails
CAP = Decimal("120000")  # the ratified ₹1,20,000 — asserted below, never edited here
NAMES = tuple(f"INE{index:03d}A01010" for index in range(1, 11))
SESSIONS = tuple(date(2024, 1, day) for day in (1, 2, 3, 4, 5, 8, 9, 10))
PRICE = Decimal("100")
OPENING = Decimal("3000000")


# ── A8's ceiling ─────────────────────────────────────────────────────────────────────────────────


def test_the_ratified_rupee_cap_is_unchanged() -> None:
    assert RAILS.max_order_value_inr == CAP


def test_the_ceiling_is_the_rupee_cap_on_a_large_book_and_the_pct_cap_on_a_small_one() -> None:
    assert order_value_ceiling(RAILS, Decimal("3000000")) == CAP  # 15% would be ₹4.5L
    assert order_value_ceiling(RAILS, Decimal("400000")) == Decimal("60000")  # 15% of ₹4L
    assert order_value_ceiling(RAILS, Decimal("0")) == CAP


# ── the shared allocator ─────────────────────────────────────────────────────────────────────────


def test_the_allocator_never_sizes_an_order_past_the_ceiling_and_keeps_the_rest_as_cash() -> None:
    targets = {NAMES[0]: Decimal("0.5"), NAMES[1]: Decimal("0.5")}
    prices = {NAMES[0]: PRICE, NAMES[1]: Decimal("37")}
    capped = simulate_sip_instalment(
        instalment=Decimal("500000"), targets=targets, prices=prices, order_ceiling=CAP
    )
    assert capped.orders and all(order.cost <= CAP for order in capped.orders)
    assert [order.quantity for order in capped.orders] == [1200, 3243]
    assert capped.residual_cash == Decimal("500000") - Decimal("120000") - Decimal("119991")

    uncapped = simulate_sip_instalment(instalment=Decimal("500000"), targets=targets, prices=prices)
    assert max(order.cost for order in uncapped.orders) > CAP


# ── the real stack ───────────────────────────────────────────────────────────────────────────────


class _Market:
    def __init__(self, sessions: tuple[date, ...]) -> None:
        self._calendar = (*sessions, date(2024, 12, 31))

    def next_session(self, after: date) -> date:
        return next(session for session in self._calendar if session > after)

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if isin not in NAMES:
            raise NoReferenceBarError(isin)
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=PRICE,
            vwap=PRICE,
            traded_value=Decimal("100000000000"),
        )


def _replay(*, order_caps: bool) -> tuple[_AccountingBroker, list[JournalEntry]]:
    records = tuple(_rec(isin, price=str(PRICE)) for isin in NAMES)
    prices: Mapping[tuple[str, date], Decimal] = {
        (isin, day): PRICE for isin in NAMES for day in SESSIONS
    }
    clock = FrozenClock(SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(SESSIONS),
        opening_cash=OPENING,
    )
    book = PortfolioBook()
    book.deposit(SESSIONS[0], OPENING)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        # One sector per name, so only the order-size caps are in play.
        sectors=SectorMap(
            source="test", sha256="test", by_isin={isin: isin[3:6] for isin in NAMES}
        ),
        provenance="test",
    )
    policy = SwingCompositePolicy(
        _Data(records),
        SwingCompositeParameters(top_n=10, exclude_vol_fraction=Decimal("0")),
        order_caps=rail_policy.rails if order_caps else None,
    )
    result = ReplayEngine(
        policy=policy,
        broker=broker,
        clock=clock,
        sessions=SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()
    blocks = [entry for entry in result.journal if entry.decision is Decision.RAIL_BLOCK]
    return broker, blocks


def test_buys_sized_to_the_ceiling_deploy_the_book_with_no_order_value_block() -> None:
    broker, blocks = _replay(order_caps=True)

    assert [b for b in blocks if RailId.MAX_ORDER_VALUE.value in b.payload["rails"]] == []
    buys = [fill for fill in broker.fills if fill.side is Side.BUY]
    assert buys and all(fill.reference_price * fill.quantity <= CAP for fill in buys)
    held: dict[str, int] = {}
    lots: list[Holding | Position] = [*broker.holdings(), *broker.positions()]
    for lot in lots:
        held[lot.isin] = held.get(lot.isin, 0) + lot.quantity
    # Every name is built past one order's worth by top-ups on later rebalances...
    assert set(held) == set(NAMES)
    assert all(quantity * PRICE > CAP for quantity in held.values())
    # ...and the book ends invested, not parked in cash.
    invested = sum((quantity * PRICE for quantity in held.values()), Decimal(0))
    assert invested >= OPENING * Decimal("0.9")


def test_without_the_ceiling_every_buy_is_refused_and_the_book_stays_in_cash() -> None:
    """The failure the ceiling exists for: A8 still refuses every over-cap buy, every session."""
    broker, blocks = _replay(order_caps=False)
    refused = [b for b in blocks if RailId.MAX_ORDER_VALUE.value in b.payload["rails"]]
    assert len(refused) == len(NAMES) * len(SESSIONS)
    assert [fill for fill in broker.fills if fill.side is Side.BUY] == []


def test_the_backtest_driver_sizes_swing_buys_to_the_rails_the_gate_enforces() -> None:
    """Every ``SwingCompositePolicy`` the driver builds is handed ``order_caps``."""
    source = (Path(__file__).resolve().parents[2] / "backtest" / "run.py").read_text("utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SwingCompositePolicy"
    ]
    assert calls
    for call in calls:
        assert "order_caps" in {keyword.arg for keyword in call.keywords}
