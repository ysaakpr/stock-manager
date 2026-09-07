"""M10.7 — the swing-composite policy, unit-tested offline (EXECUTION_PLAN §7, X2).

Every entry and exit rule is pinned by an **inversion test**: a test that fails if the rule is
reversed — the wrong end of the composite bought, a name churned that the band should carry, a
still-qualifying name liquidated at ``max_hold``, a stop that fires upward, a rejected sell
re-staged the very next session. The point-in-time guard is pinned on both reads the policy makes
(the signal and the daily marks), and determinism is pinned by deciding the same session twice.

No store, no network, no wall clock (B8, invariant #11): the policy stands alone against an
in-memory source.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision, Sleeve
from backtest.policies.swing_composite import (
    SwingCompositeParameters,
    SwingCompositePolicy,
    SwingRecord,
    composite_scores,
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
    high: str = "0.90",
    delivery: str = "0.50",
    momentum: str = "0.20",
    vol: str = "0.02",
    price: str = "100",
    knowable: date = SESSION,
) -> SwingRecord:
    return SwingRecord(
        isin=isin,
        high_proximity=Decimal(high),
        delivery_share=Decimal(delivery),
        momentum_12_1=Decimal(momentum),
        volatility=Decimal(vol),
        price=Decimal(price),
        knowable_date=knowable,
    )


#: Five candidates, every leg ordered A > B > C > D > E, so the composite order is unambiguous.
_RECORDS = (
    _rec(A, high="0.99", delivery="0.80", momentum="0.90"),
    _rec(B, high="0.95", delivery="0.70", momentum="0.70"),
    _rec(C, high="0.90", delivery="0.60", momentum="0.50"),
    _rec(D, high="0.85", delivery="0.50", momentum="0.30"),
    _rec(E, high="0.80", delivery="0.40", momentum="0.10"),
)


class _Data:
    """An in-memory ``SwingCompositeData``: fixed candidates, a rebalance flag, optional marks."""

    def __init__(
        self,
        records: tuple[SwingRecord, ...] = _RECORDS,
        *,
        rebalance: bool = True,
        marks: dict[str, str] | None = None,
    ) -> None:
        self._records = records
        self.rebalance = rebalance
        self.marks_at = marks

    def at(self, *, rebalance: bool | None = None, marks: dict[str, str] | None = None) -> _Data:
        """Move the fixture to the next session's state — what a test flips between decisions."""
        if rebalance is not None:
            self.rebalance = rebalance
        if marks is not None:
            self.marks_at = marks
        return self

    def is_rebalance(self, session: date) -> bool:
        return self.rebalance

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        return Dataset.declaring(
            f"swing@{as_of.isoformat()}", self._records, knowable_date=lambda r: r.knowable_date
        )

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        source = (
            {isin: Decimal(px) for isin, px in self.marks_at.items()}
            if self.marks_at is not None
            else {r.isin: r.price for r in self._records}
        )
        records = tuple(
            _rec(isin, price=str(price), knowable=as_of) for isin, price in sorted(source.items())
        )
        return Dataset.declaring(
            f"marks@{as_of.isoformat()}", records, knowable_date=lambda r: r.knowable_date
        )


class _LeakingSignal(_Data):
    """A source whose candidates claim to be knowable *after* the session — a leak."""

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        future = as_of + timedelta(days=1)
        leaked = tuple(_rec(r.isin, knowable=future) for r in self._records)
        return Dataset.declaring("leak", leaked, knowable_date=lambda r: r.knowable_date)


class _LeakingMarks(_Data):
    """A source whose daily marks claim to be knowable *after* the session — a leak."""

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        future = as_of + timedelta(days=1)
        leaked = tuple(_rec(r.isin, knowable=future) for r in self._records)
        return Dataset.declaring("leak", leaked, knowable_date=lambda r: r.knowable_date)


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


def _holding(isin: str, quantity: int = 10, price: str = "100") -> Holding:
    return Holding(
        isin=isin, exchange=Exchange.NSE, quantity=quantity, average_price=Decimal(price)
    )


def _bought(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.BUY}


def _sold(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.SELL}


