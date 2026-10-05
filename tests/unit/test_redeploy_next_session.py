"""Redeploy-on-settlement for the swing family and naive momentum (X2, idle-cash fix).

A rule exit on rebalance R used to leave its proceeds idle until the *next* rebalance — R+10 for
the swing composite, a month for naive momentum. With ``redeploy_next_session`` on, the policy
keeps R's target and deploys settled cash into it once the proceeds have settled: R+1 under T+1,
R+2 under T+2. These tests walk the real stack a driver uses — ``ReplayEngine`` -> ``RailGate`` ->
``SimBroker`` with the one shared cost model and the dated settlement cycle — and each fails if the
mechanic is missing, fires early, or outlives the next rebalance:

* an exit on R is followed by buys once its proceeds settle, and before R+10;
* nothing is bought while proceeds are in settlement (T+2: nothing on R+1);
* a later rebalance supersedes the pending target;
* the decision's data reads are exactly those of the off arm (no new signal, no future data);
* property: on a high-turnover synthetic stream the mean between-rebalance cash share stays under
  a bound the off arm exceeds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from analyst.journal.models import Decision, JournalEntry
from backtest.policies.momentum_v2 import RegimeReading
from backtest.policies.naive_momentum import (
    MomentumParameters,
    MomentumRecord,
    NaiveMomentumPolicy,
)
from backtest.policies.swing_composite import (
    SwingCompositeParameters,
    SwingCompositePolicy,
    SwingRecord,
)
from backtest.replay import Policy, ReplayEngine, ReplayResult, SessionContext, SessionDecision
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, Position
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import mechanics_gate

ISINS = ("INE001A01036", "INE002A01018", "INE003A01016", "INE004A01014")
A, B, C, D = ISINS
_CASH = Decimal("1000000")
_PRICE = Decimal("100")
_ONE = Decimal("1")
_ZERO = Decimal("0")


def _weekdays(start: date, n: int) -> tuple[date, ...]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


#: T+1 (2024) and T+2 (2019) eras, 30 sessions each; rebalances every 10th session from the first.
T1 = _weekdays(date(2024, 2, 5), 30)
T2 = _weekdays(date(2019, 2, 4), 30)


class _Market:
    """A flat ``SessionMarket``: every name opens at ``_PRICE`` on every session of the calendar."""

    def __init__(self, sessions: tuple[date, ...], isins: Sequence[str]) -> None:
        self._calendar = (*sessions, *_weekdays(sessions[-1] + timedelta(days=1), 10))
        self._isins = set(isins)

    def next_session(self, after: date) -> date:
        for session in self._calendar:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if isin not in self._isins:
            raise NoReferenceBarError(f"{isin} {session}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=_PRICE,
            vwap=_PRICE,
            traded_value=Decimal("10000000000"),
        )


class _Recorder:
    """Wraps a policy: records each session's cash share and the data reads it made."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self.cash_share: dict[date, Decimal] = {}

    def decide(self, ctx: SessionContext) -> SessionDecision:
        margins = ctx.broker.margins()
        lots: list[Holding | Position] = [*ctx.broker.holdings(), *ctx.broker.positions()]
        invested = sum((Decimal(lot.quantity) * _PRICE for lot in lots), _ZERO)
        nav = margins.cash_value + invested
        self.cash_share[ctx.session] = margins.cash_value / nav
        return self._policy.decide(ctx)


