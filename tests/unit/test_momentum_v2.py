"""M9.5 — momentum policy v2, unit-tested offline (EXECUTION_PLAN §7, X2).

Each of the four v2 changes — 12-1 ranking, turnover banding, the regime filter and vol-scaled
weights — is a separately-toggleable parameter, and each is pinned here by an **inversion test**: a
test that fails if the logic is reversed (a name ranked by the wrong window, a holding churned that
banding should keep, the regime read the wrong way round, weight given to the *more* volatile name).
Reversing any one behaviour flips an assertion — which is the whole point of building the changes as
independent toggles (M9.5 acceptance #1).

Also pinned: with all four toggles off the policy reproduces the naive top-N decision exactly (the
baseline the increment report is struck against); the point-in-time guard fires on a
not-yet-knowable signal *and* on a not-yet-knowable regime reading (invariant #7, acceptance #3);
orders are whole shares; and the same inputs give the same decision (determinism). No store, no
network, no wall clock (B8, invariant #11) — the policy stands alone against an in-memory source.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from analyst.journal.models import Decision, Sleeve
from backtest.policies.momentum_v2 import (
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
    inverse_vol_weights,
)
from backtest.replay import SessionContext, SessionDecision
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side

SESSION = date(2020, 1, 1)

# Real-shaped ISINs (invariant #2), distinct so ties break deterministically.
A = "INE001A01036"
B = "INE002A01018"
C = "INE009A01021"
D = "INE040A01034"
E = "INE467B01029"


def _rec(
    isin: str,
    *,
    m012: str,
    m121: str | None = None,
    price: str = "100",
    vol: str = "0.20",
    knowable: date = SESSION,
) -> MomentumV2Record:
    """A candidate record; ``m121`` defaults to ``m012`` when the two windows are not under test."""
    return MomentumV2Record(
        isin=isin,
        momentum_0_12=Decimal(m012),
        momentum_12_1=Decimal(m012 if m121 is None else m121),
        price=Decimal(price),
        volatility=Decimal(vol),
        knowable_date=knowable,
    )


# Five candidates with distinct 0-12 returns: A > B > C > D > E.
_RECORDS = (
    _rec(A, m012="0.50"),
    _rec(B, m012="0.40"),
    _rec(C, m012="0.30"),
    _rec(D, m012="0.20"),
    _rec(E, m012="-0.10"),
)


class _Data:
    """An in-memory ``MomentumV2Data``: fixed candidates, a rebalance flag, an optional regime."""

    def __init__(
        self,
        records: tuple[MomentumV2Record, ...],
        *,
        rebalance: bool = True,
        regime: RegimeReading | None = None,
    ) -> None:
        self._records = records
        self._rebalance = rebalance
        self._regime = regime

    def is_rebalance(self, session: date) -> bool:
        return self._rebalance

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return Dataset.declaring(
            f"momentum@{as_of.isoformat()}",
            self._records,
            knowable_date=lambda record: record.knowable_date,
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = self._regime or RegimeReading(
            index_level=Decimal("100"), moving_average=Decimal("90"), knowable_date=as_of
        )
        return Dataset.declaring(
            f"regime@{as_of.isoformat()}",
            (reading,),
            knowable_date=lambda r: r.knowable_date,
        )


class _LeakingSignal(_Data):
    """A source whose candidate records claim to be knowable *after* the session — a leak."""

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        future = date(as_of.year + 1, as_of.month, as_of.day)
        leaked = tuple(
            MomentumV2Record(
                isin=r.isin,
                momentum_0_12=r.momentum_0_12,
                momentum_12_1=r.momentum_12_1,
                price=r.price,
                volatility=r.volatility,
                knowable_date=future,
            )
            for r in self._records
        )
        return Dataset.declaring("leak", leaked, knowable_date=lambda r: r.knowable_date)


class _LeakingRegime(_Data):
    """A source whose regime reading claims to be knowable *after* the session — a leak."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        future = date(as_of.year + 1, as_of.month, as_of.day)
        leaked = RegimeReading(
            index_level=Decimal("100"), moving_average=Decimal("90"), knowable_date=future
        )
        return Dataset.declaring("leak", (leaked,), knowable_date=lambda r: r.knowable_date)