def _run(
    policy: SwingCompositePolicy, sessions: int, broker: _FakeBroker, *, start: date = SESSION
) -> list[SessionDecision]:
    """Drive ``policy`` over consecutive sessions against one broker, returning each decision."""
    return [policy.decide(_ctx(start + timedelta(days=n), broker)) for n in range(sessions)]


# ── the composite score ──────────────────────────────────────────────────────────────────────────


def test_composite_ranks_the_best_of_every_leg_first_and_the_worst_last() -> None:
    """With all three legs ordered the same way, the composite reproduces that order."""
    scores = composite_scores(_RECORDS, SwingCompositeParameters())
    assert scores[A] > scores[B] > scores[C] > scores[D] > scores[E]


def test_composite_is_rank_normalised_not_level_driven() -> None:
    """One name with an absurd momentum level cannot buy its way past two better-ranked legs.

    The inversion: a z-score composite would let a 40x outlier dominate. Ranks cap each leg's
    contribution at one place, so B — ahead on two of three legs — still outranks the outlier.
    """
    records = (
        _rec(A, high="0.10", delivery="0.10", momentum="40.0"),  # last on two legs, wild on one
        _rec(B, high="0.99", delivery="0.99", momentum="0.01"),
        _rec(C, high="0.50", delivery="0.50", momentum="0.02"),
    )
    scores = composite_scores(records, SwingCompositeParameters())
    assert scores[B] > scores[A]


def test_a_zero_weighted_leg_is_ignored() -> None:
    """Zeroing a weight removes that leg — the ablation the report's signal arms rely on."""
    delivery_only = SwingCompositeParameters(weight_high=Decimal("0"), weight_momentum=Decimal("0"))
    records = (
        _rec(A, high="0.99", delivery="0.10", momentum="0.99"),  # best on the ignored legs
        _rec(B, high="0.10", delivery="0.99", momentum="0.10"),  # best on delivery alone
    )
    scores = composite_scores(records, delivery_only)
    assert scores[B] > scores[A]


def test_composite_of_an_empty_candidate_set_is_empty() -> None:
    assert composite_scores((), SwingCompositeParameters()) == {}


# ── entry ────────────────────────────────────────────────────────────────────────────────────────


def test_entry_buys_the_top_n_and_nothing_below_it() -> None:
    params = SwingCompositeParameters(top_n=2, exclude_vol_fraction=Decimal("0"))
    decision = SwingCompositePolicy(_Data(), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    assert _bought(decision) == {A, B}


def test_volatility_screen_excludes_the_most_volatile_from_being_bought() -> None:
    """The calmest names are bought; the screened tail is not. Invert the sort and A appears."""
    records = (
        _rec(A, high="0.99", delivery="0.80", momentum="0.90", vol="0.90"),  # best score, wildest
        _rec(B, high="0.95", delivery="0.70", momentum="0.70", vol="0.01"),
        _rec(C, high="0.90", delivery="0.60", momentum="0.50", vol="0.02"),
        _rec(D, high="0.85", delivery="0.50", momentum="0.30", vol="0.03"),
        _rec(E, high="0.80", delivery="0.40", momentum="0.10", vol="0.04"),
    )
    params = SwingCompositeParameters(top_n=2, exclude_vol_fraction=Decimal("0.20"))
    decision = SwingCompositePolicy(_Data(records), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    assert A not in _bought(decision)
    assert _bought(decision) == {B, C}


def test_orders_are_whole_shares_and_never_outrun_the_budget() -> None:
    params = SwingCompositeParameters(top_n=2, exclude_vol_fraction=Decimal("0"))
    decision = SwingCompositePolicy(_Data(), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("10000")))
    )
    buys = [o for o in decision.orders if o.side is Side.BUY]
    assert buys and all(isinstance(o.quantity, int) and o.quantity > 0 for o in buys)
    spent = sum(o.quantity * Decimal("100") for o in buys)
    assert spent <= Decimal("10000") * params.buy_budget_fraction


def test_no_candidates_means_no_orders_but_still_evidence() -> None:
    decision = SwingCompositePolicy(_Data(())).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    assert decision.orders == ()
    assert decision.evidence.trading_date == SESSION


