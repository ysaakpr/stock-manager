"""M17 anomaly A5: a special session is not a decision session (owner ruling, 2026-11-23).

NSE held its one-hour Diwali Muhurat session on Sun 8 Nov 2026, and L1 has bars for it. The owner
ruled (HUMAN_DECISIONS.md D45) that such a session is neither a session the desk decides on nor
one a staged order fills at: an order staged on the session before it fills at the next *regular*
session's open. The M17.8 dry run showed exactly that — the Fri 6 Nov `FM-SWING-BRK` order for
INE068V01023 filled at the Mon 9 Nov open, ₹3,147.00 (ops/gates/M17-dry-run-2026-11-13.md, A5).

These tests pin it on the production pieces — the checked-in holiday calendar, `LakeM17World`
(the desk's calendar and its fill market) and `SimBroker` — so a change that let the Muhurat date
into the desk's session list (say, `expected_data_dates` in place of `expected_sessions`) fails
here. Offline: an empty lake under ``tmp_path``; the one fill reads a synthetic 9 Nov bar.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.fm_job import owed_m17_session
from backtest.fm_world import LakeM17World
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.calendar import DayKind, TradingCalendar, trading_calendar
from execution.broker import OrderRequest, OrderStatus, Side
from execution.costs import CostModel, Exchange, load_rate_card
from execution.sim_broker import ReferenceBar, SessionMarket, SimBroker

FRIDAY = date(2026, 11, 6)  # the decision session before Muhurat
MUHURAT = date(2026, 11, 8)  # Sun: Diwali Laxmi Pujan, the one-hour special session
MONDAY = date(2026, 11, 9)  # the next regular session
ISIN = "INE068V01023"  # the A5 order's name
MONDAY_OPEN = Decimal("3147.00")


@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    return trading_calendar()


@pytest.fixture
def world(tmp_path: Path, calendar: TradingCalendar) -> Iterator[LakeM17World]:
    clock = FrozenClock(datetime.combine(FRIDAY, time(22, 0), tzinfo=IST))
    lake = LakeM17World(
        data_root=tmp_path, settings=None, clock=clock, listings=None, calendar=calendar
    )
    yield lake
    lake.close()


def test_the_muhurat_date_is_a_special_session_that_publishes_data(
    calendar: TradingCalendar,
) -> None:
    """The premise: 8 Nov is a real session with data, so only the rule keeps it out."""
    assert calendar.classify(MUHURAT) is DayKind.MUHURAT
    assert MUHURAT in calendar.expected_data_dates(FRIDAY, MONDAY)
    assert calendar.expected_sessions(FRIDAY, MONDAY) == [FRIDAY, MONDAY]


def test_the_desk_does_not_decide_on_a_special_session(world: LakeM17World) -> None:
    assert not world.is_session(MUHURAT)
    assert world.sessions(FRIDAY, MONDAY) == [FRIDAY, MONDAY]
    # the deadline and the next decision both sit on the next regular session
    assert world.next_session(FRIDAY) == MONDAY
    assert world.previous_session(MONDAY) == FRIDAY


def test_a_fire_on_the_special_session_evening_owes_no_special_session(
    world: LakeM17World,
) -> None:
    """A run on the Muhurat evening (a weekday Muhurat beats the Mon-Fri cron) owes Friday."""
    evening = datetime.combine(MUHURAT, time(22, 0), tzinfo=IST)
    assert owed_m17_session(world, evening) == FRIDAY


class _MondayBar:
    """The desk's own fill market for the calendar, with a synthetic bar for 9 Nov only."""

    def __init__(self, market: SessionMarket) -> None:
        self._market = market

    def next_session(self, after: date) -> date:
        return self._market.next_session(after)

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        assert session == MONDAY, f"asked for a {session.isoformat()} bar; only 9 Nov fills"
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=MONDAY_OPEN,
            vwap=Decimal("3160.00"),
            traded_value=Decimal("5000000000"),
        )


def test_an_order_staged_before_a_special_session_fills_at_the_next_regular_open(
    world: LakeM17World,
) -> None:
    clock = FrozenClock(datetime.combine(FRIDAY, time(22, 0), tzinfo=IST))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=_MondayBar(world.fill_market(lambda: ())),
        opening_cash=Decimal("1000000"),
    )
    staged = broker.place(OrderRequest(isin=ISIN, side=Side.BUY, quantity=3))

    assert staged.target_session == MONDAY  # not the Muhurat session
    assert broker.execute_session(MUHURAT) == ()  # nothing is due on the special session
    assert broker.order(staged.order_id).status is OrderStatus.STAGED

    (filled,) = broker.execute_session(MONDAY)
    assert filled.status is OrderStatus.COMPLETE
    assert filled.fill is not None
    assert filled.fill.session == MONDAY
    assert filled.fill.reference_price == MONDAY_OPEN  # the 9 Nov open, as in the dry run
