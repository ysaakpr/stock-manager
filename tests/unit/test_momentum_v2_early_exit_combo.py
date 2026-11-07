"""M14.12 — the paper candidate (C0) with M14.5's daily exit: C1 (2% band) and C2 (unbanded).

Pre-registered in ``ops/studies/preregistration-m14-12-early-exit-combo-2026-11-07.md``. Before
M14.12 the parameters refused ``regime_daily_exit`` beside ``early_entry`` and the sector order; the
change is validation only (``decide`` already reads the daily exit before the early trigger). These
tests pin: what is now allowed and what stays refused; that D13's and C0's replays are byte-for-byte
what they were before the change; and the spell rule — a daily park opens a new parked spell in
which V2 may re-enter once — on the policy and on the real engine stack, each with an inverted twin.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.m12_rerun import ARM_SETS, EARLY_EXIT_COMBO_SET, NIFTY500_WF_SET
from backtest.policies.early_entry import EarlyEntry, RegimeTrendReading
from backtest.policies.momentum_v2 import (
    D13_SECTOR_REFILL,
    D13_V2_SECTOR_REFILL,
    PAPER_CANDIDATE_V2_SREFILL_2026_11_07,
    PAPER_RATIFIED_2026_09_06,
    V2_SREFILL_DAILY_EXIT,
    V2_SREFILL_DAILY_EXIT_BANDED,
    MomentumV2Parameters,
    MomentumV2Policy,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult
from backtest.run import _AccountingBroker
from backtest.sweep import COMBO_C0, COMBO_C1, COMBO_C2, D13_PAPER_BASELINE
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitError
from execution.broker import Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import NAMES, PRICE, RAILS, _Market
from tests.unit.test_momentum_v2_daily_regime import (
    _D13_DIGEST_BEFORE_M14_5,
    _OPENING,
    _REBALANCES,
    _SESSIONS,
    MID,
    REBALANCE,
    A,
    B,
    C,
    _Broker,
    _ctx,
    _held,
    _params,
    _ScriptedData,
    _sides,
)
from tests.unit.test_momentum_v2_early_entry import _EarlyData, _trend

D13 = PAPER_RATIFIED_2026_09_06
C0 = PAPER_CANDIDATE_V2_SREFILL_2026_11_07
C1 = V2_SREFILL_DAILY_EXIT_BANDED
C2 = V2_SREFILL_DAILY_EXIT
BAND = Decimal("0.02")

#: The digest of :func:`_replay` over C0, struck with the policy as it stood before M14.12
#: (origin/main ab69648 with only the pre-registration on top, 18e819e). Journal, book and rails
#: byte-for-byte: if the composition change moved C0 in any order, entry or fill, this moves.
_C0_DIGEST_BEFORE_M14_12 = "b08b7852e247bb7c1b63808378775cf0a793e71c88e56e040326470630a3757f"


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_c1_and_c2_are_c0_plus_exactly_the_daily_exit() -> None:
    def differing(params: MomentumV2Parameters) -> dict[str, object]:
        return {
            f.name: getattr(params, f.name)
            for f in fields(params)
            if getattr(params, f.name) != getattr(C0, f.name)
        }

    assert differing(C1) == {"regime_daily_exit": True, "regime_daily_band": BAND}
    assert differing(C2) == {"regime_daily_exit": True}
    assert not C1.regime_daily_reentry and not C2.regime_daily_reentry


def test_c0_is_the_paper_candidate_and_m14_10s_arm_by_fingerprint() -> None:
    assert repr(C0) == repr(D13_V2_SECTOR_REFILL)  # so M14.10's run digests carry over
    labels = [arm.label for arm in EARLY_EXIT_COMBO_SET]
    assert labels == [D13_PAPER_BASELINE.label, COMBO_C0, COMBO_C1, COMBO_C2]
    params = [arm.v2 for arm in EARLY_EXIT_COMBO_SET]
    assert params == [D13, C0, C1, C2]
    assert len({repr(p) for p in params}) == 4
    assert ARM_SETS["early-exit-combo"] == EARLY_EXIT_COMBO_SET
    assert repr(C0) == repr(NIFTY500_WF_SET[-1].v2)


def test_the_daily_exit_composes_only_with_a_trend_trigger() -> None:
    replace(C0, regime_daily_exit=True, regime_daily_band=BAND)
    replace(C0, regime_daily_exit=True, early_entry=EarlyEntry.CONFIRMED_CROSS)
    replace(D13, regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN)  # without S
    # Still refused: a second re-entry rule, the other triggers, S + exit with no trigger.
    with pytest.raises(ValueError, match="regime_daily_reentry"):
        replace(C2, regime_daily_reentry=True)
    with pytest.raises(ValueError, match="regime_daily_reentry"):
        replace(D13, regime_daily_reentry=True, early_entry=EarlyEntry.MOMENTUM_TURN)
    for refused in (EarlyEntry.SCALE_IN, EarlyEntry.BREADTH_TURN):
        with pytest.raises(ValueError, match="regime_daily_exit"):
            replace(D13, regime_daily_exit=True, early_entry=refused)
    with pytest.raises(ValueError, match="regime_daily_exit"):
        replace(D13_SECTOR_REFILL, regime_daily_exit=True)
    with pytest.raises(ValueError, match="vol_target_annual"):
        replace(C2, vol_target_annual=Decimal("0.15"))


# ── the spell rule on the policy ─────────────────────────────────────────────────────────────────


def _invested(params: MomentumV2Parameters, data: _EarlyData) -> MomentumV2Policy:
    """A policy whose REBALANCE read risk-on (level 105) and invested; ``data`` is reused."""
    policy = MomentumV2Policy(data, params)
    data.level = Decimal("105")
    first = policy.decide(_ctx(REBALANCE, _Broker()))
    assert _sides(first) == {Side.BUY} and not policy.parked
    return policy


def test_the_unbanded_exit_parks_below_the_ma_and_the_banded_one_waits_inversion() -> None:
    for band, parks in ((Decimal("0"), True), (BAND, False)):
        data = _EarlyData("105")
        policy = _invested(
            _params(
                regime_daily_exit=True, regime_daily_band=band, early_entry=EarlyEntry.MOMENTUM_TURN
            ),
            data,
        )
        data.level = Decimal("99")  # below MA200, above 0.98 x MA200
        decision = policy.decide(_ctx(MID, _Broker(holdings=_held(A, B))))
        assert (_sides(decision) == {Side.SELL}) is parks
        assert policy.parked is parks


def test_the_banded_exit_parks_below_the_band() -> None:
    data = _EarlyData("105")
    policy = _invested(
        _params(
            regime_daily_exit=True, regime_daily_band=BAND, early_entry=EarlyEntry.MOMENTUM_TURN
        ),
        data,
    )
    data.level = Decimal("97.99")
    decision = policy.decide(_ctx(MID, _Broker(holdings=_held(A, B))))
    assert _sides(decision) == {Side.SELL} and policy.parked


def test_without_the_exit_c0_rides_a_breakdown_to_the_rebalance_inversion() -> None:
    data = _EarlyData("105")
    policy = _invested(_params(early_entry=EarlyEntry.MOMENTUM_TURN), data)
    data.level = Decimal("90")
    assert policy.decide(_ctx(MID, _Broker(holdings=_held(A, B)))).orders == ()
    assert not policy.parked


def test_a_park_session_is_never_also_a_reentry() -> None:
    # The trend would fire, but the session that parks reads only the exit (one transition).
    data = _EarlyData("105")
    data.trend = _trend(level="99")
    policy = _invested(_params(regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN), data)
    data.level = Decimal("99")
    decision = policy.decide(_ctx(MID, _Broker(holdings=_held(A, B))))
    assert _sides(decision) == {Side.SELL} and policy.parked
    assert data.trend_reads == []


def _parked(policy: MomentumV2Policy) -> bool:
    """Read the flag through a call, so a type checker does not carry an earlier assert over."""
    return policy.parked


def test_each_daily_park_opens_a_new_spell_for_v2() -> None:
    # The pre-registered interaction (§3.2): V2 fires from 0.98 x MA200, C2 exits below MA200, so
    # in that gap the book parks and re-enters on alternating sessions — one re-entry per spell.
    data = _EarlyData("105")
    data.trend = _trend(level="99")
    policy = _invested(_params(regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN), data)
    data.level = Decimal("99")
    day = MID
    for _ in range(2):
        park = policy.decide(_ctx(day, _Broker(holdings=_held(A, B))))
        assert _sides(park) == {Side.SELL} and policy.parked
        day += timedelta(days=1)
        reentry = policy.decide(_ctx(day, _Broker(cash="1000000")))
        assert Side.BUY in _sides(reentry) and not _parked(policy)
        day += timedelta(days=1)


def test_a_quiet_trigger_leaves_a_daily_park_to_the_rebalance_inversion() -> None:
    data = _EarlyData("105")
    data.trend = _trend(level="99", short="98", short_lagged="99")  # MA50 falling
    policy = _invested(_params(regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN), data)
    data.level = Decimal("99")
    policy.decide(_ctx(MID, _Broker(holdings=_held(A, B))))
    assert policy.parked
    later = policy.decide(_ctx(MID + timedelta(days=1), _Broker(cash="1000000")))
    assert later.orders == () and policy.parked


def test_a_parked_book_is_not_re_parked_daily() -> None:
    data = _EarlyData("90")
    data.trend = _trend(level="90", short="98", short_lagged="99")
    policy = MomentumV2Policy(
        data, _params(regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN)
    )
    policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A, B, C))))  # risk-off: parks
    assert policy.parked
    assert policy.decide(_ctx(MID, _Broker(holdings=_held(A, B, C)))).orders == ()


def test_a_future_dated_exit_reading_trips_the_guard() -> None:
    data = _EarlyData("105")
    policy = _invested(_params(regime_daily_exit=True, early_entry=EarlyEntry.MOMENTUM_TURN), data)
    data._leak_regime = True  # the M14.5 source's leak switch
    with pytest.raises(PitError):
        policy.decide(_ctx(MID, _Broker(holdings=_held(A, B))))


# ── the real stack: replay digests and park/re-entry sessions ────────────────────────────────────


def _level(session: date) -> Decimal:
    """Risk-off at the January rebalance; the turn on 8 Jan below the MA; risk-on 10 Jan to
    14 Feb; 99 (inside the band) 15-21 Feb; 97 (below the band) 22-29 Feb; risk-on in March."""
    if session < date(2024, 1, 8):
        return Decimal("95")
    if session < date(2024, 1, 10):
        return Decimal("99")
    if session < date(2024, 2, 15):
        return Decimal("105")
    if session < date(2024, 2, 22):
        return Decimal("99")
    if session < date(2024, 3, 1):
        return Decimal("97")
    return Decimal("105")


class _ComboScript(_ScriptedData):
    """M14.5's ten names and monthly rebalances, with :func:`_level` as the regime and trend."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = RegimeReading(
            index_level=_level(as_of), moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)

    def regime_trend(self, as_of: date) -> Dataset[RegimeTrendReading]:
        turned = date(2024, 1, 8) <= as_of < date(2024, 2, 15) or as_of >= date(2024, 3, 1)
        level = _level(as_of)
        reading = RegimeTrendReading(
            index_level=level,
            moving_average=Decimal("100"),
            sessions_at_or_above=0 if level < 100 else 5,
            short_average=Decimal("100") if turned else Decimal("98"),
            short_average_lagged=Decimal("99"),
            level_lagged=Decimal("97"),
            knowable_date=as_of,
        )
        return Dataset.declaring(f"t@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


def _replay(params: MomentumV2Parameters, data: _ScriptedData | None = None) -> ReplayResult:
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
    sector_map = SectorMap(source="test", sha256="test", by_isin={i: i[3:6] for i in NAMES})
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=sector_map,
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in _SESSIONS}
    return ReplayEngine(
        policy=MomentumV2Policy(
            data if data is not None else _ComboScript(),
            params,
            order_caps=RAILS,
            sector_of=sector_map.sector_of,
        ),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


def _dates(result: ReplayResult, side: Decision) -> list[date]:
    return sorted({e.trading_date for e in result.journal if e.decision is side})


def test_d13_and_c0_replay_byte_for_byte_as_before_m14_12() -> None:
    from tests.unit.test_momentum_v2_daily_regime import _replay as _m14_5_replay

    assert _m14_5_replay(D13).digest() == _D13_DIGEST_BEFORE_M14_5
    assert _replay(C0).digest() == _C0_DIGEST_BEFORE_M14_12
    explicit_off = replace(C0, regime_daily_exit=False, regime_daily_band=Decimal("0"))
    assert _replay(explicit_off).digest() == _C0_DIGEST_BEFORE_M14_12


@pytest.mark.parametrize(
    ("params", "parks"),
    [
        (C0, []),  # exits monthly: the February breakdown is never acted on
        (C2, [date(2024, 2, 15)]),  # the first session below the MA
        (C1, [date(2024, 2, 22)]),  # the first session below 0.98 x the MA
    ],
)
def test_each_arm_parks_on_the_session_its_rule_says(
    params: MomentumV2Parameters, parks: list[date]
) -> None:
    result = _replay(params)
    assert _dates(result, Decision.SELL) == parks
    buys = _dates(result, Decision.BUY)
    assert buys[0] == date(2024, 1, 8)  # V2 re-enters the January park below the MA
    march = min(s for s in _REBALANCES if s.month == 3)
    if parks:
        # The trend is falling after each park, so the spell waits for March's risk-on rebalance:
        # no buy between the park and it.
        assert not [b for b in buys if parks[0] < b < march]
        assert march in buys
        assert result.digest() != _replay(C0).digest()