# ── exit: the rank band ──────────────────────────────────────────────────────────────────────────


def _aged(
    params: SwingCompositeParameters, held: str, data: _Data, sessions: int = 8
) -> SessionDecision:
    """Carry ``held`` through ``sessions`` non-rebalance sessions, then take one rebalance decision.

    Ageing matters: every exit rule but the stop is gated behind ``min_hold_sessions``, so a rule
    tested on a freshly-seen holding would pass for the wrong reason.
    """
    broker = _FakeBroker(cash=Decimal("100000"), holdings=(_holding(held),))
    data.at(rebalance=False)
    policy = SwingCompositePolicy(data, params)
    _run(policy, sessions, broker)
    data.at(rebalance=True)
    return policy.decide(_ctx(SESSION + timedelta(days=sessions), broker))


def test_band_carries_a_holding_that_left_the_top_n_but_not_the_band() -> None:
    """E ranks 5th: outside a top-2 basket, inside a top-4 band — it is carried, not churned."""
    params = SwingCompositeParameters(
        top_n=2, sell_band=5, min_hold_sessions=2, exclude_vol_fraction=Decimal("0")
    )
    assert E not in _sold(_aged(params, E, _Data()))


def test_band_sells_a_holding_that_left_the_band() -> None:
    """The inversion of the test above: narrow the band past E's rank and it is sold."""
    params = SwingCompositeParameters(
        top_n=2, sell_band=3, min_hold_sessions=2, exclude_vol_fraction=Decimal("0")
    )
    assert E in _sold(_aged(params, E, _Data()))


def test_a_holding_that_left_the_scored_universe_is_sold() -> None:
    """A name with no score at all ranks below every scored name, so the band cannot save it."""
    params = SwingCompositeParameters(
        top_n=2, sell_band=99, min_hold_sessions=2, exclude_vol_fraction=Decimal("0")
    )
    gone = "INE999Z01011"
    broker = _FakeBroker(cash=Decimal("100000"), holdings=(_holding(gone),))
    data = _Data(rebalance=False, marks={gone: "100"})
    policy = SwingCompositePolicy(data, params)
    _run(policy, 4, broker)
    data.at(rebalance=True)
    assert gone in _sold(policy.decide(_ctx(SESSION + timedelta(days=4), broker)))


def test_volatility_screen_does_not_liquidate_a_holding_it_merely_bars_from_buying() -> None:
    """A holding that turned volatile is still *ranked*, so the band — not the screen — decides.

    The inversion of the bug this guards: screening before scoring leaves the holding unranked, and
    the "left the scored universe" branch then sells it on a screen it was never sold on.
    """
    records = (
        _rec(A, high="0.99", delivery="0.80", momentum="0.90", vol="0.99"),  # top score, screened
        _rec(B, high="0.95", delivery="0.70", momentum="0.70", vol="0.01"),
        _rec(C, high="0.90", delivery="0.60", momentum="0.50", vol="0.02"),
    )
    params = SwingCompositeParameters(
        top_n=1, sell_band=3, min_hold_sessions=2, exclude_vol_fraction=Decimal("0.34")
    )
    decision = _aged(params, A, _Data(records))
    assert A not in _sold(decision)  # ranked first, inside the band — carried
    assert A not in _bought(decision)  # but screened out of the buy set


# ── exit: min hold, max hold ─────────────────────────────────────────────────────────────────────


def test_min_hold_carries_a_name_that_would_otherwise_be_sold() -> None:
    """Inside ``min_hold_sessions`` the band cannot fire — the 7-day floor of the requested band."""
    params = SwingCompositeParameters(
        top_n=2, sell_band=3, min_hold_sessions=10, exclude_vol_fraction=Decimal("0")
    )
    assert E not in _sold(_aged(params, E, _Data(), sessions=3))


def test_max_hold_re_underwrites_rather_than_force_selling_a_still_qualifying_name() -> None:
    """At ``max_hold`` a name still inside the top-N is kept — selling it would pay a round trip
    only to buy it straight back."""
    params = SwingCompositeParameters(
        top_n=2,
        sell_band=99,
        min_hold_sessions=1,
        max_hold_sessions=3,
        exclude_vol_fraction=Decimal("0"),
    )
    assert A not in _sold(_aged(params, A, _Data(), sessions=6))


