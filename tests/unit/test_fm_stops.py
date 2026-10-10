"""M17.7 — mechanical stops (Amendment 1 c): a close below a stop sells at the next open, no model.

Each test fails if its rule is inverted: a close at or above the stop must not sell and one below
it must; a stop can be raised but never lowered (directly, by a wider trail, by an add-on buy's
lower declaration); a trailing stop ratchets up and never down; a split rescales the level with
the shares; and the exit reaches the book as a ``STOP_EXIT`` sell that fills at the next open.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.fundmanager.books import BookOrder
from analyst.fundmanager.stops import (
    STOP_EXIT_EVENT,
    StopBook,
    StopKind,
    StopLoosenError,
    with_stop_exits,
)
from analyst.journal.models import Decision
from dataplatform.clock import FrozenClock
from execution.broker import Side
from tests.fm_books_support import Bar, FmMarket, ListJournal, open_book, switch_at
from tests.fm_books_support import weekdays as _weekdays

SESSIONS = _weekdays(date(2025, 1, 1), 30)
D0 = SESSIONS[19]
A = "INE00001A010"
B = "INE00002A010"


def _declared() -> StopBook:
    stops = StopBook()
    stops.declare(A, stop_pct=Decimal(10), reference_price=Decimal(1000), session=D0)
    return stops


def test_a_close_below_the_stop_exits_and_one_at_it_does_not() -> None:
    stops = _declared()
    assert stops.stops[A].level == Decimal(900)
    assert stops.on_close(D0, {A: Decimal(900)}, {A: 50}) == ()
    assert stops.on_close(D0, {A: Decimal("900.05")}, {A: 50}) == ()
    (exit_,) = stops.on_close(D0, {A: Decimal("899.95")}, {A: 50})
    assert (exit_.isin, exit_.quantity, exit_.level) == (A, 50, Decimal(900))
    order = exit_.order()
    assert (order.side, order.quantity, order.event) == (Side.SELL, 50, STOP_EXIT_EVENT)


def test_a_stop_only_tightens() -> None:
    stops = _declared()
    stops.tighten(A, level=Decimal(950), session=D0)
    with pytest.raises(StopLoosenError):
        stops.tighten(A, level=Decimal(949), session=D0)
    with pytest.raises(StopLoosenError):  # an add-on buy declaring a lower stop
        stops.declare(A, stop_pct=Decimal(10), reference_price=Decimal(1000), session=D0)
    assert stops.stops[A].level == Decimal(950)
    with pytest.raises(ValueError):
        stops.declare(B, stop_pct=Decimal(16), reference_price=Decimal(100), session=D0)


def test_a_trailing_stop_ratchets_up_and_never_down() -> None:
    stops = _declared()
    stop = stops.convert_to_trailing(A, trail_pct=Decimal(5), close=Decimal(1000), session=D0)
    assert stop.kind is StopKind.TRAILING and stop.level == Decimal(950)
    stops.on_close(SESSIONS[20], {A: Decimal(1100)}, {A: 50})
    assert stops.stops[A].level == Decimal(1045)
    stops.on_close(SESSIONS[21], {A: Decimal(1050)}, {A: 50})  # a lower close: level unchanged
    assert stops.stops[A].level == Decimal(1045)
    (exit_,) = stops.on_close(SESSIONS[22], {A: Decimal(1040)}, {A: 50})
    assert exit_.kind is StopKind.TRAILING and exit_.level == Decimal(1045)
    with pytest.raises(StopLoosenError):
        stops.convert_to_trailing(A, trail_pct=Decimal(8), close=Decimal(1040), session=D0)


def test_converting_a_tight_fixed_stop_never_lowers_it() -> None:
    stops = _declared()
    stops.tighten(A, level=Decimal(990), session=D0)
    stop = stops.convert_to_trailing(A, trail_pct=Decimal(5), close=Decimal(1000), session=D0)
    assert stop.level == Decimal(990)


def test_a_split_rescales_the_level_and_a_sold_name_loses_its_stop() -> None:
    stops = _declared()
    stops.rescale(A, numerator=Decimal(10), denominator=Decimal(2))  # x5 shares
    assert stops.stops[A].level == Decimal(180)
    assert stops.on_close(D0, {A: Decimal(190)}, {A: 250}) == ()
    assert stops.on_close(D0, {A: Decimal(100)}, {}) == ()
    assert A not in stops.stops


def test_an_open_exit_is_not_emitted_twice_and_a_missing_close_is_not_judged() -> None:
    stops = _declared()
    assert stops.on_close(D0, {A: Decimal(800)}, {A: 50}, exiting={A}) == ()
    assert stops.on_close(D0, {}, {A: 50}) == ()
    assert A in stops.stops


def test_the_stop_book_round_trips() -> None:
    stops = _declared()
    stops.convert_to_trailing(A, trail_pct=Decimal(5), close=Decimal(1000), session=D0)
    stops.declare(B, stop_pct=Decimal(3), reference_price=Decimal(200), session=D0)
    assert StopBook.from_document(stops.to_document()).stops == stops.stops


def test_stop_exits_come_first_and_replace_the_managers_order_on_that_name() -> None:
    stops = _declared()
    exits = stops.on_close(D0, {A: Decimal(800)}, {A: 50})
    manager = [BookOrder(A, Side.BUY, 5, "add"), BookOrder(B, Side.BUY, 5, "new")]
    merged = with_stop_exits(manager, exits)
    assert [(o.isin, o.side, o.event) for o in merged] == [
        (A, Side.SELL, STOP_EXIT_EVENT),
        (B, Side.BUY, "STAGED"),
    ]


def test_a_stop_exit_is_staged_journaled_and_filled_at_the_next_open(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    px = Decimal(1000)
    bars = {(A, s): Bar(px, px, Decimal("10000000000")) for s in SESSIONS}
    falling = SESSIONS[23]
    bars[(A, falling)] = Bar(px, Decimal(880), Decimal("10000000000"))
    market = FmMarket(SESSIONS, bars, {A: "IT"})
    book, account = open_book(
        "FM-SWING-BRK-10L",
        market=market,
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    stops = StopBook()
    book.execute(D0)
    book.decide(D0, [BookOrder(A, Side.BUY, 50, "buy with a 10% stop")])
    stops.declare(A, stop_pct=Decimal(10), reference_price=Decimal(1000), session=D0)
    for session in SESSIONS[20:24]:
        clock.freeze_at(session)
        book.execute(session)
        close = market.close(A, session)
        assert close is not None
        exits = stops.on_close(session, {A: close}, account.quantities())
        book.decide(session, with_stop_exits([], exits))
    sells = [e for e in journal.entries if e.decision is Decision.SELL]
    assert [(e.trading_date, e.payload["event"]) for e in sells] == [(falling, STOP_EXIT_EVENT)]
    clock.freeze_at(SESSIONS[24])
    report = book.execute(SESSIONS[24])
    assert [(f.isin, f.side, f.quantity) for f in report.fills] == [(A, Side.SELL, 50)]
    assert account.quantities() == {}
