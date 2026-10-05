"""The five other SIP-allocating policies size their buys to A8's per-order ceiling too (X2).

PR #31 found swing composite's buys sized as ``free cash / top_n`` with no regard for the ₹1.2L
``MAX_ORDER_VALUE`` cap: once the book outgrew ~₹24L, A8 refused every buy, the refused cash stayed
idle, and the next rebalance spread the larger idle balance over the same names until the book was
all cash. Naive momentum, momentum v2, sector rotation, fundamentals value and forecast daily share
that buy path (``simulate_sip_instalment``). Each is replayed here on the real stack
(policy -> ``ReplayEngine`` -> ``RailGate`` -> ``SimBroker``) on a book whose equal-weight buy is
above the cap, and each test fails if the policy stops sizing to the ceiling:

* with the rails in force as ``order_caps``: no ``MAX_ORDER_VALUE`` block, every buy within the
  cap, and the book ends invested rather than in cash;
* without them: A8 still refuses every over-cap buy — the cap was not loosened, and a buy is never
  split to get under it;
* every driver hands each policy the very rails its gate enforces, and the five runners' specs
  record the sizing so a sized run never resumes one persisted before it.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.cases import RiskRails
from analyst.journal.models import Decision
from analyst.rails import RailId
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.forecast_daily import (
    ForecastDailyParameters,
    ForecastDailyPolicy,
    ForecastRecord,
)
from backtest.policies.fundamentals_value import (
    FundamentalsRecord,
    FundamentalsValueParameters,
    FundamentalsValuePolicy,
)
from backtest.policies.momentum_v2 import MomentumV2Parameters, MomentumV2Policy, MomentumV2Record
from backtest.policies.naive_momentum import MomentumParameters, MomentumRecord, NaiveMomentumPolicy
from backtest.policies.sector_rotation import (
    SectorRotationParameters,
    SectorRotationPolicy,
    SectorRotationRecord,
)
from backtest.policies.sizing import BUY_SIZING_IDENTITY
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import Policy, ReplayEngine, SessionContext
from backtest.run import _AccountingBroker, backtest_spec
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext
from execution.broker import Exchange, Holding, Margins, Position, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import CAP, NAMES, PRICE, RAILS, _Market

SESSIONS = tuple(date(2024, 1, day) for day in (1, 2, 3, 4, 5, 8, 9, 10))
#: ₹30L over ten names is ₹2.94L a name at equal weight — every buy above the ₹1.2L cap.
OPENING = Decimal("3000000")
#: Forecast daily never tops up a held name, so its book is at most ``top_n`` ceilings: ₹13L over
#: ten names is ₹1.27L a slot, above the cap, and ten ceilings still invest most of it.
FORECAST_OPENING = Decimal("1300000")
_KNOWN = SESSIONS[0]


# ── in-memory data sources, one per policy ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Rebalancing[R]:
    """A fixed candidate set every session, and every session a rebalance."""

    records: tuple[R, ...]

    def is_rebalance(self, session: date) -> bool:
        return True

    def signal(self, as_of: date) -> Dataset[R]:
        return Dataset.declaring(
            f"candidates@{as_of.isoformat()}",
            self.records,
            knowable_date=lambda record: record.knowable_date,  # type: ignore[attr-defined]
        )


@dataclass(frozen=True, slots=True)
class _Forecasts:
    records: tuple[ForecastRecord, ...]

    def signal(self, as_of: date) -> Dataset[ForecastRecord]:
        return Dataset.declaring(
            f"forecast@{as_of.isoformat()}",
            self.records,
            knowable_date=lambda record: record.knowable_date,
        )

    def marks(self, as_of: date) -> Mapping[str, Decimal]:
        return dict.fromkeys(NAMES, PRICE)


def _naive(caps: RiskRails | None) -> Policy:
    records = tuple(
        MomentumRecord(isin=isin, momentum=Decimal("0.5"), price=PRICE, knowable_date=_KNOWN)
        for isin in NAMES
    )
    return NaiveMomentumPolicy(_Rebalancing(records), MomentumParameters(top_n=10), order_caps=caps)


def _v2(caps: RiskRails | None) -> Policy:
    records = tuple(
        MomentumV2Record(
            isin=isin,
            momentum_0_12=Decimal("0.5"),
            momentum_12_1=Decimal("0.5"),
            price=PRICE,
            volatility=Decimal("0.2"),
            knowable_date=_KNOWN,
        )
        for isin in NAMES
    )
    return MomentumV2Policy(
        _Rebalancing(records),  # type: ignore[arg-type]  # no regime: the filter is off
        MomentumV2Parameters(top_n=10),
        order_caps=caps,
    )


def _sector(caps: RiskRails | None) -> Policy:
    records = tuple(
        SectorRotationRecord(
            isin=isin, momentum=Decimal("0.5"), price=PRICE, sector="IT", knowable_date=_KNOWN
        )
        for isin in NAMES
    )
    return SectorRotationPolicy(
        _Rebalancing(records), SectorRotationParameters(top_k=1, top_n=10), order_caps=caps
    )


def _fundamentals(caps: RiskRails | None) -> Policy:
    records = tuple(
        FundamentalsRecord(
            isin=isin,
            earnings_yield=Decimal("0.08"),
            earnings_growth=None,
            roe=None,
            price=PRICE,
            knowable_date=_KNOWN,
        )
        for isin in NAMES
    )
    return FundamentalsValuePolicy(
        _Rebalancing(records), FundamentalsValueParameters(top_n=10), order_caps=caps
    )


def _forecast(caps: RiskRails | None) -> Policy:
    records = tuple(
        ForecastRecord(
            isin=isin, expected_return=Decimal("0.05"), price=PRICE, knowable_date=_KNOWN
        )
        for isin in NAMES
    )
    # Every slot may fill in one session: the policy reads only *settled* holdings, so on the next
    # session it picks its still-pending names again (MAX_POSITION refuses those), and at two
    # trades a session that would leave slots unfilled for reasons that are not the ceiling's.
    return ForecastDailyPolicy(
        _Forecasts(records),
        ForecastDailyParameters(top_n=10, max_trades_per_session=10),
        order_caps=caps,
    )


#: (policy builder, opening cash, least share of the opening the book must end invested in)
STRATEGIES: dict[str, tuple[Callable[[RiskRails | None], Policy], Decimal, Decimal]] = {
    "naive_momentum": (_naive, OPENING, Decimal("0.9")),
    "momentum_v2": (_v2, OPENING, Decimal("0.9")),
    "sector_rotation": (_sector, OPENING, Decimal("0.9")),
    "fundamentals_value": (_fundamentals, OPENING, Decimal("0.9")),
    "forecast_daily": (_forecast, FORECAST_OPENING, Decimal("0.9")),
}


# ── the real stack ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Outcome:
    broker: _AccountingBroker
    order_value_blocks: int

    def held(self) -> dict[str, int]:
        held: dict[str, int] = {}
        lots: list[Holding | Position] = [*self.broker.holdings(), *self.broker.positions()]
        for lot in lots:
            held[lot.isin] = held.get(lot.isin, 0) + lot.quantity
        return held

    def invested(self) -> Decimal:
        return sum((quantity * PRICE for quantity in self.held().values()), Decimal(0))

    def buys(self) -> list[Decimal]:
        return [
            fill.reference_price * fill.quantity
            for fill in self.broker.fills
            if fill.side is Side.BUY
        ]


def _replay(name: str, *, order_caps: bool) -> _Outcome:
    build, opening, _ = STRATEGIES[name]
    clock = FrozenClock(SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(SESSIONS),
        opening_cash=opening,
    )
    book = PortfolioBook()
    book.deposit(SESSIONS[0], opening)
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
    prices = {(isin, day): PRICE for isin in NAMES for day in SESSIONS}
    result = ReplayEngine(
        policy=build(rail_policy.rails if order_caps else None),
        broker=broker,
        clock=clock,
        sessions=SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()
    blocks = [
        entry
        for entry in result.journal
        if entry.decision is Decision.RAIL_BLOCK
        and RailId.MAX_ORDER_VALUE.value in entry.payload["rails"]
    ]
    return _Outcome(broker=broker, order_value_blocks=len(blocks))


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_buys_sized_to_the_ceiling_deploy_a_large_book_with_no_order_value_block(name: str) -> None:
    _, opening, invested_share = STRATEGIES[name]
    outcome = _replay(name, order_caps=True)

    assert outcome.order_value_blocks == 0
    buys = outcome.buys()
    assert buys and all(value <= CAP for value in buys)
    # Every name of the target is held, and the book ends invested rather than parked in cash.
    assert set(outcome.held()) == set(NAMES)
    assert outcome.invested() >= opening * invested_share


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_without_the_ceiling_a8_refuses_every_buy_and_the_book_stays_in_cash(name: str) -> None:
    """The failure the ceiling exists for, and proof the cap itself was not loosened."""
    outcome = _replay(name, order_caps=False)

    assert outcome.order_value_blocks > 0
    assert outcome.buys() == []
    assert outcome.invested() == 0


# ── momentum v2's second buy path: the session-after redeploy ────────────────────────────────────


class _FirstSessionRebalance(_Rebalancing[MomentumV2Record]):
    def is_rebalance(self, session: date) -> bool:
        return session == SESSIONS[0]


class _Book:
    """A minimal broker read surface: fixed settled holdings and free cash. Records nothing."""

    def __init__(self, cash: Decimal, holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = cash
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[Position, ...]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(session: date, book: _Book) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=book,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


@pytest.mark.parametrize("order_caps", [True, False])
def test_momentum_v2_redeploys_freed_cash_in_orders_within_the_ceiling(order_caps: bool) -> None:
    """A rebalance sells a dropout; the next session redeploys ₹30L into ten names."""
    records = tuple(
        MomentumV2Record(
            isin=isin,
            momentum_0_12=Decimal("0.5"),
            momentum_12_1=Decimal("0.5"),
            price=PRICE,
            volatility=Decimal("0.2"),
            knowable_date=_KNOWN,
        )
        for isin in NAMES
    )
    policy = MomentumV2Policy(
        _FirstSessionRebalance(records),  # type: ignore[arg-type]  # no regime: the filter is off
        MomentumV2Parameters(top_n=10, redeploy_next_session=True),
        order_caps=RAILS if order_caps else None,
    )
    dropout = Holding(
        isin="INE999A01010", exchange=Exchange.NSE, quantity=30000, average_price=PRICE
    )
    policy.decide(_ctx(SESSIONS[0], _Book(Decimal("50"), (dropout,))))
    redeploy = policy.decide(_ctx(SESSIONS[1], _Book(OPENING)))

    buys = [order for order in redeploy.orders if order.side is Side.BUY]
    assert {order.isin for order in buys} == set(NAMES)
    largest = max(order.quantity * PRICE for order in buys)
    if order_caps:
        assert largest <= CAP
    else:
        assert largest > CAP  # the failure the ceiling exists for: A8 would refuse every one


def test_the_ratified_rupee_cap_is_unchanged() -> None:
    assert RAILS.max_order_value_inr == CAP


# ── the drivers and the specs ────────────────────────────────────────────────────────────────────

_FIVE = (
    "NaiveMomentumPolicy",
    "MomentumV2Policy",
    "SectorRotationPolicy",
    "FundamentalsValuePolicy",
    "ForecastDailyPolicy",
)


def test_every_driver_sizes_the_five_policies_to_the_rails_the_gate_enforces() -> None:
    """Every construction of the five in the backtest drivers is handed ``order_caps``."""
    root = Path(__file__).resolve().parents[2] / "backtest"
    seen: set[str] = set()
    for module in ("run.py", "forecast_run.py"):
        tree = ast.parse((root / module).read_text("utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _FIVE
            ):
                seen.add(node.func.id)
                keywords = {keyword.arg for keyword in node.keywords}
                assert "order_caps" in keywords, f"{module}:{node.lineno} {node.func.id}"
    assert seen == set(_FIVE)


@pytest.mark.parametrize("runner", ["naive_momentum", "momentum_v2", "forecast_daily"])
def test_a_ceiling_sized_runner_records_the_sizing_in_its_spec(runner: str) -> None:
    spec = backtest_spec(
        runner,
        start=SESSIONS[0],
        end=SESSIONS[-1],
        parameters=MomentumParameters(),
        opening_cash=OPENING,
        adjusted=True,
        universe=None,
    )
    assert spec["buy_sizing"] == BUY_SIZING_IDENTITY


def test_the_swing_composite_spec_is_unchanged_by_the_sizing_key() -> None:
    spec = backtest_spec(
        "swing_composite",
        start=SESSIONS[0],
        end=SESSIONS[-1],
        parameters=MomentumParameters(),
        opening_cash=OPENING,
        adjusted=True,
        universe=None,
    )
    assert "buy_sizing" not in spec