def test_max_hold_sells_a_name_that_no_longer_re_qualifies() -> None:
    """The inversion: same age, but E is outside the top-N, so the re-underwrite sells it even
    though the band alone would have carried it."""
    params = SwingCompositeParameters(
        top_n=2,
        sell_band=99,
        min_hold_sessions=1,
        max_hold_sessions=3,
        exclude_vol_fraction=Decimal("0"),
    )
    assert E in _sold(_aged(params, E, _Data(), sessions=6))


# ── exit: the trailing stop ──────────────────────────────────────────────────────────────────────


def test_trailing_stop_fires_on_a_non_rebalance_session() -> None:
    """A 20 % fall from the peak trips a 10 % stop on an ordinary session — no rebalance needed."""
    params = SwingCompositeParameters(trailing_stop=Decimal("0.10"), min_hold_sessions=0)
    broker = _FakeBroker(cash=Decimal("0"), holdings=(_holding(A),))
    data = _Data(rebalance=False, marks={A: "100"})
    policy = SwingCompositePolicy(data, params)
    policy.decide(_ctx(SESSION, broker))  # peak set at 100
    data.at(marks={A: "80"})
    assert A in _sold(policy.decide(_ctx(SESSION + timedelta(days=1), broker)))


def test_trailing_stop_does_not_fire_on_a_rise() -> None:
    """The inversion: the same move upward must not sell."""
    params = SwingCompositeParameters(trailing_stop=Decimal("0.10"), min_hold_sessions=0)
    broker = _FakeBroker(cash=Decimal("0"), holdings=(_holding(A),))
    data = _Data(rebalance=False, marks={A: "100"})
    policy = SwingCompositePolicy(data, params)
    policy.decide(_ctx(SESSION, broker))
    data.at(marks={A: "120"})
    assert _sold(policy.decide(_ctx(SESSION + timedelta(days=1), broker))) == set()


def test_trailing_stop_trails_the_peak_not_the_entry() -> None:
    """A name that doubled and gave back 12 % is stopped, though it is far above its entry."""
    params = SwingCompositeParameters(trailing_stop=Decimal("0.10"), min_hold_sessions=0)
    broker = _FakeBroker(cash=Decimal("0"), holdings=(_holding(A),))
    data = _Data(rebalance=False, marks={A: "100"})
    policy = SwingCompositePolicy(data, params)
    policy.decide(_ctx(SESSION, broker))
    for day, price in enumerate(("200", "176"), start=1):
        data.at(marks={A: price})
        decision = policy.decide(_ctx(SESSION + timedelta(days=day), broker))
    assert A in _sold(decision)


def test_no_trailing_stop_means_no_stop_outs() -> None:
    params = SwingCompositeParameters(trailing_stop=None, min_hold_sessions=0)
    broker = _FakeBroker(cash=Decimal("0"), holdings=(_holding(A),))
    data = _Data(rebalance=False, marks={A: "100"})
    policy = SwingCompositePolicy(data, params)
    policy.decide(_ctx(SESSION, broker))
    data.at(marks={A: "1"})
    assert _sold(policy.decide(_ctx(SESSION + timedelta(days=1), broker))) == set()


# ── an exit that cannot fill is not re-staged every session ──────────────────────────────────────


def test_a_sell_that_did_not_fill_is_not_restaged_inside_the_cooldown() -> None:
    """A holding still on the book after a staged sell had its order rejected (a delisted name has
    no reference bar). Re-staging it every rebalance would report a handful of stuck names as
    hundreds of trades — the cooldown is what stops that."""
    params = SwingCompositeParameters(
        top_n=2,
        sell_band=3,
        min_hold_sessions=0,
        resell_cooldown_sessions=10,
        trailing_stop=None,
        exclude_vol_fraction=Decimal("0"),
    )
    broker = _FakeBroker(cash=Decimal("100000"), holdings=(_holding(E),))
    policy = SwingCompositePolicy(_Data(), params)
    decisions = _run(policy, 4, broker)  # every session is a rebalance for this source
    assert E in _sold(decisions[0])
    assert all(E not in _sold(d) for d in decisions[1:])


