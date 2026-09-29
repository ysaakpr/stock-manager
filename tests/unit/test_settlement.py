"""X1 — the dated settlement cycle, and `SimBroker` honouring it.

What is under test:

1. **The schedule.** T+2 from 2003-04-01, the 2022-02-25..2023-01-26 phase-in held at T+2
   (reconstructed, conservative), T+1 from 2023-01-27. Every boundary is pinned on both sides, and
   a date before the schedule raises `NoSettlementCycleError` — never a borrowed later cycle.
2. **Trading sessions, not calendar days.** A 2019 sale on the eve of Holi settles T+2 on the
   exchange calendar (`dataplatform.ingest.calendar`), skipping the holiday and the weekend.
3. **Sale proceeds are not spendable before settlement**, and bought shares not deliverable. A buy
   that needs the proceeds is rejected until the cycle has paid them out.

Each broker scenario is also run against a schedule with the eras *inverted* or *ignored* and must
come out differently, so the file fails if the schedule stops deciding. Offline and deterministic:
the market is in memory and the clock frozen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.ingest.calendar import trading_calendar
from execution.broker import Order, OrderRequest, OrderStatus
from execution.costs import CostModel, Side, load_rate_card
from execution.settlement import (
    Approximation,
    NoSettlementCycleError,
    Provenance,
    SettlementSchedule,
    load_settlement_schedule,
)
from execution.sim_broker import ReferenceBar, SimBroker
from tests.unit.test_sim_broker import INFY, TCS, InMemoryMarket, _bar

# ── the schedule ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("trade_date", "lag"),
    [
        (date(2003, 4, 1), 2),  # first T+2 trade date (SEBI Circular No. 19, 2003-03-04)
        (date(2019, 3, 20), 2),
        (date(2022, 2, 24), 2),  # the day before the first T+1 tranche
        (date(2022, 2, 25), 2),  # phase-in: conservatively still T+2 for every security
        (date(2023, 1, 26), 2),  # the last phase-in day
        (date(2023, 1, 27), 1),  # final tranche: everything T+1
        (date(2024, 1, 3), 1),
    ],
)
def test_the_cycle_in_force_on_each_side_of_every_boundary(trade_date: date, lag: int) -> None:
    assert load_settlement_schedule().lag_for(trade_date, INFY) == lag


def test_a_date_before_the_schedule_raises_rather_than_borrowing_a_later_cycle() -> None:
    with pytest.raises(NoSettlementCycleError, match="2003-03-31"):
        load_settlement_schedule().lag_for(date(2003, 3, 31), INFY)


def test_every_era_is_sourced_and_the_reconstructed_one_errs_conservatively() -> None:
    eras = load_settlement_schedule().eras
    assert [era.id for era in eras] == ["t2-rolling", "t1-phase-in", "t1"]
    for era in eras:
        assert era.sources, era.id
        assert all("http" in source for source in era.sources), era.id
    phase_in = eras[1]
    assert phase_in.provenance is Provenance.RECONSTRUCTED
    assert phase_in.approximation is Approximation.CONSERVATIVE
    # Conservative means never faster than any real security settled in the window.
    assert phase_in.lag_sessions >= eras[0].lag_sessions


def test_a_reconstructed_era_must_state_its_direction() -> None:
    raw = _schedule_dict({"2003-04-01": 2}, provenance="reconstructed")
    with pytest.raises(ValueError, match="direction"):
        SettlementSchedule.model_validate(raw)


def test_eras_out_of_order_are_refused() -> None:
    raw = _schedule_dict({"2023-01-27": 1, "2003-04-01": 2})
    with pytest.raises(ValueError, match="ascending"):
        SettlementSchedule.model_validate(raw)


# ── the broker: 2019, T+2 across Holi ──────────────────────────────────────────────────────────

#: 2019-03-21 (Thu) is Holi on the NSE calendar. A sale on Wed 2019-03-20 settles T+2 in trading
#: sessions on Mon 2019-03-25 — not Fri 03-22 (T+2 in calendar days, or T+1 in sessions).
SELL_2019: Final[date] = date(2019, 3, 20)
HOLI_2019: Final[date] = date(2019, 3, 21)
T1_2019: Final[date] = date(2019, 3, 22)
T2_2019: Final[date] = date(2019, 3, 25)

#: A pre-2020-07-01 trade needs the account's state for stamp duty.
_STATE: Final[str] = "KA"


def _sessions(start: date, end: date) -> list[date]:
    """Sessions from the checked-in exchange calendar — the holiday is the calendar's, not ours."""
    return trading_calendar().expected_sessions(start, end)