class _FakeBroker:
    """A minimal ``Broker`` read surface: fixed holdings and free cash. Records nothing."""

    def __init__(self, *, cash: Decimal, holdings: tuple[Holding, ...] = ()) -> None:
        self._cash = cash
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(session: date, broker: _FakeBroker) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


def _holding(isin: str, quantity: int, price: str = "100") -> Holding:
    return Holding(
        isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=Decimal(price)
    )


def _bought(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.BUY}


def _sold(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.SELL}


# ── all toggles off reproduces the naive top-N policy ────────────────────────────────────────────


def test_all_toggles_off_holds_exactly_the_top_n_and_sells_dropouts() -> None:
    """The default v2 config is the naive rule: hold the top-N, sell a holding that left it."""
    policy = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=3))
    held = (_holding(E, 40),)  # E ranks 5th — outside the top-3
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"), holdings=held)))

    assert _bought(decision) <= {A, B, C}
    assert _bought(decision), "the top names should be bought with a full budget"
    assert D not in _bought(decision) and E not in _bought(decision)
    assert _sold(decision) == {E}  # the drop-out is liquidated in full (naive rule, no banding)


def test_all_toggles_off_matches_the_naive_policy_orders() -> None:
    """Parity: v2 all-off and the naive policy stage the same orders on the same inputs."""
    from backtest.policies.naive_momentum import (
        MomentumParameters,
        MomentumRecord,
        NaiveMomentumPolicy,
    )

    class _NaiveData:
        def is_rebalance(self, session: date) -> bool:
            return True

        def signal(self, as_of: date) -> Dataset[MomentumRecord]:
            records = tuple(
                MomentumRecord(
                    isin=r.isin,
                    momentum=r.momentum_0_12,
                    price=r.price,
                    knowable_date=r.knowable_date,
                )
                for r in _RECORDS
            )
            return Dataset.declaring(
                "n", records, knowable_date=lambda record: record.knowable_date
            )

    held = (_holding(E, 40),)
    v2 = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=3)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("500000"), holdings=held))
    )
    naive = NaiveMomentumPolicy(_NaiveData(), MomentumParameters(top_n=3)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("500000"), holdings=held))
    )
    assert [(o.isin, o.side, o.quantity) for o in v2.orders] == [
        (o.isin, o.side, o.quantity) for o in naive.orders
    ]


# ── 12-1 momentum: rank on the return skipping the most recent month ─────────────────────────────


def test_12_1_ranking_picks_a_different_name_than_0_12_inversion() -> None:
    """A name that ran up entirely in the last month leads on 0-12 but not on 12-1.

    ``X`` has a big 0-12 return (+1.00) but a flat 12-1 return (+0.05) — the whole move was the
    excluded last month. ``Y`` is steady (+0.50 over 0-12, +0.60 over 12-1). Ranking on 0-12 picks
    ``X``; ranking on 12-1 picks ``Y``. Flip the toggle and the single pick flips — the inversion.
    """
    x = _rec(A, m012="1.00", m121="0.05")
    y = _rec(B, m012="0.50", m121="0.60")
    data = _Data((x, y))

    on_0_12 = MomentumV2Policy(data, MomentumV2Parameters(top_n=1, use_12_1=False)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))
    )
    on_12_1 = MomentumV2Policy(data, MomentumV2Parameters(top_n=1, use_12_1=True)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))
    )
    assert _bought(on_0_12) == {A}  # 0-12 leader (the last-month spike)
    assert _bought(on_12_1) == {B}  # 12-1 leader (steady, spike excluded)


# ── turnover banding / hysteresis: keep a holding until it leaves the outer band ──────────────────


def test_banding_keeps_a_holding_inside_the_outer_band_that_naive_would_sell_inversion() -> None:
    """C ranks 3rd; with top_n=2 the naive rule sells it, banding to top-4 keeps it (the inversion).

    ``C`` (rank 3) and ``E`` (rank 5) are both held. Without banding (buy band = sell band = top-2)
    both are drop-outs and sold. With ``sell_band=4`` only ``E`` (outside the top-4) is sold; ``C``
    (inside the band) is held. So the same holding is sold under one setting and kept under the
    other — reversing the banding logic flips the assertion.
    """
    held = (_holding(C, 10), _holding(E, 10))

    def broker() -> _FakeBroker:
        return _FakeBroker(cash=Decimal("1000000"), holdings=held)

    naive = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=2)).decide(
        _ctx(SESSION, broker())
    )
    banded = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=2, sell_band=4)).decide(
        _ctx(SESSION, broker())
    )

    assert _sold(naive) == {C, E}  # no banding: both drop-outs churned
    assert _sold(banded) == {E}  # banding: C is inside the top-4 band and kept
    assert C not in _sold(banded)


