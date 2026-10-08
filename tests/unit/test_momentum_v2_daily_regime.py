"""M14.5 — momentum v2's daily regime re-entry and exit (both off by default).

D13 reads the regime only on the monthly rebalance session. The two M14.5 switches read it on
every other session too: ``regime_daily_reentry`` re-enters an all-cash book on the first risk-on
session, ``regime_daily_exit`` parks an invested one on the first risk-off session, and
``regime_daily_band`` widens both triggers. Each behaviour test here has an inverted twin that
fails if the rule is flipped; the PIT tests prove the daily reading goes through the guard; and the
replay tests pin that the defaults reproduce D13's journal, book and rails byte-for-byte (the
digest was struck with the pre-M14.5 policy) and that D13's run fingerprint did not move.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision, Sleeve
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Position, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import NAMES, PRICE, RAILS, _Market

D13 = PAPER_RATIFIED_2026_09_06
REBALANCE = date(2024, 1, 1)
MID = date(2024, 1, 10)  # a non-rebalance session
A, B, C = NAMES[0], NAMES[1], NAMES[2]


def _params(**overrides: object) -> MomentumV2Parameters:
    fields = {
        "top_n": 2,
        "regime_filter": True,
        **overrides,
    }
    return MomentumV2Parameters(**fields)  # type: ignore[arg-type]


def _records(knowable: date) -> tuple[MomentumV2Record, ...]:
    return tuple(
        MomentumV2Record(
            isin=isin,
            momentum_0_12=Decimal(momentum),
            momentum_12_1=Decimal(momentum),
            price=PRICE,
            volatility=Decimal("0.2"),
            knowable_date=knowable,
        )
        for isin, momentum in ((A, "0.5"), (B, "0.4"), (C, "0.3"))
    )


class _Data:
    """Rebalance on REBALANCE only; a scripted regime level against a moving average of 100."""

    def __init__(self, level: str, *, leak_signal: bool = False, leak_regime: bool = False):
        self.level = Decimal(level)
        self._leak_signal = leak_signal
        self._leak_regime = leak_regime
        self.signal_reads: list[date] = []
        self.regime_reads: list[date] = []

    def is_rebalance(self, session: date) -> bool:
        return session == REBALANCE

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        self.signal_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak_signal else as_of
        return Dataset.declaring(
            f"m@{as_of}", _records(knowable), knowable_date=lambda r: r.knowable_date
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        self.regime_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak_regime else as_of
        reading = RegimeReading(
            index_level=self.level, moving_average=Decimal("100"), knowable_date=knowable
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


class _Broker:
    """The read surface the policy uses: settled holdings, unsettled positions, free cash."""

    def __init__(
        self,
        *,
        cash: str = "100000",
        holdings: tuple[Holding, ...] = (),
        positions: tuple[Position, ...] = (),
    ) -> None:
        self._cash = Decimal(cash)
        self._holdings = holdings
        self._positions = positions

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[Position, ...]:
        return self._positions

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _held(*isins: str) -> tuple[Holding, ...]:
    return tuple(
        Holding(isin=isin, exchange=Exchange.NSE, quantity=100, average_price=PRICE)
        for isin in isins
    )


def _ctx(session: date, broker: _Broker) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


def _decide(
    params: MomentumV2Parameters, data: _Data, broker: _Broker, session: date = MID
) -> SessionDecision:
    """One decision by a fresh (never parked) policy."""
    return MomentumV2Policy(data, params).decide(_ctx(session, broker))


def _sides(decision: SessionDecision) -> set[Side]:
    return {order.side for order in decision.orders}


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_the_daily_switches_are_off_by_default_and_d13_leaves_them_off() -> None:
    params = MomentumV2Parameters()
    assert not params.regime_daily_reentry
    assert not params.regime_daily_exit
    assert params.regime_daily_band == Decimal("0")
    assert not D13.regime_daily


def test_d13s_repr_is_the_pre_m14_5_rendering_so_its_run_fingerprint_is_unchanged() -> None:
    # The run identity is ``repr`` of the parameters (backtest.run_ledger). This is the string the
    # policy rendered before M14.5 added its fields; if it moves, every persisted D13 run is
    # orphaned and the paper book's evidence no longer matches its configuration.
    assert repr(D13) == (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )


def test_a_switched_on_variant_never_shares_d13s_fingerprint() -> None:
    from dataclasses import replace

    reentry = replace(D13, regime_daily_reentry=True)
    both = replace(D13, regime_daily_reentry=True, regime_daily_exit=True)
    banded = replace(reentry, regime_daily_band=Decimal("0.02"))
    renders = {repr(D13), repr(reentry), repr(both), repr(banded)}
    assert len(renders) == 4
    assert repr(reentry).endswith(", regime_daily_reentry=True)")
    assert "regime_daily_band=Decimal('0.02')" in repr(banded)


def test_the_daily_checks_are_validated() -> None:
    with pytest.raises(ValueError, match="regime_filter"):
        MomentumV2Parameters(regime_daily_reentry=True)
    with pytest.raises(ValueError, match="regime_filter"):
        MomentumV2Parameters(regime_daily_exit=True)
    with pytest.raises(ValueError, match="only to a daily"):
        _params(regime_daily_band=Decimal("0.02"))
    with pytest.raises(ValueError, match=r"\[0, 1\)"):
        _params(regime_daily_reentry=True, regime_daily_band=Decimal("-0.01"))
    with pytest.raises(TypeError):
        _params(regime_daily_reentry=True, regime_daily_band=0.02)


# ── daily re-entry ───────────────────────────────────────────────────────────────────────────────


def _parked_then(
    params: MomentumV2Parameters, data: _Data, level: str, broker: _Broker
) -> tuple[MomentumV2Policy, SessionDecision]:
    """Park on the risk-off REBALANCE (holding A..C), then decide MID at ``level``."""
    policy = MomentumV2Policy(data, params)
    data.level = Decimal("90")
    parked = policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A, B, C))))
    assert _sides(parked) == {Side.SELL} and policy.parked
    data.level = Decimal(level)
    return policy, policy.decide(_ctx(MID, broker))


def test_daily_reentry_rebalances_a_parked_book_on_a_risk_on_session() -> None:
    data = _Data("110")
    policy, decision = _parked_then(_params(regime_daily_reentry=True), data, "110", _Broker())
    assert {o.isin for o in decision.orders} == {A, B}  # the full top-2 rebalance
    assert _sides(decision) == {Side.BUY}
    assert data.signal_reads == [MID]  # the signal was computed for the mid-month session
    assert not policy.parked


def test_without_daily_reentry_a_parked_book_waits_for_the_rebalance_inversion() -> None:
    data = _Data("110")
    _, decision = _parked_then(_params(), data, "110", _Broker())
    assert decision.orders == ()
    assert data.regime_reads == [REBALANCE]  # D13's rule never reads the regime off a rebalance


def test_daily_reentry_does_not_fire_while_risk_off_inversion() -> None:
    policy, decision = _parked_then(
        _params(regime_daily_reentry=True), _Data("90"), "90", _Broker()
    )
    assert decision.orders == ()
    assert policy.parked


def test_a_parked_book_kept_at_the_holdings_floor_still_reenters() -> None:
    # A8's minimum-holdings rail refuses the last sells of a park, so a parked book keeps names.
    # Re-entry is keyed on the remembered park, not on an empty book, and rebalances around them.
    _, decision = _parked_then(
        _params(regime_daily_reentry=True), _Data("110"), "110", _Broker(holdings=_held(C))
    )
    assert _sides(decision) == {Side.SELL, Side.BUY}  # C left the top-2 band; A and B bought
    assert {o.isin for o in decision.orders if o.side is Side.BUY} == {A, B}


def test_daily_reentry_leaves_an_unparked_book_alone() -> None:
    # Never parked: an invested (or never-built) book is rebalanced monthly, as before.
    for broker in (_Broker(holdings=_held(C)), _Broker()):
        assert _decide(_params(regime_daily_reentry=True), _Data("110"), broker).orders == ()


def test_daily_reentry_alone_never_exits_mid_month() -> None:
    # "Monthly exit + daily re-entry": a risk-off reading mid-month leaves the basket held.
    decision = _decide(_params(regime_daily_reentry=True), _Data("90"), _Broker(holdings=_held(A)))
    assert decision.orders == ()


def test_parked_state_survives_a_resume() -> None:
    params = _params(regime_daily_reentry=True)
    fresh = MomentumV2Policy(_Data("110"), params)
    fresh.resume(None, parked=True)
    assert _sides(fresh.decide(_ctx(MID, _Broker()))) == {Side.BUY}
    reset = MomentumV2Policy(_Data("110"), params)
    reset.resume(None)
    assert reset.decide(_ctx(MID, _Broker())).orders == ()


# ── daily exit ───────────────────────────────────────────────────────────────────────────────────


def test_daily_exit_parks_an_invested_book_on_a_risk_off_session() -> None:
    decision = _decide(_params(regime_daily_exit=True), _Data("90"), _Broker(holdings=_held(A, C)))
    assert _sides(decision) == {Side.SELL}
    assert {o.isin for o in decision.orders} == {A, C}  # everything, not just band dropouts
    assert all(o.quantity == 100 for o in decision.orders)
    assert {e.sleeve for e in decision.entries} == {Sleeve.CASH}
    assert {e.decision for e in decision.entries} == {Decision.SELL}


def test_without_daily_exit_the_book_rides_a_mid_month_breakdown_inversion() -> None:
    decision = _decide(_params(), _Data("90"), _Broker(holdings=_held(A, C)))
    assert decision.orders == ()


def test_daily_exit_holds_while_risk_on_inversion() -> None:
    decision = _decide(_params(regime_daily_exit=True), _Data("110"), _Broker(holdings=_held(A)))
    assert decision.orders == ()


def test_daily_exit_alone_never_reenters_mid_month() -> None:
    _, decision = _parked_then(_params(regime_daily_exit=True), _Data("110"), "110", _Broker())
    assert decision.orders == ()


def test_daily_exit_never_re_parks_a_parked_book() -> None:
    # The names A8's floor kept are not offered for sale again every session.
    _, decision = _parked_then(
        _params(regime_daily_exit=True), _Data("90"), "90", _Broker(holdings=_held(A))
    )
    assert decision.orders == ()


def test_daily_exit_marks_the_book_parked() -> None:
    policy = MomentumV2Policy(_Data("90"), _params(regime_daily_exit=True))
    policy.decide(_ctx(MID, _Broker(holdings=_held(A))))
    assert policy.parked


def test_daily_exit_drops_a_pending_redeploy() -> None:
    params = _params(regime_daily_exit=True, redeploy_next_session=True)
    data = _Data("110")
    policy = MomentumV2Policy(data, params)

    ctx = _ctx
    # A risk-on rebalance that sells a dropout arms tomorrow's redeploy...
    policy.decide(ctx(REBALANCE, _Broker(cash="50", holdings=_held(C, NAMES[5]))))
    assert policy.pending is not None
    # ...but tomorrow reads risk-off: the book is parked, nothing is redeployed.
    data.level = Decimal("90")
    decision = policy.decide(ctx(date(2024, 1, 2), _Broker(holdings=_held(A, B))))
    assert _sides(decision) == {Side.SELL}
    assert policy.pending is None


# ── the band ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("level", "enters"), [("101", False), ("102", True), ("103", True)])
def test_a_banded_reentry_needs_the_level_above_ma_times_one_plus_band(
    level: str, enters: bool
) -> None:
    params = _params(regime_daily_reentry=True, regime_daily_band=Decimal("0.02"))
    _, decision = _parked_then(params, _Data(level), level, _Broker())
    assert bool(decision.orders) is enters


@pytest.mark.parametrize(("level", "exits"), [("99", False), ("98", False), ("97", True)])
def test_a_banded_exit_needs_the_level_below_ma_times_one_minus_band(
    level: str, exits: bool
) -> None:
    params = _params(regime_daily_exit=True, regime_daily_band=Decimal("0.02"))
    assert bool(_decide(params, _Data(level), _Broker(holdings=_held(A))).orders) is exits


def test_the_band_never_touches_the_monthly_rule() -> None:
    # At 101 vs a 100 MA the banded daily re-entry would wait; the rebalance-day rule does not.
    params = _params(regime_daily_reentry=True, regime_daily_band=Decimal("0.02"))
    decision = _decide(params, _Data("101"), _Broker(), session=REBALANCE)
    assert _sides(decision) == {Side.BUY}


# ── point in time ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("switch", ["regime_daily_reentry", "regime_daily_exit"])
def test_a_future_dated_daily_regime_reading_trips_the_guard(switch: str) -> None:
    with pytest.raises(PitError):
        _decide(
            _params(**{switch: True}), _Data("110", leak_regime=True), _Broker(holdings=_held(A))
        )


def test_a_future_dated_signal_on_a_reentry_session_trips_the_guard() -> None:
    policy = MomentumV2Policy(_Data("110", leak_signal=True), _params(regime_daily_reentry=True))
    policy.resume(None, parked=True)
    with pytest.raises(PitError):
        policy.decide(_ctx(MID, _Broker()))


def test_the_daily_reading_is_for_the_session_itself_never_a_later_one() -> None:
    data = _Data("90")
    _decide(_params(regime_daily_exit=True), data, _Broker(holdings=_held(A)))
    assert data.regime_reads == [MID]


# ── the real stack: replay digests ───────────────────────────────────────────────────────────────

#: Weekdays of Q1 2024; the first session of each month is a rebalance.
_SESSIONS = tuple(
    day for day in (date(2024, 1, 1) + timedelta(days=n) for n in range(91)) if day.weekday() < 5
)
_REBALANCES = frozenset(
    min(s for s in _SESSIONS if s.month == month) for month in {s.month for s in _SESSIONS}
)
_OPENING = Decimal("1000000")


def _risk_on(session: date) -> bool:
    # Risk-off at the January rebalance, back on mid-January, off for the second half of
    # February, on again from the 29th — so the monthly rule and the daily ones part ways.
    return date(2024, 1, 10) <= session < date(2024, 2, 15) or session >= date(2024, 2, 29)


class _ScriptedData:
    """Ten names, a momentum order that rotates each month, and the scripted regime above."""

    def is_rebalance(self, session: date) -> bool:
        return session in _REBALANCES

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        shift = as_of.month
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal((index + shift) % 10) / 10,
                momentum_12_1=Decimal((index * 3 + shift) % 10) / 10,
                price=PRICE,
                volatility=Decimal("0.1") + Decimal(index) / 100,
                knowable_date=as_of,
            )
            for index, isin in enumerate(NAMES)
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        level = Decimal("105") if _risk_on(as_of) else Decimal("95")
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


def _replay(params: MomentumV2Parameters) -> ReplayResult:
    clock = FrozenClock(_SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(_SESSIONS),
        opening_cash=_OPENING,
    )
    book = PortfolioBook()
    book.deposit(_SESSIONS[0], _OPENING)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=SectorMap(source="test", sha256="test", by_isin={i: i[3:6] for i in NAMES}),
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in _SESSIONS}
    return ReplayEngine(
        policy=MomentumV2Policy(_ScriptedData(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


#: The digest of :func:`_replay` over D13, struck with the policy as it stood before M14.5
#: (origin/main c504f9c, which includes M14.4's HOLD on an empty park). Journal, book and rails
#: byte-for-byte: if the defaults-off path drifts in any order, entry or fill, this moves.
_D13_DIGEST_BEFORE_M14_5 = "67d887030dd04994c4f675992924b893a108030d0b3d6270dd40963daf879b28"


def _trade_dates(result: ReplayResult, side: Decision) -> list[date]:
    return sorted({e.trading_date for e in result.journal if e.decision is side})


def test_defaults_off_reproduces_d13_byte_for_byte() -> None:
    from dataclasses import replace

    explicit_off = replace(
        D13, regime_daily_reentry=False, regime_daily_exit=False, regime_daily_band=Decimal("0")
    )
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M14_5
    assert _replay(explicit_off).digest() == _D13_DIGEST_BEFORE_M14_5


def test_d13_enters_at_the_february_rebalance_and_holds_through_the_breakdown() -> None:
    result = _replay(D13)
    assert _trade_dates(result, Decision.BUY)[0] == date(2024, 2, 1)
    assert _trade_dates(result, Decision.SELL) == []  # Feb 15-28 risk-off is never read


def test_daily_reentry_enters_on_the_first_risk_on_session_inversion() -> None:
    from dataclasses import replace

    result = _replay(replace(D13, regime_daily_reentry=True))
    assert _trade_dates(result, Decision.BUY)[0] == date(2024, 1, 10)
    assert result.digest() != _replay(D13).digest()
    # Monthly exit only: the mid-February breakdown is still not acted on.
    assert _trade_dates(result, Decision.SELL) == []


def test_daily_exit_and_reentry_park_on_the_breakdown_and_come_back() -> None:
    from dataclasses import replace

    result = _replay(replace(D13, regime_daily_reentry=True, regime_daily_exit=True))
    sells = _trade_dates(result, Decision.SELL)
    buys = _trade_dates(result, Decision.BUY)
    assert sells and sells[0] == date(2024, 2, 15)
    assert all(e.sleeve is Sleeve.CASH for e in result.journal if e.decision is Decision.SELL)
    assert buys[0] == date(2024, 1, 10)
    assert date(2024, 2, 29) in buys  # re-entered the session the regime turned back
    assert not [d for d in buys if date(2024, 2, 15) <= d < date(2024, 2, 29)]
    # A8's minimum-holdings floor stops the park at eight names, and the parked book is not
    # offered for sale again: one park session's worth of refusals, not one per risk-off session.
    floor_blocks = {
        e.trading_date
        for e in result.journal
        if e.decision is Decision.RAIL_BLOCK and "MIN_HOLDINGS" in e.payload["rails"]
    }
    assert floor_blocks == {date(2024, 2, 15)}