def _replay(
    policy: Policy, sessions: tuple[date, ...], isins: Sequence[str] = ISINS
) -> tuple[ReplayResult, _Recorder]:
    clock = FrozenClock(sessions[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(sessions, isins),
        opening_cash=_CASH,
    )
    closes = {(isin, s): _PRICE for isin in isins for s in sessions}
    recorder = _Recorder(policy)
    result = ReplayEngine(
        policy=recorder,
        broker=sim,
        clock=clock,
        sessions=sessions,
        rails=mechanics_gate(closes),
    ).run()
    return result, recorder


def _buys_on(result: ReplayResult, day: date) -> list[JournalEntry]:
    return [e for e in result.journal if e.trading_date == day and e.decision is Decision.BUY]


def _sells_on(result: ReplayResult, day: date) -> list[JournalEntry]:
    return [e for e in result.journal if e.trading_date == day and e.decision is Decision.SELL]


# ── the swing composite ─────────────────────────────────────────────────────────────────────────


class _SwingData:
    """Ranks by ``high_proximity`` from a per-rebalance score table; records every read."""

    def __init__(
        self,
        sessions: tuple[date, ...],
        scores: Mapping[date, Mapping[str, Decimal]],
        rebalances: set[date] | None = None,
    ) -> None:
        self._sessions = sessions
        self._scores = scores
        self._rebalances = rebalances if rebalances is not None else set(scores)
        self.reads: list[tuple[str, date]] = []

    def is_rebalance(self, session: date) -> bool:
        self.reads.append(("is_rebalance", session))
        return session in self._rebalances

    def _scores_for(self, as_of: date) -> Mapping[str, Decimal]:
        known = [d for d in sorted(self._scores) if d <= as_of]
        return self._scores[known[-1]]

    def signal(self, as_of: date) -> Dataset[SwingRecord]:
        self.reads.append(("signal", as_of))
        records = tuple(
            SwingRecord(
                isin=isin,
                high_proximity=score,
                delivery_share=Decimal("0.5"),
                momentum_12_1=_ZERO,
                volatility=Decimal("0.02"),
                price=_PRICE,
                knowable_date=as_of,
            )
            for isin, score in sorted(self._scores_for(as_of).items())
        )
        return Dataset.declaring("swing", records, knowable_date=lambda r: r.knowable_date)

    def marks(self, as_of: date) -> Dataset[SwingRecord]:
        self.reads.append(("marks", as_of))
        records = tuple(
            SwingRecord(
                isin=isin,
                high_proximity=_ONE,
                delivery_share=_ZERO,
                momentum_12_1=_ZERO,
                volatility=_ZERO,
                price=_PRICE,
                knowable_date=as_of,
            )
            for isin in sorted(self._scores_for(as_of))
        )
        return Dataset.declaring("marks", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        self.reads.append(("regime", as_of))
        reading = RegimeReading(
            index_level=Decimal("110"), moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring("regime", (reading,), knowable_date=lambda r: r.knowable_date)


def _swing_params(
    *, redeploy: bool, top_n: int = 2, regime: bool = False
) -> SwingCompositeParameters:
    return SwingCompositeParameters(
        top_n=top_n,
        sell_band=top_n,
        weight_delivery=_ZERO,
        weight_momentum=_ZERO,
        trailing_stop=None,
        min_hold_sessions=0,
        exclude_vol_fraction=_ZERO,
        regime_filter=regime,
        redeploy_next_session=redeploy,
    )


def _rotation(sessions: tuple[date, ...]) -> dict[date, dict[str, Decimal]]:
    """R0 holds A, B; R1 (session 10) exits B for C, keeping A; R2 (session 20) changes nothing.

    Half the book turns over, not all of it: the mechanics rails keep a one-holding floor, and a
    two-name book sold out entirely would be a test of that rail rather than of redeployment.
    """
    first = {A: Decimal("4"), B: Decimal("3"), C: Decimal("2"), D: Decimal("1")}
    second = {A: Decimal("3"), B: Decimal("1"), C: Decimal("4"), D: Decimal("2")}
    return {sessions[0]: first, sessions[10]: second, sessions[20]: second}


def _swing_run(
    sessions: tuple[date, ...], *, redeploy: bool, rebalances: set[date] | None = None
) -> tuple[ReplayResult, _Recorder, _SwingData]:
    data = _SwingData(sessions, _rotation(sessions), rebalances)
    policy = SwingCompositePolicy(data, _swing_params(redeploy=redeploy))
    result, recorder = _replay(policy, sessions)
    return result, recorder, data


def test_swing_without_redeploy_the_proceeds_wait_for_the_next_rebalance_inversion() -> None:
    """The defect this fixes: R1's exit proceeds sit idle until R2 (R1+10)."""
    result, recorder, _ = _swing_run(T1, redeploy=False)
    r1 = 10
    assert {e.isin for e in _sells_on(result, T1[r1])} == {B}
    for k in range(1, 10):
        assert not _buys_on(result, T1[r1 + k])
    assert recorder.cash_share[T1[r1 + 5]] > Decimal("0.4")


def test_swing_t1_exit_on_r_buys_on_r_plus_1_once_proceeds_settle() -> None:
    result, recorder, _ = _swing_run(T1, redeploy=True)
    r1 = 10
    assert {e.isin for e in _sells_on(result, T1[r1])} == {B}
    bought = _buys_on(result, T1[r1 + 1])
    assert C in {e.isin for e in bought}
    assert all("redeploy settled proceeds" in (e.rationale or "") for e in bought)
    # Deployed, then left alone: no buy on any later session before the next rebalance.
    for k in range(2, 10):
        assert not _buys_on(result, T1[r1 + k])
    assert recorder.cash_share[T1[r1 + 5]] < Decimal("0.05")


def test_swing_t2_nothing_deploys_before_settlement_then_buys_on_r_plus_2() -> None:
    result, recorder, _ = _swing_run(T2, redeploy=True)
    r1 = 10
    assert {e.isin for e in _sells_on(result, T2[r1])} == {B}
    assert not _buys_on(result, T2[r1 + 1]), "a T+2 sale's proceeds are not spendable on R+1"
    assert C in {e.isin for e in _buys_on(result, T2[r1 + 2])}
    assert recorder.cash_share[T2[r1 + 5]] < Decimal("0.05")


def test_swing_a_later_rebalance_supersedes_the_pending_target() -> None:
    """R1 sells under T+2; R1+1 is itself a rebalance with nothing to sell, so nothing pends."""
    rebalances = {T2[0], T2[10], T2[11], T2[20]}
    result, _, _ = _swing_run(T2, redeploy=True, rebalances=rebalances)
    assert {e.isin for e in _sells_on(result, T2[10])} == {B}
    for k in range(12, 20):
        assert not _buys_on(result, T2[k]), f"R1's target outlived the R1+1 rebalance ({T2[k]})"


def test_swing_redeploy_reads_exactly_what_the_off_arm_reads() -> None:
    """No new signal computation on a deployment session, and nothing dated past the session."""
    _, _, off = _swing_run(T2, redeploy=False)
    _, _, on = _swing_run(T2, redeploy=True)
    assert on.reads == off.reads
    signal_days = {day for kind, day in on.reads if kind == "signal"}
    assert signal_days == {T2[0], T2[10], T2[20]}


def test_swing_redeploy_with_the_regime_gate_reads_no_extra_regime() -> None:
    def run(redeploy: bool) -> list[tuple[str, date]]:
        data = _SwingData(T1, _rotation(T1))
        _replay(SwingCompositePolicy(data, _swing_params(redeploy=redeploy, regime=True)), T1)
        return data.reads

    assert run(True) == run(False)


def test_swing_redeploy_off_is_absent_from_the_repr_and_on_is_present() -> None:
    assert "redeploy" not in repr(SwingCompositeParameters())
    assert "redeploy_next_session=True" in repr(
        SwingCompositeParameters(redeploy_next_session=True)
    )


# ── naive momentum ──────────────────────────────────────────────────────────────────────────────


class _NaiveData:
    def __init__(
        self,
        scores: Mapping[date, Mapping[str, Decimal]],
        rebalances: set[date] | None = None,
    ) -> None:
        self._scores = scores
        self._rebalances = rebalances if rebalances is not None else set(scores)
        self.reads: list[tuple[str, date]] = []

    def is_rebalance(self, session: date) -> bool:
        self.reads.append(("is_rebalance", session))
        return session in self._rebalances

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        self.reads.append(("signal", as_of))
        known = [d for d in sorted(self._scores) if d <= as_of]
        records = tuple(
            MomentumRecord(isin=isin, momentum=score, price=_PRICE, knowable_date=as_of)
            for isin, score in sorted(self._scores[known[-1]].items())
        )
        return Dataset.declaring("naive", records, knowable_date=lambda r: r.knowable_date)


def _naive_run(
    sessions: tuple[date, ...], *, redeploy: bool, rebalances: set[date] | None = None
) -> tuple[ReplayResult, _Recorder, _NaiveData]:
    data = _NaiveData(_rotation(sessions), rebalances)
    policy = NaiveMomentumPolicy(data, MomentumParameters(top_n=2, redeploy_next_session=redeploy))
    result, recorder = _replay(policy, sessions)
    return result, recorder, data


def test_naive_without_redeploy_the_proceeds_wait_for_the_next_rebalance_inversion() -> None:
    result, recorder, _ = _naive_run(T1, redeploy=False)
    assert {e.isin for e in _sells_on(result, T1[10])} == {B}
    for k in range(11, 20):
        assert not _buys_on(result, T1[k])
    assert recorder.cash_share[T1[15]] > Decimal("0.4")


def test_naive_t1_exit_on_r_buys_on_r_plus_1() -> None:
    result, recorder, _ = _naive_run(T1, redeploy=True)
    assert {e.isin for e in _sells_on(result, T1[10])} == {B}
    assert C in {e.isin for e in _buys_on(result, T1[11])}
    for k in range(12, 20):
        assert not _buys_on(result, T1[k])
    assert recorder.cash_share[T1[15]] < Decimal("0.05")


def test_naive_t2_nothing_deploys_before_settlement() -> None:
    result, recorder, _ = _naive_run(T2, redeploy=True)
    assert not _buys_on(result, T2[11])
    assert C in {e.isin for e in _buys_on(result, T2[12])}
    assert recorder.cash_share[T2[15]] < Decimal("0.05")


def test_naive_a_later_rebalance_supersedes_the_pending_target() -> None:
    rebalances = {T2[0], T2[10], T2[11], T2[20]}
    result, _, _ = _naive_run(T2, redeploy=True, rebalances=rebalances)
    for k in range(12, 20):
        assert not _buys_on(result, T2[k])


def test_naive_redeploy_reads_exactly_what_the_off_arm_reads() -> None:
    _, _, off = _naive_run(T2, redeploy=False)
    _, _, on = _naive_run(T2, redeploy=True)
    assert on.reads == off.reads
    assert {day for kind, day in on.reads if kind == "signal"} == {T2[0], T2[10], T2[20]}


def test_naive_parameters_repr_is_unchanged_when_off() -> None:
    assert repr(MomentumParameters(top_n=20)) == (
        "MomentumParameters(top_n=20, buy_budget_fraction=Decimal('0.98'), "
        "sleeve=<Sleeve.TACTICAL: 'TACTICAL'>)"
    )
    assert "redeploy_next_session=True" in repr(MomentumParameters(redeploy_next_session=True))


# ── property: a high-turnover stream does not leave the book in cash between rebalances ────────

#: Eight names, top-4 held, a fresh random ranking every rebalance (every 5 sessions) — a stream
#: built to turn the book over as often as the cadence allows.
_NAMES = tuple(f"INE{n:03d}A01010" for n in range(10, 18))
_STREAM = _weekdays(date(2024, 3, 4), 30)
_REBALANCES = _STREAM[::5]


def _mean_between_cash(
    make: Callable[[bool, Mapping[date, Mapping[str, Decimal]]], tuple[Policy, set[date]]],
    redeploy: bool,
    scores: Mapping[date, Mapping[str, Decimal]],
) -> Decimal:
    policy, rebalances = make(redeploy, scores)
    _, recorder = _replay(policy, _STREAM, _NAMES)
    between = [s for s in _STREAM[1:] if s not in rebalances]
    return sum((recorder.cash_share[s] for s in between), _ZERO) / Decimal(len(between))


def _swing_policy(
    redeploy: bool, scores: Mapping[date, Mapping[str, Decimal]]
) -> tuple[Policy, set[date]]:
    data = _SwingData(_STREAM, scores)
    return SwingCompositePolicy(data, _swing_params(redeploy=redeploy, top_n=4)), set(scores)


def _naive_policy(
    redeploy: bool, scores: Mapping[date, Mapping[str, Decimal]]
) -> tuple[Policy, set[date]]:
    params = MomentumParameters(top_n=4, redeploy_next_session=redeploy)
    return NaiveMomentumPolicy(_NaiveData(scores), params), set(scores)


_rankings = st.lists(
    st.permutations(range(len(_NAMES))), min_size=len(_REBALANCES), max_size=len(_REBALANCES)
)


def _scores(rankings: list[list[int]]) -> dict[date, dict[str, Decimal]]:
    return {
        day: {isin: Decimal(rank + 1) for isin, rank in zip(_NAMES, ranking, strict=True)}
        for day, ranking in zip(_REBALANCES, rankings, strict=True)
    }


#: Under T+1 the proceeds of a rebalance's exits are idle on one session of the four between
#: rebalances, so the mean between-rebalance cash share is bounded by about 1/4 plus the margin.
_BOUND = Decimal("0.30")


@settings(max_examples=15, deadline=None)
@given(rankings=_rankings)
def test_property_swing_mean_between_rebalance_cash_stays_under_the_bound(
    rankings: list[list[int]],
) -> None:
    assert _mean_between_cash(_swing_policy, True, _scores(rankings)) < _BOUND


@settings(max_examples=15, deadline=None)
@given(rankings=_rankings)
def test_property_naive_mean_between_rebalance_cash_stays_under_the_bound(
    rankings: list[list[int]],
) -> None:
    assert _mean_between_cash(_naive_policy, True, _scores(rankings)) < _BOUND


def test_the_bound_is_one_the_off_arm_breaches_on_a_full_rotation() -> None:
    """Inversion: the same stream with redeploy off sits above the bound, so the bound bites."""
    full = [list(range(8)), *([[7 - i for i in range(8)], list(range(8))] * 3)][: len(_REBALANCES)]
    scores = _scores(full)
    assert _mean_between_cash(_swing_policy, False, scores) > _BOUND
    assert _mean_between_cash(_naive_policy, False, scores) > _BOUND
    assert _mean_between_cash(_swing_policy, True, scores) < _BOUND