def test_sell_band_below_top_n_is_refused() -> None:
    """The outer band cannot be tighter than the buy band — a nonsensical config fails loud."""
    with pytest.raises(ValueError, match="sell_band must be >= top_n"):
        MomentumV2Parameters(top_n=20, sell_band=10)


# ── regime filter: hold the basket only while the index is above its moving average ───────────────


def test_regime_off_parks_the_basket_and_buys_nothing_inversion() -> None:
    """Index below its MA → sell the whole basket, buy nothing; above → normal. Flipping inverts.

    Risk-off (level 100 < MA 120): the held name is sold and no buy is staged — the cash is parked
    in the liquid sleeve. Risk-on (level 120 >= MA 100): the basket is bought as usual. The only
    change between the two runs is which side of the MA the level sits, so reversing the comparison
    reverses the decision.
    """
    held = (_holding(A, 10),)
    risk_off = RegimeReading(
        index_level=Decimal("100"), moving_average=Decimal("120"), knowable_date=SESSION
    )
    risk_on = RegimeReading(
        index_level=Decimal("120"), moving_average=Decimal("100"), knowable_date=SESSION
    )
    params = MomentumV2Parameters(top_n=3, regime_filter=True)

    off = MomentumV2Policy(_Data(_RECORDS, regime=risk_off), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000"), holdings=held))
    )
    on = MomentumV2Policy(_Data(_RECORDS, regime=risk_on), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000"), holdings=held))
    )

    assert _bought(off) == set()  # risk-off: nothing bought
    assert _sold(off) == {A}  # risk-off: the basket is liquidated to cash
    park_entries = [e for e in off.entries if e.decision is Decision.SELL]
    assert park_entries and park_entries[0].sleeve is Sleeve.CASH  # parked in the liquid sleeve

    assert _bought(on), "risk-on: the basket should be bought"
    assert _sold(on) == set()  # A is top-ranked and held; nothing to sell


def test_regime_reading_at_the_ma_is_risk_on() -> None:
    """The boundary is inclusive: level == MA counts as risk-on (holds the basket)."""
    at = RegimeReading(
        index_level=Decimal("100"), moving_average=Decimal("100"), knowable_date=SESSION
    )
    assert at.risk_on is True


# ── volatility-scaled weights: size ~ 1/vol, not equal, not ~vol ──────────────────────────────────


def test_inverse_vol_weights_favour_the_lower_vol_name_and_sum_to_one_inversion() -> None:
    """A name with half the volatility gets twice the weight; the weights sum to exactly 1.

    ``A`` has vol 0.10, ``B`` vol 0.20 — ``A``'s inverse-vol weight must be double ``B``'s (2/3 vs
    1/3), the opposite of a vol-proportional (wrong) sizing that would underweight the calm name.
    """
    weights = inverse_vol_weights({A: Decimal("0.10"), B: Decimal("0.20")})
    assert sum(weights.values()) == Decimal("1")
    assert weights[A] > weights[B]  # lower vol → higher weight (inversion tripwire)
    # 1/0.10 : 1/0.20 = 2 : 1, so A is exactly twice B.
    assert weights[A] == (weights[B] * 2).quantize(Decimal("0.00000001")) or abs(
        weights[A] - weights[B] * 2
    ) <= Decimal("0.00000001")


def test_vol_scaling_buys_more_of_the_lower_vol_name() -> None:
    """Through the allocator: at equal prices, vol-scaling buys more shares of the calmer name."""
    low = _rec(A, m012="0.50", vol="0.10", price="100")
    high = _rec(B, m012="0.40", vol="0.40", price="100")
    data = _Data((low, high))

    scaled = MomentumV2Policy(data, MomentumV2Parameters(top_n=2, vol_scaled=True)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))
    )

    qty = {order.isin: order.quantity for order in scaled.orders if order.side is Side.BUY}
    assert qty.get(A, 0) > qty.get(B, 0), "the lower-vol name should get the larger allocation"