def _market(sessions: list[date]) -> InMemoryMarket:
    bars: dict[tuple[str, date], ReferenceBar] = {}
    for session in sessions:
        bars[(INFY, session)] = _bar(INFY, session, open_="1000", vwap="1000")
        bars[(TCS, session)] = _bar(TCS, session, open_="2000", vwap="2000")
    return InMemoryMarket(sessions, bars)


def _schedule_dict(lags: dict[str, int], *, provenance: str = "verified") -> dict[str, object]:
    return {
        "version": 1,
        "schedule": {"market": "test", "scope": "test"},
        "eras": [
            {
                "id": f"era-{when}",
                "effective_from": when,
                "lag_sessions": lag,
                "label": "test",
                "provenance": provenance,
                "sources_read_on": "2026-09-28",
                "sources": ["https://example.invalid/test"],
                "notes": "test",
            }
            for when, lag in lags.items()
        ],
    }


def _schedule(lags: dict[str, int]) -> SettlementSchedule:
    return SettlementSchedule.model_validate(_schedule_dict(lags))


#: The real eras with their cycles swapped: T+1 in 2019, T+2 in 2024.
INVERTED: Final[dict[str, int]] = {"2003-04-01": 1, "2023-01-27": 2}
#: The pre-fix behaviour: T+1 across all history.
IGNORED: Final[dict[str, int]] = {"2003-04-01": 1}


def _run(
    broker: SimBroker, clock: FrozenClock, decide_on: date, request: OrderRequest, fill_on: date
) -> Order:
    """Place `request` on the evening of `decide_on` and run the session it fills in."""
    clock.freeze_at(decide_on)
    placed = broker.place(request)
    assert placed.target_session == fill_on
    (resolved,) = [o for o in broker.execute_session(fill_on) if o.order_id == placed.order_id]
    return resolved


def _holi_scenario(settlement: SettlementSchedule | None) -> tuple[SimBroker, dict[str, Order]]:
    """Hold 100 INFY, sell it on 2019-03-20, then try to buy TCS with the proceeds each evening.

    Opening cash leaves ~₹10k after the INFY buy, so the ~₹1 lakh TCS buy can only be paid for
    with the INFY sale proceeds. Returns the broker and the TCS attempts by fill session.
    """
    sessions = _sessions(date(2019, 3, 1), date(2019, 4, 30))
    market = _market(sessions)
    clock = FrozenClock(date(2019, 3, 12))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_STATE),
        market=market,
        opening_cash=Decimal("110000"),
        settlement=settlement,
    )
    bought = _run(
        broker,
        clock,
        date(2019, 3, 12),
        OrderRequest(isin=INFY, side=Side.BUY, quantity=100),
        date(2019, 3, 13),
    )
    assert bought.status is OrderStatus.COMPLETE
    for session in _sessions(date(2019, 3, 14), date(2019, 3, 19)):
        broker.execute_session(session)
    sold = _run(
        broker,
        clock,
        date(2019, 3, 19),
        OrderRequest(isin=INFY, side=Side.SELL, quantity=100),
        SELL_2019,
    )
    assert sold.status is OrderStatus.COMPLETE

    attempts: dict[str, Order] = {}
    for decide_on, fill_on in ((SELL_2019, T1_2019), (T1_2019, T2_2019)):
        order = _run(
            broker, clock, decide_on, OrderRequest(isin=TCS, side=Side.BUY, quantity=50), fill_on
        )
        attempts[fill_on.isoformat()] = order
        if order.status is OrderStatus.COMPLETE:
            break
    return broker, attempts


