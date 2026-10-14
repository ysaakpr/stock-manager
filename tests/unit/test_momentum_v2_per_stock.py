"""M14.6 — momentum v2's per-stock entry and review switches (all off by default).

D13 rebalances the whole book on the first session of each month. The M14.6 switches ask whether
that cadence costs the book exposure: ``daily_slot_fill`` (V1) refills free slots every session,
``review_sessions`` (V2) gives every holding its own 21-session review clock, ``daily_regime_gate``
(V3) reads the regime every session to block entries, ``daily_regime_park`` (V3b) also parks the
book on risk-off, and V4 is V2 with no regime filter. Each timing test here has an inverted twin
or an exact-session assertion that fails if the rule is flipped; the PIT tests prove the daily
ranking and the daily regime go through the guard, and that the L1 daily ranking reads no close
after its session; the replay test pins that the defaults reproduce D13 byte-for-byte (the digest
was struck with the pre-M14.6 policy) and the parameter tests that D13's fingerprint did not move.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.momentum_v2 import (
    D13_DAILY_SLOT_FILL,
    D13_PER_STOCK_CLOCK,
    D13_PER_STOCK_DAILY_GATE,
    D13_PER_STOCK_DAILY_PARK,
    D13_PER_STOCK_NO_REGIME,
    PAPER_RATIFIED_2026_09_06,
    PER_STOCK_REVIEW_SESSIONS,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult
from backtest.run import _L1MomentumV2Data
from backtest.signal_panel import (
    SignalPanel,
    SignalPanelError,
    panel_key,
    signal_panel_in_force,
    write_month,
    write_panel_manifest,
)
from dataplatform.clock import FrozenClock
from dataplatform.identity.master import ListingStatus
from dataplatform.query.pit import Dataset, PitError
from dataplatform.query.universe import ListingWindow
from execution.broker import Exchange, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import PRICE, RAILS
from tests.unit.test_momentum_v2_daily_regime import _Broker, _ctx, _held

D13 = PAPER_RATIFIED_2026_09_06

#: Forty names, so D13's top-20 / band-30 has names outside the band to sell.
NAMES = tuple(f"INE{index:03d}B01010" for index in range(1, 41))
SESSIONS = tuple(
    day for day in (date(2024, 1, 1) + timedelta(days=n) for n in range(182)) if day.weekday() < 5
)
REBALANCES = frozenset(
    min(s for s in SESSIONS if (s.year, s.month) == key)
    for key in {(s.year, s.month) for s in SESSIONS}
)
OPENING = Decimal("1000000")


# ── a scripted world: a ranking that rotates every week, a scripted regime ───────────────────────


def _momentum(index: int, session: date, *, rotate: bool = True) -> Decimal:
    if not rotate:
        return Decimal(index) / 40  # a fixed order: the highest index ranks first
    week = (session - SESSIONS[0]).days // 7
    return Decimal((index * 7 + week * 3) % 40) / 40


class _World:
    """Forty names whose 12-1 order rotates weekly (or stays fixed); risk-on unless in ``off``."""

    def __init__(
        self,
        off: frozenset[date] = frozenset(),
        *,
        momentum: dict[str, dict[date, Decimal]] | None = None,
        rotate: bool = True,
        leak_signal: bool = False,
        leak_regime: bool = False,
    ) -> None:
        self._off = off
        self._momentum = momentum
        self._rotate = rotate
        self._leak_signal = leak_signal
        self._leak_regime = leak_regime
        self.regime_reads: list[date] = []
        self.signal_reads: list[date] = []

    def is_rebalance(self, session: date) -> bool:
        return session in REBALANCES

    def _value(self, index: int, isin: str, session: date) -> Decimal:
        if self._momentum is not None and isin in self._momentum:
            return self._momentum[isin][session]
        return _momentum(index, session, rotate=self._rotate)

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        self.signal_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak_signal else as_of
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=self._value(index, isin, as_of),
                momentum_12_1=self._value(index, isin, as_of),
                price=PRICE,
                volatility=Decimal("0.1") + Decimal(index % 5) / 100,
                knowable_date=knowable,
            )
            for index, isin in enumerate(NAMES)
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        self.regime_reads.append(as_of)
        level = Decimal("95") if as_of in self._off else Decimal("105")
        knowable = as_of + timedelta(days=1) if self._leak_regime else as_of
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=knowable
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


class _Market:
    """Every one of the forty names prints at PRICE on every session, deep enough to fill."""

    def __init__(self, sessions: tuple[date, ...]) -> None:
        self._calendar = (*sessions, date(2024, 12, 31))

    def next_session(self, after: date) -> date:
        return next(session for session in self._calendar if session > after)

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if isin not in NAMES:
            raise NoReferenceBarError(isin)
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=PRICE,
            vwap=PRICE,
            traded_value=Decimal("100000000000"),
        )


def _replay(params: MomentumV2Parameters, world: _World | None = None) -> ReplayResult:
    from backtest.run import _AccountingBroker

    clock = FrozenClock(SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(SESSIONS),
        opening_cash=OPENING,
    )
    book = PortfolioBook()
    book.deposit(SESSIONS[0], OPENING)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=SectorMap(source="test", sha256="test", by_isin={i: i[3:6] for i in NAMES}),
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in SESSIONS}
    return ReplayEngine(
        policy=MomentumV2Policy(world or _World(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


def _dates(result: ReplayResult, side: Decision, isin: str | None = None) -> list[date]:
    return sorted(
        {
            e.trading_date
            for e in result.journal
            if e.decision is side and (isin is None or e.isin == isin)
        }
    )


def _small(**overrides: object) -> MomentumV2Parameters:
    """D13's toggles on a book small enough to read: top-10, band 14 (above A8's 8-name floor)."""
    return replace(D13, top_n=10, sell_band=14, **overrides)  # type: ignore[arg-type]


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_the_per_stock_switches_are_off_by_default_and_d13_leaves_them_off() -> None:
    params = MomentumV2Parameters()
    assert not params.daily_slot_fill and params.review_sessions is None
    assert not params.daily_regime_gate and not params.daily_regime_park
    assert not D13.daily_slot_fill and D13.review_sessions is None


def test_d13s_repr_is_unchanged_so_its_run_fingerprint_is_unchanged() -> None:
    assert repr(D13) == (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )


def test_the_variants_are_d13_plus_their_switches_and_never_share_a_fingerprint() -> None:
    variants = (
        D13_DAILY_SLOT_FILL,
        D13_PER_STOCK_CLOCK,
        D13_PER_STOCK_DAILY_GATE,
        D13_PER_STOCK_DAILY_PARK,
        D13_PER_STOCK_NO_REGIME,
    )
    assert len({repr(D13), *map(repr, variants)}) == 6
    assert repr(D13_DAILY_SLOT_FILL) == repr(D13).removesuffix(")") + ", daily_slot_fill=True)"
    assert D13_PER_STOCK_CLOCK.review_sessions == PER_STOCK_REVIEW_SESSIONS == 21
    assert D13_PER_STOCK_DAILY_PARK.daily_regime_gate
    assert not D13_PER_STOCK_NO_REGIME.regime_filter
    for variant in variants:  # every D13 toggle except what the variant names is D13's
        assert (variant.top_n, variant.sell_band, variant.use_12_1) == (20, 30, True)
        assert variant.vol_scaled and variant.redeploy_next_session


def test_the_per_stock_switches_are_validated() -> None:
    with pytest.raises(ValueError, match="daily_slot_fill"):
        replace(D13, review_sessions=21)
    with pytest.raises(ValueError, match="positive"):
        replace(D13_DAILY_SLOT_FILL, review_sessions=0)
    with pytest.raises(ValueError, match="per-stock book"):
        replace(D13_DAILY_SLOT_FILL, daily_regime_gate=True)
    with pytest.raises(ValueError, match="per-stock book"):
        replace(D13_PER_STOCK_NO_REGIME, daily_regime_gate=True)
    with pytest.raises(ValueError, match="daily_regime_gate"):
        replace(D13_PER_STOCK_CLOCK, daily_regime_park=True)
    with pytest.raises(ValueError, match="regime_daily_reentry"):
        replace(D13_DAILY_SLOT_FILL, regime_daily_reentry=True)
    with pytest.raises(ValueError, match="vol_target_annual"):
        replace(D13_DAILY_SLOT_FILL, vol_target_annual=Decimal("0.15"))


def test_a_per_stock_book_refuses_resume_rather_than_drop_its_clocks() -> None:
    policy = MomentumV2Policy(_World(), D13_PER_STOCK_CLOCK)
    with pytest.raises(NotImplementedError, match="research-only"):
        policy.resume(None)


# ── D13 unchanged ────────────────────────────────────────────────────────────────────────────────

#: The digest of :func:`_replay` over D13, struck with the policy as it stood before M14.6
#: (origin/main e84649c). Journal, book and rails byte-for-byte.
_D13_DIGEST_BEFORE_M14_6 = "70ca24ce33ac37464febea415f9ad410c67e1395981e20e59d2c20546624fa1e"


def test_defaults_off_reproduces_d13_byte_for_byte() -> None:
    explicit_off = replace(
        D13,
        daily_slot_fill=False,
        review_sessions=None,
        daily_regime_gate=False,
        daily_regime_park=False,
    )
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M14_6
    assert _replay(explicit_off).digest() == _D13_DIGEST_BEFORE_M14_6


# ── V1: daily slot fill ──────────────────────────────────────────────────────────────────────────

MID = date(2024, 1, 10)  # not a rebalance session


def _bought(decision: object) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.BUY}  # type: ignore[attr-defined]


def test_v1_fills_free_slots_on_a_non_rebalance_session_with_the_best_unheld_names() -> None:
    world = _World()
    ranked = sorted(world.signal(MID).records, key=lambda r: (-r.momentum_12_1, r.isin))
    top = [r.isin for r in ranked[:10]]
    broker = _Broker(cash="1000000", holdings=_held(top[0]))
    decision = MomentumV2Policy(world, _small(daily_slot_fill=True)).decide(_ctx(MID, broker))
    assert _bought(decision) == set(top[1:10])  # nine free slots, ranks 2-10; never rank 11+
    assert all(o.side is Side.BUY for o in decision.orders)  # V1 never sells off a rebalance


def test_without_v1_the_same_session_is_a_heartbeat_inversion() -> None:
    world = _World()
    broker = _Broker(cash="400000", holdings=_held(NAMES[0]))
    decision = MomentumV2Policy(world, _small()).decide(_ctx(MID, broker))
    assert decision.orders == ()


def test_v1_does_not_fill_a_full_book() -> None:
    world = _World()
    broker = _Broker(cash="400000", holdings=_held(*NAMES[30:40]))  # ten names, top_n=10
    decision = MomentumV2Policy(world, _small(daily_slot_fill=True)).decide(_ctx(MID, broker))
    assert decision.orders == ()


def test_v1_does_not_fill_while_the_monthly_regime_has_the_book_parked() -> None:
    off = frozenset({SESSIONS[0]})  # risk-off at the January rebalance only
    result = _replay(replace(D13_DAILY_SLOT_FILL), _World(off))
    buys = _dates(result, Decision.BUY)
    assert buys and buys[0] == date(2024, 2, 1)  # parked until the February rebalance
    on = _replay(D13_DAILY_SLOT_FILL)
    assert _dates(on, Decision.BUY)[0] == SESSIONS[0]


# ── V2: the per-stock review clock ───────────────────────────────────────────────────────────────

LEADER = NAMES[39]


def _leader_then_laggard(drop_after: int) -> dict[str, dict[date, Decimal]]:
    """LEADER ranks first for ``drop_after`` sessions, then last for the rest of the replay."""
    return {
        LEADER: {
            s: (Decimal("9") if n < drop_after else Decimal("-9")) for n, s in enumerate(SESSIONS)
        }
    }


def test_v2_reviews_a_holding_exactly_review_sessions_after_its_entry() -> None:
    world = _World(rotate=False, momentum=_leader_then_laggard(3))
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), world)
    assert _dates(result, Decision.BUY, LEADER)[0] == SESSIONS[0]
    # Ranked last from the fourth session on, but held until its first review, 21 sessions after
    # the session it was bought on — not sold the day it left the band, nor at the next month.
    assert _dates(result, Decision.SELL, LEADER) == [SESSIONS[PER_STOCK_REVIEW_SESSIONS]]


def test_d13_sells_the_same_name_at_the_next_monthly_rebalance_inversion() -> None:
    world = _World(rotate=False, momentum=_leader_then_laggard(3))
    result = _replay(_small(), world)
    assert _dates(result, Decision.SELL, LEADER)[0] == date(2024, 2, 1)
    assert SESSIONS[PER_STOCK_REVIEW_SESSIONS] != date(2024, 2, 1)


def test_v2_keeps_a_name_still_in_band_at_review_and_restarts_its_clock() -> None:
    # In band through the first review, out of it after: sold at the *second* review.
    world = _World(rotate=False, momentum=_leader_then_laggard(PER_STOCK_REVIEW_SESSIONS + 2))
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), world)
    assert _dates(result, Decision.SELL, LEADER) == [SESSIONS[2 * PER_STOCK_REVIEW_SESSIONS]]


def test_v2_refills_a_freed_slot_on_a_non_rebalance_session() -> None:
    world = _World(rotate=False, momentum=_leader_then_laggard(3))
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), world)
    sold = SESSIONS[PER_STOCK_REVIEW_SESSIONS]
    later = [d for d in _dates(result, Decision.BUY) if d > sold]
    assert later and later[0] not in REBALANCES
    assert later[0] <= SESSIONS[PER_STOCK_REVIEW_SESSIONS + 3]  # proceeds settle T+1/T+2


def test_v2_has_no_book_wide_rebalance() -> None:
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14))
    # Every sale is a review: the clock of every name sold ran at least 21 sessions.
    index = {s: n for n, s in enumerate(SESSIONS)}
    bought: dict[str, int] = {}
    for entry in result.journal:
        if entry.isin is None:
            continue
        if entry.decision is Decision.BUY:
            bought.setdefault(entry.isin, index[entry.trading_date])
        elif entry.decision is Decision.SELL:
            assert index[entry.trading_date] - bought.pop(entry.isin) >= 21
    assert _dates(result, Decision.SELL)  # the rotating ranking does force reviews to sell


def test_v2_reads_the_regime_monthly_and_parks_like_d13() -> None:
    off = frozenset(s for s in SESSIONS if date(2024, 1, 15) <= s < date(2024, 3, 1))
    world = _World(off)
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), world)
    assert set(world.regime_reads) == set(REBALANCES)  # never read mid-month
    parks = {e.trading_date for e in result.journal if e.decision is Decision.SELL}
    assert date(2024, 2, 1) in parks  # the February rebalance reads risk-off and parks
    assert not [d for d in _dates(result, Decision.BUY) if date(2024, 2, 1) <= d < date(2024, 3, 1)]


# ── V3 / V3b: the daily regime gate ──────────────────────────────────────────────────────────────


#: Risk-off over LEADER's first review (SESSIONS[21], 30 January) but off every month's first
#: session, so the monthly reading V2 takes never sees it.
OFF_LATE_JAN = frozenset(s for s in SESSIONS if date(2024, 1, 22) <= s < date(2024, 2, 1))


def test_v3_blocks_entries_on_risk_off_sessions_and_sells_nothing_for_the_regime() -> None:
    world = _World(OFF_LATE_JAN, rotate=False, momentum=_leader_then_laggard(3))
    result = _replay(replace(D13_PER_STOCK_DAILY_GATE, top_n=10, sell_band=14), world)
    assert world.regime_reads == list(SESSIONS)  # read every session
    assert not [d for d in _dates(result, Decision.BUY) if d in OFF_LATE_JAN]
    # The review still runs under risk-off: LEADER is sold on its clock, nothing else is.
    assert _dates(result, Decision.SELL) == [SESSIONS[PER_STOCK_REVIEW_SESSIONS]]
    assert not [e for e in result.journal if e.sleeve is D13.parking_sleeve]  # never parks
    assert min(d for d in _dates(result, Decision.BUY) if d > SESSIONS[0]) == date(2024, 2, 1)


def test_v2_on_the_same_world_refills_inside_the_risk_off_week_inversion() -> None:
    world = _World(OFF_LATE_JAN, rotate=False, momentum=_leader_then_laggard(3))
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), world)
    assert [d for d in _dates(result, Decision.BUY) if d in OFF_LATE_JAN]


OFF_MID_JAN = frozenset(s for s in SESSIONS if date(2024, 1, 15) <= s < date(2024, 1, 29))


def test_v3b_parks_on_the_first_risk_off_session_and_re_enters_when_it_lifts() -> None:
    world = _World(OFF_MID_JAN)
    result = _replay(replace(D13_PER_STOCK_DAILY_PARK, top_n=10, sell_band=14), world)
    park_sells = sorted(
        {
            e.trading_date
            for e in result.journal
            if e.decision is Decision.SELL and e.sleeve is D13.parking_sleeve
        }
    )
    assert park_sells == [date(2024, 1, 15)]
    assert date(2024, 1, 29) in _dates(result, Decision.BUY)


# ── V4: no regime ────────────────────────────────────────────────────────────────────────────────


def test_v4_never_reads_the_regime_and_enters_on_risk_off_sessions() -> None:
    off = frozenset(SESSIONS)  # risk-off throughout
    world = _World(off)
    result = _replay(replace(D13_PER_STOCK_NO_REGIME, top_n=10, sell_band=14), world)
    assert world.regime_reads == []
    assert _dates(result, Decision.BUY)[0] == SESSIONS[0]


def test_v2_in_the_same_risk_off_world_never_buys_inversion() -> None:
    off = frozenset(SESSIONS)
    result = _replay(replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14), _World(off))
    assert _dates(result, Decision.BUY) == []


# ── no look-ahead ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params",
    [
        _small(daily_slot_fill=True),
        replace(D13_PER_STOCK_CLOCK, top_n=10, sell_band=14),
        replace(D13_PER_STOCK_NO_REGIME, top_n=10, sell_band=14),
    ],
)
def test_the_daily_ranking_goes_through_the_pit_guard(params: MomentumV2Parameters) -> None:
    broker = _Broker(cash="400000", holdings=_held(NAMES[0]))
    policy = MomentumV2Policy(_World(leak_signal=True), params)
    with pytest.raises(PitError):
        policy.decide(_ctx(MID, broker))


def test_the_daily_regime_goes_through_the_pit_guard() -> None:
    broker = _Broker(cash="400000")
    policy = MomentumV2Policy(
        _World(leak_regime=True), replace(D13_PER_STOCK_DAILY_GATE, top_n=10, sell_band=14)
    )
    with pytest.raises(PitError):
        policy.decide(_ctx(MID, broker))


class _RecordingReader:
    """The slice of ``_L1Reader`` the L1 daily ranking reads: raw closes per session."""

    def __init__(self, closes: dict[date, dict[str, Decimal]]) -> None:
        self._closes = closes
        self.asked: list[date] = []

    def closes_on(self, session: date) -> dict[str, Decimal]:
        self.asked.append(session)
        return self._closes.get(session, {})


def _l1_source(closes: dict[date, dict[str, Decimal]], calendar: list[date]) -> _L1MomentumV2Data:
    source = _L1MomentumV2Data.__new__(_L1MomentumV2Data)
    reader = _RecordingReader(closes)
    source._reader = reader  # type: ignore[assignment]
    source._signal_closes = reader.closes_on
    source._universe_filter = None
    source._sessions = calendar
    source._windows = tuple(
        ListingWindow(
            isin=i, listed_from=calendar[0], delisted_on=None, status=ListingStatus.ACTIVE
        )
        for i in NAMES[:5]
    )
    return source


def test_the_l1_daily_ranking_reads_no_close_after_its_session() -> None:
    calendar = [date(2023, 1, 2) + timedelta(days=n) for n in range(600)]
    closes = {
        s: {i: Decimal(100 + n + k) for k, i in enumerate(NAMES[:5])}
        for n, s in enumerate(calendar)
    }
    as_of = calendar[450]  # a mid-month day, not a rebalance
    source = _l1_source(closes, calendar)
    before = source.records_on(as_of)
    assert before and all(r.knowable_date == as_of for r in before)
    assert max(source._reader.asked) <= as_of  # type: ignore[attr-defined]
    # Rewrite every close after the session: the ranking of the session must not move.
    future = {
        s: ({i: c * 3 for i, c in day.items()} if s > as_of else day) for s, day in closes.items()
    }
    after = _l1_source(future, calendar).records_on(as_of)
    assert sorted(before, key=lambda r: r.isin) == sorted(after, key=lambda r: r.isin)


# ── the signal panel ─────────────────────────────────────────────────────────────────────────────


def _panel(tmp_path: Path, records: dict[date, tuple[MomentumV2Record, ...]], key: str) -> Path:
    write_month(tmp_path, "series", "2024-01", records)
    write_panel_manifest(
        tmp_path, {"first": "2024-01-01", "last": "2024-01-31", "keys": {key: "series"}}
    )
    return tmp_path


def test_the_panel_serves_exactly_the_records_it_was_cut_from(tmp_path: Path) -> None:
    session = date(2024, 1, 10)
    records = tuple(_World().signal(session).records)
    panel = SignalPanel(_panel(tmp_path, {session: records}, "k"))
    served = panel.lookup("k", session)
    assert served is not None
    assert sorted(served, key=lambda r: r.isin) == sorted(records, key=lambda r: r.isin)
    assert panel.lookup("other", session) is None  # another universe never reads it
    assert panel.lookup("k", date(2024, 2, 1)) is None  # outside its span


def test_the_panel_refuses_a_record_knowable_after_its_session(tmp_path: Path) -> None:
    session = date(2024, 1, 10)
    leaked = tuple(_World(leak_signal=True).signal(session).records)
    with pytest.raises(SignalPanelError, match="knowable"):
        write_month(tmp_path, "series", "2024-01", {session: leaked})


def test_an_l1_source_reads_the_panel_in_force_only_for_its_own_key(tmp_path: Path) -> None:
    calendar = [date(2023, 1, 2) + timedelta(days=n) for n in range(420)]
    session = calendar[-1]
    closes = {s: {i: Decimal(100 + n) for i in NAMES[:5]} for n, s in enumerate(calendar)}
    canned = tuple(
        MomentumV2Record(i, Decimal("7"), Decimal("7"), PRICE, Decimal("0.2"), session)
        for i in NAMES[:2]
    )
    key = panel_key("None", adjusted=False, l1_isins_only=False, data_root="(configured)")
    write_month(tmp_path, "series", session.strftime("%Y-%m"), {session: canned})
    write_panel_manifest(
        tmp_path,
        {"first": session.isoformat(), "last": session.isoformat(), "keys": {key: "series"}},
    )
    with signal_panel_in_force(SignalPanel(tmp_path)):
        source = _l1_source(closes, calendar)
        source._panel = SignalPanel(tmp_path)
        source._panel_key = key
        assert {r.isin for r in source._records(session)} == set(NAMES[:2])
        source._panel_key = None  # a scripted close source never matches a lake panel
        assert {r.isin for r in source._records(session)} == set(NAMES[:5])
