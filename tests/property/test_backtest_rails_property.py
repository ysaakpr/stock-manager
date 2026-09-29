"""X2 x A8 property: no generated order stream through a backtest breaches a cap (§8.4).

``tests/property/test_rails_property.py`` proves the pure checks hold; this proves the *replay*
holds them — that no order a policy returns can reach the book except through A8. Hypothesis draws
an arbitrary multi-session stream of buys and sells over a twelve-name universe (three labelled
sectors and a pool of unlabelled names), a scripted policy returns it through ``ReplayEngine``, and
a flat, cost-free broker fills every order the engine places. After the run the book and the placed
stream are checked against every order rail of the ratified policy:

* no holding above ``max_position_pct`` and no sector (``UNKNOWN`` included) above
  ``max_sector_pct`` of case value, after any session;
* no placed order above ``max_order_value_inr`` or ``max_order_pct_of_case``;
* once the book has held ``min_holdings`` names, it never holds fewer.

The broker fills at the marks the rails value at and charges nothing, so the book it ends on is the
book A8 projected — a breach can only mean an order went round the rails.
``test_the_property_has_teeth`` runs the same checker over a gate that lets everything through and
asserts it fails: the property is not vacuous.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from analyst.cases import RiskRails
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor
from backtest.rails import (
    UNKNOWN_SECTOR,
    BacktestRailPolicy,
    GateOutcome,
    RailGate,
    SectorMap,
    ratified_backtest_rail_policy,
)
from backtest.replay import ReplayEngine, SessionContext, SessionDecision
from dataplatform.clock import Clock, FrozenClock
from execution.broker import (
    Broker,
    Exchange,
    Holding,
    LedgerEntry,
    Margins,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
)

_START = date(2024, 1, 1)
_SESSIONS = tuple(_START + timedelta(days=offset) for offset in range(8))
_CASH = Decimal("1000000")

#: (isin, sector, price). Sector None = unlabelled, pooled as UNKNOWN by the gate.
_UNIVERSE: tuple[tuple[str, str | None, Decimal], ...] = (
    ("INE101A01011", "IT", Decimal("100")),
    ("INE102A01019", "IT", Decimal("250")),
    ("INE103A01017", "IT", Decimal("500")),
    ("INE104A01015", "PHARMA", Decimal("120")),
    ("INE105A01012", "PHARMA", Decimal("300")),
    ("INE106A01010", "PHARMA", Decimal("75")),
    ("INE107A01018", "ENERGY", Decimal("200")),
    ("INE108A01016", "ENERGY", Decimal("90")),
    ("INE109A01014", "ENERGY", Decimal("60")),
    ("INE110A01012", None, Decimal("150")),
    ("INE111A01010", None, Decimal("40")),
    ("INE112A01018", None, Decimal("220")),
)
_PRICES = {isin: price for isin, _, price in _UNIVERSE}
_SECTORS = {isin: sector for isin, sector, _ in _UNIVERSE if sector is not None}


def _policy() -> BacktestRailPolicy:
    """The ratified caps, over this universe's sector map."""
    return BacktestRailPolicy(
        policy_id="property-ratified-caps",
        version=1,
        rails=ratified_backtest_rail_policy().rails,
        sectors=SectorMap(source="property", sha256="property", by_isin=_SECTORS),
        provenance="property test",
    )


def _marks(session: date) -> dict[str, Decimal]:
    return dict(_PRICES)


# ── the broker: fills what was placed, at the mark, for free ────────────────────────────────────


@dataclass
class _FlatBroker:
    cash: Decimal
    held: dict[str, int] = field(default_factory=dict)
    placed: list[tuple[Decimal, OrderRequest]] = field(default_factory=list)
    books: list[dict[str, int]] = field(default_factory=list)
    _staged: list[OrderRequest] = field(default_factory=list)

    def case_value(self) -> Decimal:
        return self.cash + sum((_PRICES[i] * q for i, q in self.held.items()), Decimal(0))

    def execute_session(self, session: date) -> tuple[Order, ...]:
        for request in self._staged:
            value = _PRICES[request.isin] * request.quantity
            if request.side is Side.BUY:
                if value > self.cash:
                    continue  # the broker's own refusal — never reached through a railed gate
                self.cash -= value
                self.held[request.isin] = self.held.get(request.isin, 0) + request.quantity
            else:
                have = self.held.get(request.isin, 0)
                if request.quantity > have:
                    continue
                self.cash += value
                self.held[request.isin] = have - request.quantity
                if self.held[request.isin] == 0:
                    del self.held[request.isin]
        self._staged = []
        self.books.append(dict(self.held))
        return ()

    def session_valid(self) -> bool:
        return True

    def place(self, request: OrderRequest) -> Order:
        self.placed.append((self.case_value(), request))
        self._staged.append(request)
        return Order(
            order_id=f"P-{len(self.placed):05d}",
            request=request,
            status=OrderStatus.STAGED,
            decision_date=_START,
            target_session=_START,
        )

    def modify(self, order_id: str, *, quantity: int) -> Order:
        raise NotImplementedError

    def cancel(self, order_id: str) -> Order:
        raise NotImplementedError

    def positions(self) -> tuple[Position, ...]:
        return ()

    def holdings(self) -> tuple[Holding, ...]:
        return tuple(
            Holding(isin=i, exchange=Exchange.NSE, quantity=q, average_price=_PRICES[i])
            for i, q in sorted(self.held.items())
        )

    def ledger(self) -> tuple[LedgerEntry, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=self.cash, utilised=Decimal(0))