def test_the_calendar_really_has_holi_2019_between_the_sale_and_its_settlement() -> None:
    assert HOLI_2019 not in _sessions(SELL_2019, T2_2019)
    assert _sessions(SELL_2019, T2_2019) == [SELL_2019, T1_2019, T2_2019]


def test_a_2019_sale_settles_t_plus_2_trading_sessions_later_skipping_holi() -> None:
    """The proceeds cannot fund a buy on 03-22 (T+1); they fund one on 03-25 (T+2, after Holi)."""
    broker, attempts = _holi_scenario(settlement=None)

    early = attempts[T1_2019.isoformat()]
    assert early.status is OrderStatus.REJECTED
    assert early.reason is not None and "insufficient cash" in early.reason

    settled = attempts[T2_2019.isoformat()]
    assert settled.status is OrderStatus.COMPLETE
    assert broker.unsettled_proceeds == Decimal("0")


def test_2019_proceeds_are_not_spendable_until_the_eve_of_their_settlement_session() -> None:
    """`margins().available` excludes the sale until an order decided that evening fills on T+2."""
    sessions = _sessions(date(2019, 3, 1), date(2019, 4, 30))
    clock = FrozenClock(date(2019, 3, 12))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_STATE),
        market=_market(sessions),
        opening_cash=Decimal("110000"),
    )
    _run(
        broker,
        clock,
        date(2019, 3, 12),
        OrderRequest(isin=INFY, side=Side.BUY, quantity=100),
        date(2019, 3, 13),
    )
    for session in _sessions(date(2019, 3, 14), date(2019, 3, 19)):
        broker.execute_session(session)
    cash_before = broker.margins().available
    sold = _run(
        broker,
        clock,
        date(2019, 3, 19),
        OrderRequest(isin=INFY, side=Side.SELL, quantity=100),
        SELL_2019,
    )
    assert sold.fill is not None
    proceeds = sold.fill.net_cash

    # Evening of the sale (T): the next fill is T+1 — proceeds are not paid out yet.
    assert broker.margins().available == cash_before
    assert broker.unsettled_proceeds == proceeds
    assert broker.ledger()[-1].balance == cash_before + proceeds  # the account is owed it

    # Evening of T+1 (03-22): the next fill is 03-25 = T+2, the settlement session.
    broker.execute_session(T1_2019)
    assert broker.margins().available == cash_before + proceeds
    assert broker.unsettled_proceeds == Decimal("0")


def test_2019_bought_shares_are_not_deliverable_until_t_plus_2() -> None:
    """A buy filled 2019-03-20 cannot be sold on 03-22 (T+1); it can on 03-25 (T+2)."""
    sessions = _sessions(date(2019, 3, 1), date(2019, 4, 30))
    clock = FrozenClock(date(2019, 3, 19))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_STATE),
        market=_market(sessions),
        opening_cash=Decimal("1000000"),
    )
    _run(
        broker,
        clock,
        date(2019, 3, 19),
        OrderRequest(isin=INFY, side=Side.BUY, quantity=10),
        SELL_2019,
    )
    early = _run(
        broker, clock, SELL_2019, OrderRequest(isin=INFY, side=Side.SELL, quantity=10), T1_2019
    )
    assert early.status is OrderStatus.REJECTED
    assert [p.session for p in broker.positions()] == [SELL_2019]
    assert broker.holdings() == ()

    settled = _run(
        broker, clock, T1_2019, OrderRequest(isin=INFY, side=Side.SELL, quantity=10), T2_2019
    )
    assert settled.status is OrderStatus.COMPLETE


