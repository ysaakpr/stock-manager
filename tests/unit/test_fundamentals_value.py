"""M10.6 — the fundamentals-signal policy, unit-tested offline against an in-memory source.

Acceptance, each pinned with a case that fails if the logic is inverted:

1. **The signal reads fundamentals point-in-time.** A candidate whose newest filing is dated after
   the session trips the guard (invariant #7) — the engine's scope, not the policy's memory; and a
   stale name (newest filing older than the staleness limit) is dropped rather than held on a TTM
   that stopped updating.
2. **VALUE ranks on earnings yield, highest first, and never buys a loss-maker**; GROWTH ranks on
   stated TTM-on-TTM growth and skips names without it; QUALITY_VALUE blends the two ranks and
   needs a stated ROE. Reversing any ordering flips the chosen basket.
3. **The mechanics are the momentum v2 mechanics**: whole-share equal-weight buys, hysteresis via
   ``sell_band``, a heartbeat off a rebalance, determinism, every order tagged FUNDAMENTALS.

No store, no network, no wall clock (B8, invariant #11).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision
from backtest.policies.fundamentals_value import (
    FundamentalsRecord,
    FundamentalsSignal,
    FundamentalsValueParameters,
    FundamentalsValuePolicy,
    rank_candidates,
)
from backtest.replay import SessionContext, SessionDecision
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side

SESSION = date(2021, 6, 1)
A, B, C, D, E = "INE001A01036", "INE002A01018", "INE009A01021", "INE040A01034", "INE467B01029"
_D = Decimal


def _rec(
    isin: str,
    *,
    ey: str,
    growth: str | None = None,
    roe: str | None = None,
    price: str = "100",
    knowable: date | None = None,
    momentum: str | None = None,
) -> FundamentalsRecord:
    return FundamentalsRecord(
        isin=isin,
        earnings_yield=_D(ey),
        earnings_growth=None if growth is None else _D(growth),
        roe=None if roe is None else _D(roe),
        price=_D(price),
        knowable_date=knowable or (SESSION - timedelta(days=30)),
        momentum_12_1=None if momentum is None else _D(momentum),
    )


# Yields A > B > C > D; E is a loss-maker. Growth order is the reverse (D > C > B > A).
_RECORDS = (
    _rec(A, ey="0.10", growth="0.05", roe="0.30"),
    _rec(B, ey="0.08", growth="0.10", roe="0.05"),
    _rec(C, ey="0.06", growth="0.20", roe="0.20"),
    _rec(D, ey="0.04", growth="0.40", roe="0.10"),
    _rec(E, ey="-0.02", growth="1.00", roe="-0.10"),
)


class _Data:
    def __init__(
        self, records: tuple[FundamentalsRecord, ...] = _RECORDS, *, rebalance: bool = True
    ) -> None:
        self._records = records
        self._rebalance = rebalance

    def is_rebalance(self, session: date) -> bool:
        return self._rebalance

    def signal(self, as_of: date) -> Dataset[FundamentalsRecord]:
        return Dataset.declaring(
            f"fundamentals@{as_of}", self._records, knowable_date=lambda r: r.knowable_date
        )


class _Broker:
    def __init__(self, cash: str = "100000", holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = _D(cash)
        self._holdings = holdings

    def session_valid(self) -> bool:
        return True

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[()]:
        return ()

    def ledger(self) -> tuple[()]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=_D("0"))

    def place(self, request: object) -> object:  # pragma: no cover - never called by the policy
        raise NotImplementedError

    def modify(self, order_id: str, *, quantity: int) -> object:  # pragma: no cover
        raise NotImplementedError

    def cancel(self, order_id: str) -> object:  # pragma: no cover
        raise NotImplementedError


def _decide(
    params: FundamentalsValueParameters,
    *,
    data: _Data | None = None,
    broker: _Broker | None = None,
) -> SessionDecision:
    ctx = SessionContext(
        session=SESSION,
        pit=PitContext(as_of=SESSION),
        broker=broker or _Broker(),  # type: ignore[arg-type]
        clock=FrozenClock(SESSION),
    )
    return FundamentalsValuePolicy(data or _Data(), params).decide(ctx)


def _bought(decision: SessionDecision) -> list[str]:
    return sorted(o.isin for o in decision.orders if o.side is Side.BUY)


# ── 1. point-in-time ────────────────────────────────────────────────────────────────────────────


def test_a_not_yet_knowable_record_trips_the_guard() -> None:
    leaking = (*_RECORDS[:4], _rec(E, ey="0.50", knowable=SESSION + timedelta(days=1)))
    with pytest.raises(PitError):
        _decide(FundamentalsValueParameters(top_n=2), data=_Data(leaking))


def test_a_stale_name_is_dropped_not_held_inversion() -> None:
    stale = (
        _rec(A, ey="0.10", knowable=SESSION - timedelta(days=201)),
        _rec(B, ey="0.08"),
        _rec(C, ey="0.06"),
    )
    chosen = _bought(
        _decide(FundamentalsValueParameters(top_n=2, max_staleness_days=200), data=_Data(stale))
    )
    assert chosen == [B, C]  # A has the best yield but its newest filing is too old
    fresh_enough = (_rec(A, ey="0.10", knowable=SESSION - timedelta(days=200)), *stale[1:])
    assert _bought(_decide(FundamentalsValueParameters(top_n=2), data=_Data(fresh_enough))) == [
        A,
        B,
    ]


# ── 2. the three signals ────────────────────────────────────────────────────────────────────────


def test_value_ranks_on_earnings_yield_highest_first_inversion() -> None:
    assert _bought(_decide(FundamentalsValueParameters(top_n=2))) == [A, B]


def test_value_never_buys_a_loss_maker() -> None:
    only_losers = (_rec(A, ey="-0.01"), _rec(B, ey="-0.20"))
    decision = _decide(FundamentalsValueParameters(top_n=2), data=_Data(only_losers))
    assert decision.orders == ()


def test_growth_ranks_on_stated_growth_and_skips_names_without_it_inversion() -> None:
    params = FundamentalsValueParameters(signal=FundamentalsSignal.GROWTH, top_n=2)
    assert _bought(_decide(params)) == [D, E]  # the loss-maker's growth is stated, so it ranks
    without = tuple(
        FundamentalsRecord(
            isin=r.isin,
            earnings_yield=r.earnings_yield,
            earnings_growth=None if r.isin in (D, E) else r.earnings_growth,
            roe=r.roe,
            price=r.price,
            knowable_date=r.knowable_date,
        )
        for r in _RECORDS
    )
    assert _bought(_decide(params, data=_Data(without))) == [B, C]


def test_quality_value_blends_the_two_ranks_and_needs_roe_inversion() -> None:
    params = FundamentalsValueParameters(signal=FundamentalsSignal.QUALITY_VALUE, top_n=2)
    # Yield ranks: A0 B1 C2 D3. ROE ranks: A0 C1 D2 B3. Sums: A0 C3 B4 D5 -> A, C.
    assert _bought(_decide(params)) == [A, C]
    no_roe = tuple(
        FundamentalsRecord(
            isin=r.isin,
            earnings_yield=r.earnings_yield,
            earnings_growth=r.earnings_growth,
            roe=None if r.isin == A else r.roe,
            price=r.price,
            knowable_date=r.knowable_date,
        )
        for r in _RECORDS
    )
    assert A not in _bought(_decide(params, data=_Data(no_roe)))


def test_momentum_value_blends_yield_and_momentum_ranks_and_needs_both_inversion() -> None:
    # Yield ranks: A0 B1 C2 D3. Momentum ranks: D0 C1 B2 A3. Sums: A3 B3 C3 D3 -> ties on ISIN.
    symmetric = (
        _rec(A, ey="0.10", momentum="0.05"),
        _rec(B, ey="0.08", momentum="0.10"),
        _rec(C, ey="0.06", momentum="0.20"),
        _rec(D, ey="0.04", momentum="0.40"),
        _rec(E, ey="-0.02", momentum="1.00"),  # loss-maker: never eligible
    )
    params = FundamentalsValueParameters(signal=FundamentalsSignal.MOMENTUM_VALUE, top_n=2)
    assert _bought(_decide(params, data=_Data(symmetric))) == [A, B]
    # Give C the momentum lead outright: yield rank 2 + momentum rank 0 = 2, the best blend.
    tilted = (
        _rec(A, ey="0.10", momentum="0.05"),
        _rec(B, ey="0.08", momentum="0.10"),
        _rec(C, ey="0.06", momentum="0.90"),
        _rec(D, ey="0.04", momentum="0.40"),
    )
    assert C in _bought(_decide(params, data=_Data(tilted)))
    # A name without a momentum figure is unrankable under the blend, whatever its yield.
    no_momentum = (
        _rec(A, ey="0.50"),
        _rec(B, ey="0.08", momentum="0.1"),
        _rec(C, ey="0.06", momentum="0.2"),
    )
    assert A not in _bought(_decide(params, data=_Data(no_momentum)))


def test_rank_candidates_is_deterministic_and_breaks_ties_on_isin() -> None:
    tied = (_rec(B, ey="0.10"), _rec(A, ey="0.10"), _rec(C, ey="0.10"))
    ranked = rank_candidates(
        tied, signal=FundamentalsSignal.VALUE, session=SESSION, max_staleness_days=200
    )
    assert [r.isin for r in ranked] == [A, B, C]


# ── 3. mechanics ───────────────────────────────────────────────────────────────────────────────


def test_every_order_is_a_whole_share_and_tagged_fundamentals() -> None:
    decision = _decide(FundamentalsValueParameters(top_n=3))
    assert decision.orders
    for order in decision.orders:
        assert isinstance(order.quantity, int) and order.quantity > 0
        assert order.tag == "FUNDAMENTALS"
        assert order.exchange is Exchange.NSE
    assert all(e.decision in (Decision.BUY, Decision.SELL) for e in decision.entries)


def test_banding_keeps_a_holding_inside_the_outer_band_inversion() -> None:
    held = (Holding(isin=C, exchange=Exchange.NSE, quantity=10, average_price=_D("100")),)
    # C is rank 3: outside the top-2 buy band but inside a top-3 sell band.
    banded = _decide(
        FundamentalsValueParameters(top_n=2, sell_band=3), broker=_Broker(holdings=held)
    )
    assert not [o for o in banded.orders if o.side is Side.SELL]
    unbanded = _decide(FundamentalsValueParameters(top_n=2), broker=_Broker(holdings=held))
    sells = [o for o in unbanded.orders if o.side is Side.SELL]
    assert [o.isin for o in sells] == [C]
    assert sells[0].quantity == 10


def test_no_orders_off_a_rebalance_session() -> None:
    decision = _decide(FundamentalsValueParameters(), data=_Data(rebalance=False))
    assert decision.orders == ()
    assert decision.entries == ()


def test_same_inputs_produce_the_same_decision() -> None:
    a = _decide(FundamentalsValueParameters(top_n=3))
    b = _decide(FundamentalsValueParameters(top_n=3))
    assert a.orders == b.orders
    assert a.evidence.ref() == b.evidence.ref()


def test_bad_parameters_are_refused() -> None:
    with pytest.raises(ValueError):
        FundamentalsValueParameters(top_n=0)
    with pytest.raises(ValueError):
        FundamentalsValueParameters(top_n=5, sell_band=3)
    with pytest.raises(ValueError):
        FundamentalsValueParameters(max_staleness_days=0)
    with pytest.raises(TypeError):
        FundamentalsValueParameters(buy_budget_fraction=0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _rec(A, ey="0.1", price="100").__class__(
            isin=A,
            earnings_yield=0.1,  # type: ignore[arg-type]
            earnings_growth=None,
            roe=None,
            price=_D("100"),
            knowable_date=SESSION,
        )