# ── the policy: replays the drawn stream, sizing sells to what is held ──────────────────────────

#: One drawn intent: (universe index, is_buy, quantity or fraction-of-holding in percent).
_Intent = tuple[int, bool, int]


class _StreamPolicy:
    def __init__(self, stream: Sequence[Sequence[_Intent]]) -> None:
        self._stream = stream

    def decide(self, ctx: SessionContext) -> SessionDecision:
        index = _SESSIONS.index(ctx.session)
        held = {h.isin: h.quantity for h in ctx.broker.holdings()}
        orders: list[OrderRequest] = []
        seen: set[str] = set()
        for name, is_buy, amount in self._stream[index] if index < len(self._stream) else ():
            isin = _UNIVERSE[name][0]
            if isin in seen:
                continue  # one order per scrip per session, as the EOD model nets
            if is_buy:
                quantity = amount
            else:
                quantity = held.get(isin, 0) * amount // 100 or held.get(isin, 0)
                if quantity == 0:
                    continue
            seen.add(isin)
            side = Side.BUY if is_buy else Side.SELL
            orders.append(
                OrderRequest(isin=isin, side=side, quantity=quantity, exchange=Exchange.NSE)
            )
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(EvidenceItem(kind=EvidenceKind.PRICE, source="p", label="c", value=Decimal(1)),),
        )
        return SessionDecision(evidence=evidence, orders=tuple(orders))


# ── the checker ────────────────────────────────────────────────────────────────────────────────


def _assert_no_cap_breached(broker: _FlatBroker, rails: RiskRails) -> None:
    hundred = Decimal(100)
    for value, request in broker.placed:
        order_value = _PRICES[request.isin] * request.quantity
        assert order_value <= rails.max_order_value_inr, f"order value {order_value} > cap"
        assert order_value * hundred / value <= rails.max_order_pct_of_case, "order % over cap"
    # Case value is invariant under trades at flat marks with no costs.
    total = _CASH
    reached_floor = False
    for held in broker.books:
        by_sector: dict[str, Decimal] = {}
        for isin, quantity in held.items():
            position = _PRICES[isin] * quantity
            assert position * hundred / total <= rails.max_position_pct, f"{isin} over cap"
            sector = _SECTORS.get(isin, UNKNOWN_SECTOR)
            by_sector[sector] = by_sector.get(sector, Decimal(0)) + position
        for sector, exposure in by_sector.items():
            assert exposure * hundred / total <= rails.max_sector_pct, f"{sector} over cap"
        if len(held) >= rails.min_holdings:
            reached_floor = True
        elif reached_floor:
            raise AssertionError(f"book fell to {len(held)} names below the floor it had reached")


def _run(stream: Sequence[Sequence[_Intent]], gate: RailGate) -> _FlatBroker:
    broker = _FlatBroker(cash=_CASH)
    ReplayEngine(
        policy=_StreamPolicy(stream),
        broker=broker,
        clock=FrozenClock(_SESSIONS[0]),
        sessions=_SESSIONS,
        rails=gate,
    ).run()
    return broker


_intent = st.tuples(
    st.integers(min_value=0, max_value=len(_UNIVERSE) - 1),
    st.booleans(),
    st.integers(min_value=1, max_value=3000),
)
_stream = st.lists(st.lists(_intent, max_size=14), min_size=1, max_size=len(_SESSIONS))


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(stream=_stream)
def test_no_generated_order_stream_through_a_backtest_breaches_a_cap(
    stream: list[list[_Intent]],
) -> None:
    policy = _policy()
    broker = _run(stream, RailGate(policy, _marks))
    _assert_no_cap_breached(broker, policy.rails)


class _NoRails(RailGate):
    """What an unrailed engine was: every order straight to the broker. For the teeth test only."""

    def clear(
        self,
        session: date,
        orders: Sequence[OrderRequest],
        *,
        broker: Broker,
        clock: Clock,
        case_id: str | None,
        sleeves: Any,
    ) -> GateOutcome:
        return GateOutcome(allowed=tuple(orders), entries=())


def test_the_property_has_teeth() -> None:
    """The same checker over a bypassed gate fails: a stream that breaches does exist."""
    # 40% of the book into one IT name, then ten names built and all sold in one session.
    concentrated: list[list[_Intent]] = [[(0, True, 4000)]]
    with pytest.raises(AssertionError):
        _assert_no_cap_breached(_run(concentrated, _NoRails(_policy(), _marks)), _policy().rails)
    # And the railed run of the same stream holds.
    _assert_no_cap_breached(_run(concentrated, RailGate(_policy(), _marks)), _policy().rails)

    build = [(i, True, int(Decimal(80000) / _PRICES[_UNIVERSE[i][0]])) for i in range(10)]
    dump = [(i, False, 100) for i in range(10)]
    basket: list[list[_Intent]] = [build, [], dump]
    with pytest.raises(AssertionError, match="floor"):
        _assert_no_cap_breached(_run(basket, _NoRails(_policy(), _marks)), _policy().rails)
    _assert_no_cap_breached(_run(basket, RailGate(_policy(), _marks)), _policy().rails)