@pytest.mark.parametrize("lags", [INVERTED, IGNORED], ids=["inverted", "ignored"])
def test_the_2019_scenario_fails_if_the_schedule_is_inverted_or_ignored(
    lags: dict[str, int],
) -> None:
    """With T+1 in 2019 the proceeds would fund the 03-22 buy — the liquidity the fix removes."""
    _, attempts = _holi_scenario(settlement=_schedule(lags))
    assert attempts[T1_2019.isoformat()].status is OrderStatus.COMPLETE


# ── the broker: 2024, T+1 ─────────────────────────────────────────────────────────────────────

SELL_2024: Final[date] = date(2024, 1, 3)
NEXT_2024: Final[date] = date(2024, 1, 4)


def _t1_scenario(settlement: SettlementSchedule | None) -> tuple[SimBroker, Order, Order]:
    """Sell INFY on 2024-01-03; buy TCS with the proceeds in the same session, then the next."""
    sessions = _sessions(date(2023, 12, 1), date(2024, 1, 31))
    clock = FrozenClock(date(2023, 12, 28))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=_market(sessions),
        opening_cash=Decimal("110000"),
        settlement=settlement,
    )
    _run(
        broker,
        clock,
        date(2023, 12, 28),
        OrderRequest(isin=INFY, side=Side.BUY, quantity=100),
        date(2023, 12, 29),
    )
    broker.execute_session(date(2024, 1, 1))
    broker.execute_session(date(2024, 1, 2))

    clock.freeze_at(date(2024, 1, 2))
    broker.place(OrderRequest(isin=INFY, side=Side.SELL, quantity=100))
    same_day = broker.place(OrderRequest(isin=TCS, side=Side.BUY, quantity=50))
    results = {o.order_id: o for o in broker.execute_session(SELL_2024)}
    next_day = _run(
        broker, clock, SELL_2024, OrderRequest(isin=TCS, side=Side.BUY, quantity=50), NEXT_2024
    )
    return broker, results[same_day.order_id], next_day


def test_a_2024_sale_settles_t_plus_1_and_funds_the_next_sessions_buy() -> None:
    _, same_day, next_day = _t1_scenario(settlement=None)
    # T+0: a buy in the sale's own session cannot use its proceeds, even placed after the sell.
    assert same_day.status is OrderStatus.REJECTED
    # T+1: an order decided the evening of the sale fills on the settlement session.
    assert next_day.status is OrderStatus.COMPLETE


def test_the_2024_scenario_fails_if_the_schedule_is_inverted() -> None:
    _, _, next_day = _t1_scenario(settlement=_schedule(INVERTED))
    assert next_day.status is OrderStatus.REJECTED


# ── coverage and determinism ──────────────────────────────────────────────────────────────────


def test_a_fill_the_schedule_does_not_cover_raises_and_leaves_the_book_untouched() -> None:
    sessions = _sessions(date(2019, 3, 1), date(2019, 3, 29))
    clock = FrozenClock(date(2019, 3, 12))
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state=_STATE),
        market=_market(sessions),
        opening_cash=Decimal("1000000"),
        settlement=_schedule({"2020-01-01": 2}),
    )
    broker.place(OrderRequest(isin=INFY, side=Side.BUY, quantity=10))
    with pytest.raises(NoSettlementCycleError, match="2019-03-13"):
        broker.execute_session(date(2019, 3, 13))
    assert broker.cash == Decimal("1000000")
    assert broker.positions() == ()
    assert broker.ledger() == ()


def test_the_settled_book_is_reproducible() -> None:
    first, _ = _holi_scenario(settlement=None)
    second, _ = _holi_scenario(settlement=None)
    assert first.ledger() == second.ledger()
    assert first.holdings() == second.holdings()
    assert first.margins() == second.margins()
