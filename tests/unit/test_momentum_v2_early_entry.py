"""M14.9 — momentum v2's early re-entry triggers for a parked book (all off by default).

D13 re-enters a regime-parked book only on a risk-on monthly rebalance. M14.9's pre-registered
variants (``ops/studies/preregistration-m14-9-momentum-turn-entry-2026-11-05.md``) may re-enter it
between rebalances: a confirmed cross (V1), a momentum turn (V2), a half-size scale-in (V3) or a
breadth turn (V4). Each trigger test has an inverted twin that fails if the rule is flipped; the
replay tests pin that D13 is byte-for-byte what it was before M14.9 (its parameter ``repr``, and
the journal, book and rails digest struck before M14.5), and that each variant re-enters on the
session its rule says, on the real engine stack.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.breadth import BreadthError, BreadthSeries
from backtest.policies.early_entry import (
    CONFIRM_SESSIONS,
    SCALE_IN_FRACTION,
    BreadthReading,
    EarlyEntry,
    RegimeTrendReading,
    breadth_turn,
    confirmed_cross,
    momentum_turn,
)
from backtest.policies.momentum_v2 import (
    D13_EARLY_BREADTH_TURN,
    D13_EARLY_CONFIRMED_CROSS,
    D13_EARLY_MOMENTUM_TURN,
    D13_EARLY_SCALE_IN,
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
)
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult, SessionDecision
from backtest.run import _AccountingBroker
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
    _Data,
    _held,
    _params,
    _risk_on,
    _ScriptedData,
    _sides,
)

D13 = PAPER_RATIFIED_2026_09_06
VARIANTS = (
    D13_EARLY_CONFIRMED_CROSS,
    D13_EARLY_MOMENTUM_TURN,
    D13_EARLY_SCALE_IN,
    D13_EARLY_BREADTH_TURN,
)


def _trend(
    *,
    level: str = "101",
    ma: str = "100",
    streak: int | None = None,
    short: str = "100",
    short_lagged: str = "99",
    lagged: str = "95",
    knowable: date = MID,
) -> RegimeTrendReading:
    at_or_above = Decimal(level) >= Decimal(ma)
    return RegimeTrendReading(
        index_level=Decimal(level),
        moving_average=Decimal(ma),
        sessions_at_or_above=(CONFIRM_SESSIONS if at_or_above else 0) if streak is None else streak,
        short_average=Decimal(short),
        short_average_lagged=Decimal(short_lagged),
        level_lagged=Decimal(lagged),
        knowable_date=knowable,
    )


def _breadth(share: str, lagged: str, knowable: date = MID) -> BreadthReading:
    return BreadthReading(
        share_above=Decimal(share),
        share_above_lagged=Decimal(lagged),
        names_counted=100,
        knowable_date=knowable,
    )


# ── parameters and the run fingerprint ───────────────────────────────────────────────────────────


def test_early_entry_is_off_by_default_and_d13_leaves_it_off() -> None:
    assert MomentumV2Parameters().early_entry is None
    assert D13.early_entry is None


def test_d13s_repr_is_unchanged_by_m14_9() -> None:
    # The run identity is ``repr`` of the parameters (backtest.run_ledger); the string is the one
    # D13 rendered before M14.5, M14.6 and M14.9 added their fields.
    expected = (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )
    assert repr(D13) == expected
    assert repr(replace(D13, early_entry=None)) == expected


def test_each_variant_has_its_own_fingerprint() -> None:
    renders = {repr(D13), *(repr(v) for v in VARIANTS)}
    assert len(renders) == 5
    for variant in VARIANTS:
        assert repr(variant).endswith(f"early_entry={variant.early_entry!r})")
        assert replace(variant, early_entry=None) == D13  # one switch, nothing else changed


def test_early_entry_is_validated() -> None:
    with pytest.raises(ValueError, match="regime_filter"):
        MomentumV2Parameters(early_entry=EarlyEntry.CONFIRMED_CROSS)
    with pytest.raises(TypeError):
        _params(early_entry="confirmed_cross")
    with pytest.raises(ValueError, match="regime_daily_reentry"):
        _params(early_entry=EarlyEntry.SCALE_IN, regime_daily_reentry=True)
    with pytest.raises(ValueError, match="daily_slot_fill"):
        _params(early_entry=EarlyEntry.SCALE_IN, daily_slot_fill=True)
    with pytest.raises(ValueError, match="vol_target_annual"):
        _params(early_entry=EarlyEntry.SCALE_IN, vol_target_annual=Decimal("0.15"))


@pytest.mark.parametrize(
    "mode", [EarlyEntry.CONFIRMED_CROSS, EarlyEntry.MOMENTUM_TURN, EarlyEntry.BREADTH_TURN]
)
def test_a_trigger_without_its_data_seam_is_refused_at_construction(mode: EarlyEntry) -> None:
    with pytest.raises(TypeError, match="does not serve"):
        MomentumV2Policy(_Data("110"), _params(early_entry=mode))


def test_the_scale_in_needs_only_the_regime_reading() -> None:
    MomentumV2Policy(_Data("110"), _params(early_entry=EarlyEntry.SCALE_IN))


# ── the trigger predicates, each with its inversion ──────────────────────────────────────────────


@pytest.mark.parametrize(("streak", "fires"), [(0, False), (4, False), (5, True), (9, True)])
def test_confirmed_cross_needs_five_straight_closes_at_or_above_the_ma(
    streak: int, fires: bool
) -> None:
    level = "101" if streak else "99"
    assert confirmed_cross(_trend(level=level, streak=streak)) is fires


def test_momentum_turn_fires_below_the_ma_within_two_percent() -> None:
    assert momentum_turn(_trend(level="98.5"))  # still below MA200, but the trend has turned
    assert momentum_turn(_trend(level="98"))  # exactly 0.98 x MA


@pytest.mark.parametrize(
    "broken",
    [
        {"level": "97.9", "lagged": "95"},  # more than 2% below the MA
        {"short": "99", "short_lagged": "99"},  # MA50 flat, not rising
        {"short": "98", "short_lagged": "99"},  # MA50 falling
        {"lagged": "101"},  # 20-session return negative (level 101 below 101? equal: not > 0)
        {"lagged": "102"},  # 20-session return negative
    ],
)
def test_momentum_turn_needs_all_three_conditions_inversion(broken: dict[str, str]) -> None:
    assert not momentum_turn(_trend(**broken))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("share", "lagged", "fires"),
    [
        ("0.50", "0.40", True),
        ("0.62", "0.61", True),
        ("0.49", "0.30", False),  # below the threshold, though rising
        ("0.60", "0.60", False),  # at the threshold but not higher than 20 sessions ago
        ("0.60", "0.70", False),  # falling
    ],
)
def test_breadth_turn_needs_half_the_universe_above_and_rising(
    share: str, lagged: str, fires: bool
) -> None:
    assert breadth_turn(_breadth(share, lagged)) is fires


# ── the policy: a parked book, a mid-month session ───────────────────────────────────────────────


class _EarlyData(_Data):
    """The M14.5 test source plus scripted M14.9 trend and breadth readings."""

    def __init__(self, level: str, *, leak: bool = False) -> None:
        super().__init__(level)
        self.trend = _trend()
        self.breadth_reading = _breadth("0.6", "0.5")
        self._leak = leak
        self.trend_reads: list[date] = []
        self.breadth_reads: list[date] = []

    def regime_trend(self, as_of: date) -> Dataset[RegimeTrendReading]:
        self.trend_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak else as_of
        reading = replace(self.trend, knowable_date=knowable)
        return Dataset.declaring(f"t@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)

    def breadth(self, as_of: date) -> Dataset[BreadthReading]:
        self.breadth_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak else as_of
        reading = replace(self.breadth_reading, knowable_date=knowable)
        return Dataset.declaring(f"b@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


def _parked_then(
    mode: EarlyEntry, data: _EarlyData, broker: _Broker, level: str = "110"
) -> tuple[MomentumV2Policy, SessionDecision]:
    """Park on the risk-off REBALANCE (holding A..C), then decide MID at ``level``."""
    policy = MomentumV2Policy(data, _params(early_entry=mode))
    data.level = Decimal("90")
    parked = policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A, B, C))))
    assert _sides(parked) == {Side.SELL} and policy.parked
    data.level = Decimal(level)
    return policy, policy.decide(_ctx(MID, broker))


@pytest.mark.parametrize(
    "mode", [EarlyEntry.CONFIRMED_CROSS, EarlyEntry.MOMENTUM_TURN, EarlyEntry.BREADTH_TURN]
)
def test_a_firing_trigger_rebalances_a_parked_book(mode: EarlyEntry) -> None:
    data = _EarlyData("110")
    policy, decision = _parked_then(mode, data, _Broker())
    assert {o.isin for o in decision.orders} == {A, B}  # the full top-2 rebalance
    assert _sides(decision) == {Side.BUY}
    assert not policy.parked
    labels = {item.label for item in decision.evidence.items}
    assert f"early_entry_{mode}" in labels  # the trigger's reading is filed with the decision


@pytest.mark.parametrize(
    ("mode", "quiet"),
    [
        (EarlyEntry.CONFIRMED_CROSS, {"trend": _trend(streak=4)}),
        (EarlyEntry.MOMENTUM_TURN, {"trend": _trend(short="98", short_lagged="99")}),
        (EarlyEntry.BREADTH_TURN, {"breadth_reading": _breadth("0.45", "0.40")}),
    ],
)
def test_a_quiet_trigger_leaves_a_parked_book_parked_inversion(
    mode: EarlyEntry, quiet: dict[str, object]
) -> None:
    data = _EarlyData("110")
    for name, value in quiet.items():
        setattr(data, name, value)
    policy, decision = _parked_then(mode, data, _Broker())
    assert decision.orders == ()
    assert policy.parked


def test_the_momentum_turn_reenters_below_the_ma_and_is_not_re_parked() -> None:
    # Level 99 against an MA of 100: risk-off by D13's reading, yet the turn has fired. The
    # re-entry must not re-read the regime and park the book it is entering.
    data = _EarlyData("99")
    data.trend = _trend(level="99")
    policy, decision = _parked_then(EarlyEntry.MOMENTUM_TURN, data, _Broker(), level="99")
    assert _sides(decision) == {Side.BUY}
    assert not policy.parked


def test_the_breadth_turn_reenters_whatever_the_index_reads() -> None:
    data = _EarlyData("95")
    _, decision = _parked_then(EarlyEntry.BREADTH_TURN, data, _Broker(), level="95")
    assert _sides(decision) == {Side.BUY}


def test_a_trigger_never_acts_on_an_unparked_book() -> None:
    for mode in EarlyEntry:
        data = _EarlyData("110")
        policy = MomentumV2Policy(data, _params(early_entry=mode))
        assert policy.decide(_ctx(MID, _Broker())).orders == ()
        assert data.trend_reads == [] and data.breadth_reads == []


def test_a_parked_spell_reenters_once() -> None:
    data = _EarlyData("110")
    policy, first = _parked_then(EarlyEntry.CONFIRMED_CROSS, data, _Broker())
    assert first.orders
    again = policy.decide(_ctx(MID + timedelta(days=1), _Broker(holdings=_held(A, B))))
    assert again.orders == ()


def test_without_the_switch_d13_waits_for_the_rebalance() -> None:
    data = _EarlyData("110")
    policy = MomentumV2Policy(data, _params())
    data.level = Decimal("90")
    policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A, B, C))))
    data.level = Decimal("110")
    assert policy.decide(_ctx(MID, _Broker())).orders == ()
    assert data.trend_reads == [] and data.breadth_reads == []


def test_the_rebalance_session_is_always_d13s() -> None:
    # A risk-off rebalance parks even when the trend trigger would fire that session.
    data = _EarlyData("90")
    policy = MomentumV2Policy(data, _params(early_entry=EarlyEntry.MOMENTUM_TURN))
    decision = policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A))))
    assert _sides(decision) == {Side.SELL} and policy.parked


# ── the scale-in ─────────────────────────────────────────────────────────────────────────────────


def _bought_value(decision: SessionDecision) -> Decimal:
    return sum(
        (Decimal(o.quantity) * PRICE for o in decision.orders if o.side is Side.BUY), Decimal(0)
    )


def test_the_scale_in_deploys_half_the_book_and_a_full_reentry_all_of_it() -> None:
    cash = Decimal("100000")
    _, half = _parked_then(EarlyEntry.SCALE_IN, _EarlyData("110"), _Broker(cash=str(cash)))
    _, full = _parked_then(EarlyEntry.CONFIRMED_CROSS, _EarlyData("110"), _Broker(cash=str(cash)))
    assert _bought_value(half) <= cash * SCALE_IN_FRACTION
    assert _bought_value(half) > cash * SCALE_IN_FRACTION * Decimal("0.9")
    # Inversion: the cap is a cap on the scale-in only — a full re-entry spends ~98% of cash.
    assert _bought_value(full) > cash * Decimal("0.9")


def test_the_scale_in_waits_while_below_the_ma_inversion() -> None:
    policy, decision = _parked_then(EarlyEntry.SCALE_IN, _EarlyData("99"), _Broker(), level="99")
    assert decision.orders == () and policy.parked


# ── point in time ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode", [EarlyEntry.CONFIRMED_CROSS, EarlyEntry.MOMENTUM_TURN, EarlyEntry.BREADTH_TURN]
)
def test_a_future_dated_trigger_reading_trips_the_guard(mode: EarlyEntry) -> None:
    data = _EarlyData("110", leak=True)
    policy = MomentumV2Policy(data, _params(early_entry=mode))
    policy.resume(None, parked=True)
    with pytest.raises(PitError):
        policy.decide(_ctx(MID, _Broker()))


# ── the breadth series ───────────────────────────────────────────────────────────────────────────

_CAL = tuple(date(2024, 1, 1) + timedelta(days=n) for n in range(8))


def _series(closes: dict[date, dict[str, Decimal]], universe: set[str]) -> BreadthSeries:
    return BreadthSeries(
        calendar=_CAL,
        first=_CAL[4],
        last=_CAL[-1],
        closes=lambda s: closes.get(s, {}),
        universe=lambda _r: frozenset(universe),
        ma_days=3,
        lag=1,
    )


def test_breadth_counts_names_strictly_above_their_own_mean() -> None:
    d = Decimal
    # X rises (above its 3-close mean), Y falls (below), Z is flat (equal: not above).
    closes = {s: {"X": d(10 + i), "Y": d(20 - i), "Z": d(5)} for i, s in enumerate(_CAL)}
    reading = _series(closes, {"X", "Y", "Z"}).reading(_CAL[5])
    assert reading.names_counted == 3
    assert reading.share_above == d(1) / d(3)
    assert reading.knowable_date == _CAL[5]


def test_breadth_ignores_names_outside_the_universe_or_short_of_history() -> None:
    d = Decimal
    closes = {s: {"X": d(10 + i), "Y": d(20 - i)} for i, s in enumerate(_CAL)}
    closes[_CAL[5]]["NEW"] = d(50)  # first print: no 3-close history yet
    reading = _series(closes, {"X", "NEW"}).reading(_CAL[5])  # Y is not in the universe
    assert reading.names_counted == 1
    assert reading.share_above == d(1)


def test_breadth_reads_the_lagged_share_and_never_a_later_close() -> None:
    d = Decimal
    closes = {s: {"X": d(10 + i)} for i, s in enumerate(_CAL)}
    closes[_CAL[6]] = {"X": d(1)}  # a crash after the session asked for must not reach it
    reading = _series(closes, {"X"}).reading(_CAL[5])
    assert reading.share_above == d(1) and reading.share_above_lagged == d(1)
    crashed = _series(closes, {"X"}).reading(_CAL[6])
    assert crashed.share_above == d(0) and crashed.share_above_lagged == d(1)


def test_breadth_refuses_a_session_outside_its_span() -> None:
    series = _series({s: {"X": Decimal(1)} for s in _CAL}, {"X"})
    with pytest.raises(BreadthError):
        series.reading(_CAL[3])


# ── the real stack: replay digests ───────────────────────────────────────────────────────────────


class _ScriptedEarly(_ScriptedData):
    """The M14.5 scripted month plus trend and breadth readings derived from the same script.

    Risk-off at the January rebalance and on again from 10 January (M14.5's script). The momentum
    turn is scripted to fire from 8 January, still below the MA; breadth from 12 January.
    """

    def regime_trend(self, as_of: date) -> Dataset[RegimeTrendReading]:
        streak = 0
        for session in reversed([s for s in _SESSIONS if s <= as_of]):
            if not _risk_on(session):
                break
            streak += 1
        level = Decimal("105") if _risk_on(as_of) else Decimal("99")
        turned = as_of >= date(2024, 1, 8)
        reading = RegimeTrendReading(
            index_level=level,
            moving_average=Decimal("100"),
            sessions_at_or_above=streak,
            short_average=Decimal("100") if turned else Decimal("98"),
            short_average_lagged=Decimal("99"),
            level_lagged=Decimal("97"),
            knowable_date=as_of,
        )
        return Dataset.declaring(f"t@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)

    def breadth(self, as_of: date) -> Dataset[BreadthReading]:
        share = Decimal("0.55") if as_of >= date(2024, 1, 12) else Decimal("0.40")
        reading = BreadthReading(
            share_above=share,
            share_above_lagged=Decimal("0.45"),
            names_counted=100,
            knowable_date=as_of,
        )
        return Dataset.declaring(f"b@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


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
        policy=MomentumV2Policy(_ScriptedEarly(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


def _buy_dates(result: ReplayResult) -> list[date]:
    return sorted({e.trading_date for e in result.journal if e.decision is Decision.BUY})


def test_switch_off_reproduces_d13_byte_for_byte_on_a_source_serving_every_seam() -> None:
    # The digest was struck before M14.5; the source here also serves trend and breadth, which
    # D13 must never read.
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M14_5
    assert _replay(replace(D13, early_entry=None)).digest() == _D13_DIGEST_BEFORE_M14_5


@pytest.mark.parametrize(
    ("params", "first_buy"),
    [
        (D13, date(2024, 2, 1)),  # D13: the February rebalance
        (D13_EARLY_SCALE_IN, date(2024, 1, 10)),  # first close at or above the MA
        (D13_EARLY_CONFIRMED_CROSS, date(2024, 1, 16)),  # 10, 11, 12, 15, 16 January
        (D13_EARLY_MOMENTUM_TURN, date(2024, 1, 8)),  # below the MA, trend turned
        (D13_EARLY_BREADTH_TURN, date(2024, 1, 12)),
    ],
)
def test_each_variant_reenters_on_the_session_its_rule_says(
    params: MomentumV2Parameters, first_buy: date
) -> None:
    result = _replay(params)
    assert _buy_dates(result)[0] == first_buy
    if params is not D13:
        assert result.digest() != _D13_DIGEST_BEFORE_M14_5
    # Exits stay monthly: the mid-February breakdown is never acted on by any variant.
    assert not [e for e in result.journal if e.decision is Decision.SELL]


def test_the_scale_in_completes_the_basket_at_the_next_risk_on_rebalance() -> None:
    result = _replay(D13_EARLY_SCALE_IN)
    buys = _buy_dates(result)
    february = min(s for s in _REBALANCES if s.month == 2)
    assert february in buys  # the rest deployed at the next monthly rebalance
    assert [b for b in buys if b < february]  # the half-entry happened in January
    # A full early re-entry leaves the February rebalance little to buy; the scale-in leaves it
    # half the book. Inversion: if the cap were not applied, February would buy as little.
    full = _replay(D13_EARLY_CONFIRMED_CROSS)

    def february_buys(r: ReplayResult) -> int:
        return sum(
            1 for e in r.journal if e.decision is Decision.BUY and e.trading_date == february
        )

    assert february_buys(result) > february_buys(full)
