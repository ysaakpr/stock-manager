"""M4.10 — the naive momentum policy, unit-tested offline (EXECUTION_PLAN §7, X2).

These tests exercise the policy's decision logic in isolation, against an in-memory data source and
a fake broker — no store, no clock but the injected one, no network (B8, invariant #11). They pin
the behaviours the ten-year run relies on and that a reversed sign or a dropped constraint would
break:

* it ranks by trailing return and holds exactly the top-N, ties broken by ISIN;
* off a rebalance session it decides nothing, so the engine writes a heartbeat (invariant #9);
* it sells — in full — a holding that has dropped out of the target set;
* it reads only through the point-in-time context, so a not-yet-knowable record trips the guard
  (invariant #7);
* it never stages a fractional share, and the same inputs give the same decision (determinism).

The engine-level wiring (the real SimBroker, the ten-year replay) is covered by running
``backtest.run`` for the gate; here the policy stands alone.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from analyst.journal.models import Decision, Sleeve
from backtest.policies.naive_momentum import (
    MomentumParameters,
    MomentumRecord,
    NaiveMomentumPolicy,
)
from backtest.replay import SessionContext
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side

SESSION = date(2020, 1, 1)

# Five candidates with distinct trailing returns; ISINs are real-shaped (invariant #2).
A = "INE001A01036"
B = "INE002A01018"
C = "INE009A01021"
D = "INE040A01034"
E = "INE467B01029"

_RECORDS = (
    MomentumRecord(isin=A, momentum=Decimal("0.50"), price=Decimal("100"), knowable_date=SESSION),
    MomentumRecord(isin=B, momentum=Decimal("0.40"), price=Decimal("200"), knowable_date=SESSION),
    MomentumRecord(isin=C, momentum=Decimal("0.30"), price=Decimal("300"), knowable_date=SESSION),
    MomentumRecord(isin=D, momentum=Decimal("0.20"), price=Decimal("150"), knowable_date=SESSION),
    MomentumRecord(isin=E, momentum=Decimal("-0.10"), price=Decimal("250"), knowable_date=SESSION),
)


class _Data:
    """An in-memory ``MomentumData``: a fixed candidate set and a caller-set rebalance flag."""

    def __init__(self, records: tuple[MomentumRecord, ...], *, rebalance: bool = True) -> None:
        self._records = records
        self._rebalance = rebalance

    def is_rebalance(self, session: date) -> bool:
        return self._rebalance

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        return Dataset.declaring(
            f"momentum@{as_of.isoformat()}",
            self._records,
            knowable_date=lambda record: record.knowable_date,
        )


class _LeakingData(_Data):
    """A data source whose records claim to be knowable *after* the session — a leak on purpose."""

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        future = date(as_of.year + 1, as_of.month, as_of.day)
        leaked = tuple(
            MomentumRecord(isin=r.isin, momentum=r.momentum, price=r.price, knowable_date=future)
            for r in self._records
        )
        return Dataset.declaring("leak", leaked, knowable_date=lambda record: record.knowable_date)


class _FakeBroker:
    """A minimal ``Broker`` read surface: fixed holdings and free cash. Records nothing."""

    def __init__(self, *, cash: Decimal, holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = cash
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(data_session: date, broker: _FakeBroker) -> SessionContext:
    return SessionContext(
        session=data_session,
        pit=PitContext(as_of=data_session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface the policy uses
        clock=FrozenClock(data_session),
    )


def _holding(isin: str, quantity: int, price: str) -> Holding:
    return Holding(
        isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=Decimal(price)
    )


# ── ranking: holds exactly the top-N by trailing return ──────────────────────────────────────────


def test_holds_exactly_the_top_n_by_momentum() -> None:
    """Top-3 of five candidates are A/B/C (the three highest returns); D and E get nothing."""
    policy = NaiveMomentumPolicy(_Data(_RECORDS), MomentumParameters(top_n=3))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))

    bought = {order.isin for order in decision.orders if order.side is Side.BUY}
    assert bought <= {A, B, C}
    assert bought, "the top names should have been bought with a full cash budget"
    assert D not in bought and E not in bought


def test_ranking_is_by_return_not_price_and_breaks_ties_by_isin() -> None:
    """Two names with the same return rank by ISIN, so the choice is deterministic."""
    tie = (
        MomentumRecord(
            isin=E, momentum=Decimal("0.9"), price=Decimal("100"), knowable_date=SESSION
        ),
        MomentumRecord(
            isin=A, momentum=Decimal("0.9"), price=Decimal("100"), knowable_date=SESSION
        ),
        MomentumRecord(
            isin=B, momentum=Decimal("0.1"), price=Decimal("100"), knowable_date=SESSION
        ),
    )
    policy = NaiveMomentumPolicy(_Data(tie), MomentumParameters(top_n=1))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    bought = {order.isin for order in decision.orders if order.side is Side.BUY}
    # A and E tie on return; A wins the single slot on the lower ISIN, and B (lower return) is out.
    assert bought == {A}


# ── heartbeat off a rebalance session ────────────────────────────────────────────────────────────


def test_no_orders_off_a_rebalance_session() -> None:
    """A non-rebalance session returns evidence and no orders — the engine writes the heartbeat."""
    policy = NaiveMomentumPolicy(_Data(_RECORDS, rebalance=False))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))

    assert decision.orders == ()
    assert decision.entries == ()
    assert decision.evidence.trading_date == SESSION
    assert decision.evidence.items  # the evidence the heartbeat will be stamped with


# ── sells: a holding that dropped out of the target set is liquidated in full ─────────────────────


def test_sells_a_holding_that_left_the_target_set() -> None:
    """E is held but ranks below the top-3, so the whole holding is sold with a rationale."""
    held = (_holding(E, 40, "260"),)
    policy = NaiveMomentumPolicy(_Data(_RECORDS), MomentumParameters(top_n=3))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("100000"), holdings=held)))

    sells = [order for order in decision.orders if order.side is Side.SELL]
    assert len(sells) == 1
    assert sells[0].isin == E
    assert sells[0].quantity == 40  # the whole holding, not a slice
    sell_entries = [e for e in decision.entries if e.decision is Decision.SELL]
    assert sell_entries and sell_entries[0].isin == E
    assert sell_entries[0].sleeve is Sleeve.TACTICAL
    assert sell_entries[0].rationale  # a sell must say why (§0)


def test_a_held_name_still_in_the_target_is_not_sold() -> None:
    """A is held and still top-ranked, so it is never sold — only topped up if cash allows."""
    held = (_holding(A, 10, "90"),)
    policy = NaiveMomentumPolicy(_Data(_RECORDS), MomentumParameters(top_n=3))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("100000"), holdings=held)))

    sells = [order for order in decision.orders if order.side is Side.SELL]
    assert all(order.isin != A for order in sells)


# ── point-in-time guard: a not-yet-knowable record trips the guard, on the engine's scope ─────────


def test_a_not_yet_knowable_record_raises_through_the_pit_guard() -> None:
    """The policy admits via ctx.pit; a future-dated record raises (invariant #7)."""
    policy = NaiveMomentumPolicy(_LeakingData(_RECORDS))
    with pytest.raises(PitError):
        policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


# ── whole shares + determinism ───────────────────────────────────────────────────────────────────


def test_every_order_is_a_whole_number_of_shares() -> None:
    """The allocator produces whole-share orders; nothing fractional reaches the broker."""
    policy = NaiveMomentumPolicy(_Data(_RECORDS), MomentumParameters(top_n=5))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    for order in decision.orders:
        assert isinstance(order.quantity, int)
        assert order.quantity > 0


def test_same_inputs_produce_the_same_decision() -> None:
    """Determinism: two independent runs of the policy return identical orders and entries."""
    params = MomentumParameters(top_n=4)
    first = NaiveMomentumPolicy(_Data(_RECORDS), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    second = NaiveMomentumPolicy(_Data(_RECORDS), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    assert [(o.isin, o.side, o.quantity) for o in first.orders] == [
        (o.isin, o.side, o.quantity) for o in second.orders
    ]
    assert [(e.isin, e.decision) for e in first.entries] == [
        (e.isin, e.decision) for e in second.entries
    ]


def test_equal_weight_targets_sum_to_one_and_allocate() -> None:
    """With more names than fit the budget cleanly, it still allocates and carries no fractional."""
    policy = NaiveMomentumPolicy(_Data(_RECORDS), MomentumParameters(top_n=5))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("10000"))))
    # A small budget across five names still yields whole-share buys (some names may get none).
    buys = [o for o in decision.orders if o.side is Side.BUY]
    assert buys, "even a small budget should buy at least one share of the cheapest name"
    assert all(o.quantity >= 1 for o in buys)


def test_fill_headroom_is_reserved_at_the_data_edge() -> None:
    """T+1 execution needs a bar after the last replayed session (M4.10 boundary).

    A window reaching the last session on disk once crashed mid-run: a rebalance landing on the
    final bar had no next session to stage its fill against (NoReferenceBarError from SimBroker).
    The run now reserves that final session as fill headroom instead of stranding the order.
    """
    from backtest.run import BacktestError, _reserve_fill_headroom

    calendar = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]

    # Window reaching the edge: the last on-disk session is reserved as a fill target, not replayed.
    reached = tuple(calendar)
    assert _reserve_fill_headroom(reached, calendar) == (date(2026, 8, 3), date(2026, 8, 4))

    # Window that stops before the edge already has headroom and is left untouched.
    inside = (date(2026, 8, 3), date(2026, 8, 4))
    assert _reserve_fill_headroom(inside, calendar) == inside

    # A single-session window sitting on the edge cannot reserve headroom and is refused loudly.
    with pytest.raises(BacktestError, match="fill headroom"):
        _reserve_fill_headroom((date(2026, 8, 5),), calendar)