def test_the_sell_is_retried_once_the_cooldown_has_passed() -> None:
    """The inversion: past the cooldown, an unfilled exit is attempted again."""
    params = SwingCompositeParameters(
        top_n=2,
        sell_band=3,
        min_hold_sessions=0,
        resell_cooldown_sessions=3,
        trailing_stop=None,
        exclude_vol_fraction=Decimal("0"),
    )
    broker = _FakeBroker(cash=Decimal("100000"), holdings=(_holding(E),))
    policy = SwingCompositePolicy(_Data(), params)
    decisions = _run(policy, 6, broker)
    assert sum(E in _sold(d) for d in decisions) >= 2


# ── cadence, journaling, point-in-time, determinism ──────────────────────────────────────────────


def test_a_non_rebalance_session_places_no_rotation_orders_but_still_journals_evidence() -> None:
    decision = SwingCompositePolicy(_Data(rebalance=False)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    assert decision.orders == ()
    assert decision.evidence.trading_date == SESSION
    assert decision.evidence.items


def test_every_order_is_journalled_with_its_sleeve_and_isin() -> None:
    params = SwingCompositeParameters(top_n=2, exclude_vol_fraction=Decimal("0"))
    decision = SwingCompositePolicy(_Data(), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    assert len(decision.entries) == len(decision.orders)
    for entry, order in zip(decision.entries, decision.orders, strict=True):
        assert entry.isin == order.isin
        assert entry.sleeve is Sleeve.TACTICAL
        assert entry.trading_date == SESSION
        assert entry.decision in (Decision.BUY, Decision.SELL)
        assert entry.rationale


def test_a_signal_not_yet_knowable_trips_the_pit_guard() -> None:
    with pytest.raises(PitError):
        SwingCompositePolicy(_LeakingSignal()).decide(
            _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
        )


def test_marks_not_yet_knowable_trip_the_pit_guard() -> None:
    """The stop reads prices every session, so that read is guarded too — not just the signal."""
    with pytest.raises(PitError):
        SwingCompositePolicy(_LeakingMarks()).decide(
            _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
        )


def test_the_same_inputs_give_the_same_decision() -> None:
    params = SwingCompositeParameters(top_n=3, exclude_vol_fraction=Decimal("0"))
    one, two = (
        SwingCompositePolicy(_Data(), params).decide(
            _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
        )
        for _ in range(2)
    )
    assert one.orders == two.orders
    assert [e.rationale for e in one.entries] == [e.rationale for e in two.entries]


# ── the parameter and record contracts ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_n": 0},
        {"top_n": 20, "sell_band": 19},  # a band inside the basket
        {"rebalance_interval_sessions": 0},
        {"max_hold_sessions": 0},
        {"resell_cooldown_sessions": 0},
        {"min_hold_sessions": -1},
        {"min_hold_sessions": 63, "max_hold_sessions": 63},
        {"buy_budget_fraction": Decimal("1.5")},
        {"exclude_vol_fraction": Decimal("1")},
        {"trailing_stop": Decimal("0")},
        {"trailing_stop": Decimal("1")},
    ],
)
def test_invalid_parameters_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        SwingCompositeParameters(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["high_proximity", "delivery_share", "momentum_12_1", "price"])
def test_a_float_signal_or_price_is_refused(field: str) -> None:
    """Money and signals are Decimal, never float (CLAUDE.md)."""
    kwargs: dict[str, object] = {
        "isin": A,
        "high_proximity": Decimal("0.9"),
        "delivery_share": Decimal("0.5"),
        "momentum_12_1": Decimal("0.2"),
        "volatility": Decimal("0.02"),
        "price": Decimal("100"),
        "knowable_date": SESSION,
    }
    kwargs[field] = 1.0
    with pytest.raises(TypeError):
        SwingRecord(**kwargs)  # type: ignore[arg-type]


def test_a_non_positive_price_is_refused() -> None:
    with pytest.raises(ValueError):
        _rec(A, price="0")