def test_equal_weight_does_not_favour_the_lower_vol_name() -> None:
    """The contrast: with vol-scaling off, equal weight sizes the two names roughly evenly."""
    low = _rec(A, m012="0.50", vol="0.10", price="100")
    high = _rec(B, m012="0.40", vol="0.40", price="100")
    data = _Data((low, high))

    equal = MomentumV2Policy(data, MomentumV2Parameters(top_n=2, vol_scaled=False)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))
    )
    qty = {order.isin: order.quantity for order in equal.orders if order.side is Side.BUY}
    assert abs(qty.get(A, 0) - qty.get(B, 0)) <= 1  # equal weight, equal price → ~equal shares


def test_zero_volatility_is_refused() -> None:
    """A non-positive volatility cannot be inverse-vol weighted — it fails loud, not silently."""
    with pytest.raises(ValueError, match="must be positive"):
        inverse_vol_weights({A: Decimal("0")})


# ── point-in-time guard (invariant #7, acceptance #3) ─────────────────────────────────────────────


def test_a_not_yet_knowable_signal_trips_the_guard() -> None:
    """The policy admits candidates via ctx.pit; a future-dated record raises (invariant #7)."""
    policy = MomentumV2Policy(_LeakingSignal(_RECORDS), MomentumV2Parameters(top_n=3))
    with pytest.raises(PitError):
        policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


def test_a_not_yet_knowable_regime_reading_trips_the_guard() -> None:
    """The regime filter admits its reading via ctx.pit; a future-dated regime raises (inv. #7)."""
    policy = MomentumV2Policy(
        _LeakingRegime(_RECORDS), MomentumV2Parameters(top_n=3, regime_filter=True)
    )
    with pytest.raises(PitError):
        policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))


def test_regime_reading_is_not_read_when_the_filter_is_off() -> None:
    """With the regime filter off, a leaking regime source is never touched, so no guard trips."""
    policy = MomentumV2Policy(
        _LeakingRegime(_RECORDS), MomentumV2Parameters(top_n=3, regime_filter=False)
    )
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    assert _bought(decision)  # ran normally — the (leaking) regime was never consulted


# ── whole shares, heartbeat, determinism ──────────────────────────────────────────────────────────


def test_every_order_is_a_whole_number_of_shares() -> None:
    """The allocator produces whole-share orders; nothing fractional reaches the broker."""
    policy = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=5, vol_scaled=True))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    for order in decision.orders:
        assert isinstance(order.quantity, int)
        assert order.quantity > 0


def test_no_orders_off_a_rebalance_session() -> None:
    """A non-rebalance session returns evidence and no orders — the engine writes the heartbeat."""
    policy = MomentumV2Policy(_Data(_RECORDS, rebalance=False))
    decision = policy.decide(_ctx(SESSION, _FakeBroker(cash=Decimal("1000000"))))
    assert decision.orders == ()
    assert decision.entries == ()
    assert decision.evidence.items


def test_same_inputs_produce_the_same_decision() -> None:
    """Determinism: two runs with every toggle on return identical orders and entries."""
    params = MomentumV2Parameters(
        top_n=4, use_12_1=True, sell_band=5, regime_filter=True, vol_scaled=True
    )
    on = RegimeReading(
        index_level=Decimal("120"), moving_average=Decimal("100"), knowable_date=SESSION
    )
    data = _Data(_RECORDS, regime=on)
    first = MomentumV2Policy(data, params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    second = MomentumV2Policy(data, params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("750000")))
    )
    assert [(o.isin, o.side, o.quantity) for o in first.orders] == [
        (o.isin, o.side, o.quantity) for o in second.orders
    ]
    assert [(e.isin, e.decision) for e in first.entries] == [
        (e.isin, e.decision) for e in second.entries
    ]


def test_all_toggles_on_still_buys_a_basket_when_risk_on() -> None:
    """The four changes compose: 12-1 + banding + risk-on regime + vol-scaling buys a basket."""
    params = MomentumV2Parameters(
        top_n=3, use_12_1=True, sell_band=4, regime_filter=True, vol_scaled=True
    )
    on = RegimeReading(
        index_level=Decimal("120"), moving_average=Decimal("100"), knowable_date=SESSION
    )
    decision = MomentumV2Policy(_Data(_RECORDS, regime=on), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("1000000")))
    )
    assert _bought(decision), "all-on, risk-on: a basket should be bought"
