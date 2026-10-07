"""``SimBroker.export_state`` / ``SimBroker.restore`` — a broker continued in another process.

The daily paper session (M13.1) lives one session per process, so it persists the broker's state
and restores it the next day. These tests pin that a restored broker is indistinguishable from the
one that never stopped: mid-settlement buys, sale proceeds in settlement, orders still staged, and
the id counters all carry over, and the two brokers then fill, settle and number identically.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from analyst.journal.evidence import canonical_bytes
from dataplatform.clock import FrozenClock
from execution.broker import OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker, SimBrokerState
from tests.paper_session_support import (
    ISINS,
    OCT_FIRST,
    OCT_FOURTH,
    OCT_SECOND,
    OCT_THIRD,
    FixtureWorld,
)

_A, _B, _C, _D = ISINS[:4]


def _market() -> object:
    return FixtureWorld().market(first=OCT_FIRST, through=OCT_FOURTH, held=lambda: ())


def _broker(clock: FrozenClock) -> SimBroker:
    return SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_market(),  # type: ignore[arg-type]
        opening_cash=Decimal("1000000"),
    )


def _mid_flight(clock: FrozenClock) -> SimBroker:
    """A broker with proceeds released ahead, a pending buy and a staged order all open at once."""
    broker = _broker(clock)
    clock.freeze_at(OCT_FIRST)
    broker.place(OrderRequest(isin=_A, side=Side.BUY, quantity=100))
    broker.place(OrderRequest(isin=_B, side=Side.BUY, quantity=50))
    clock.freeze_at(OCT_SECOND)
    broker.execute_session(OCT_SECOND)
    clock.freeze_at(OCT_THIRD)
    broker.execute_session(OCT_THIRD)  # settles
    broker.place(OrderRequest(isin=_A, side=Side.SELL, quantity=40))
    broker.place(OrderRequest(isin=_C, side=Side.BUY, quantity=30))
    clock.freeze_at(OCT_FOURTH)
    broker.execute_session(OCT_FOURTH)  # the sale's proceeds released ahead; the buy is pending
    broker.place(OrderRequest(isin=_D, side=Side.BUY, quantity=20))  # staged for tomorrow
    return broker


def _continue(broker: SimBroker, clock: FrozenClock) -> None:
    for day in (date(2026, 10, 8), date(2026, 10, 9)):
        clock.freeze_at(day)
        broker.execute_session(day)
    broker.place(OrderRequest(isin=_B, side=Side.SELL, quantity=10))


def test_a_restored_broker_continues_exactly_like_the_one_that_never_stopped() -> None:
    clock = FrozenClock(OCT_FIRST)
    original = _mid_flight(clock)
    state = original.export_state()
    # The fixture is genuinely mid-flight: every piece of carried state is non-trivial.
    assert state.staged and state.pending and state.holdings
    assert state.released_ahead > 0

    # Through JSON, as the ledger stores it.
    document = json.loads(json.dumps(state.to_document()))
    restored_clock = FrozenClock(OCT_FOURTH)
    restored = SimBroker.restore(
        SimBrokerState.from_document(document),
        clock=restored_clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_market(),  # type: ignore[arg-type]
    )
    assert restored.export_state() == state

    original_tail = len(original.ledger())
    _continue(original, clock)
    _continue(restored, restored_clock)

    assert restored.export_state() == original.export_state()
    assert restored.ledger() == original.ledger()[original_tail:]
    assert restored.margins() == original.margins()
    assert restored.holdings() == original.holdings()


def test_the_state_document_is_canonical_and_round_trips() -> None:
    state = _mid_flight(FrozenClock(OCT_FIRST)).export_state()
    document = state.to_document()
    assert SimBrokerState.from_document(document) == state
    assert canonical_bytes(document) == canonical_bytes(
        SimBrokerState.from_document(json.loads(json.dumps(document))).to_document()
    )
    # Money never travels as a JSON number.
    assert isinstance(document["cash"], str)
    assert all(isinstance(lot["cost"], str) for lot in document["holdings"])
