"""Per-stock rebalance: momentum v2's daily band exit and daily drift trim (both off by default).

D13 rebalances the whole book on the first session of each month and does nothing per stock in
between. ``band_exit_daily`` sells, on every non-rebalance session, a settled holding that has left
the top ``sell_band`` of that session's ranking; ``drift_trim_band`` trims, on every session, a
settled holding marked more than the band above its target value back to target
(``ops/studies/preregistration-per-stock-rebalance-2026-10-15.md``).

Every rule test has an inverted twin at the boundary (rank 30 kept / rank 31 sold; +24 % kept /
+26 % trimmed; exactly +25 % kept), so flipping a comparison fails a test. The PIT test proves the
daily ranking goes through the guard; the replay test pins that the switches off reproduce D13's
journal, book and rails byte for byte (the digest struck before M14.6), and the parameter tests
that D13's run fingerprint did not move.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.fundmanager.controls import CONTROL_REBALANCE_BAND
from analyst.journal.models import Decision, JournalEntry
from backtest.policies.momentum_v2 import (
    D13_DAILY_BAND_EXIT,
    D13_DAILY_SLOT_FILL,
    D13_DRIFT_TRIM,
    DRIFT_TRIM_BAND,
    PAPER_RATIFIED_2026_09_06,
    TALLIED_SELL_KINDS,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.replay import SessionContext, SessionDecision
from backtest.run import policy_sell_tally
from backtest.run_ledger import RunSummary
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Position, Side
from tests.unit.test_momentum_v2_per_stock import _D13_DIGEST_BEFORE_M14_6, _replay

D13 = PAPER_RATIFIED_2026_09_06
PRICE = Decimal("100")
#: Forty names; NAMES[i] ranks i + 1 (a strictly falling 12-1 momentum), all at one volatility,
#: so D13's inverse-vol weights over its top 20 are exactly 0.05 each.
NAMES = tuple(f"INE{index:03d}C01010" for index in range(1, 41))
REBALANCE = date(2024, 1, 1)
DAY = date(2024, 1, 10)  # a non-rebalance session, and not the redeploy session after REBALANCE
NEXT = date(2024, 1, 11)


def _rank(isin: str) -> int:
    return NAMES.index(isin) + 1


class _Data:
    """Rebalance on REBALANCE only; risk-on unless ``off``; the ranking can be rescripted."""

    def __init__(self, *, off: bool = False, leak_signal: bool = False) -> None:
        self.order: dict[date, tuple[str, ...]] = {}
        self.missing: set[str] = set()
        self._off = off
        self._leak_signal = leak_signal
        self.signal_reads: list[date] = []

    def is_rebalance(self, session: date) -> bool:
        return session == REBALANCE

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        self.signal_reads.append(as_of)
        knowable = as_of + timedelta(days=1) if self._leak_signal else as_of
        order = self.order.get(as_of, NAMES)
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal(100 - position),
                momentum_12_1=Decimal(100 - position),
                price=PRICE,
                volatility=Decimal("0.2"),
                knowable_date=knowable,
            )
            for position, isin in enumerate(order)
            if isin not in self.missing
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        level = Decimal("95") if self._off else Decimal("105")
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


class _Broker:
    """The read surface the policy uses: settled holdings, unsettled positions, cash."""

    def __init__(
        self,
        *,
        cash: str = "1000000",
        holdings: dict[str, int] | None = None,
        positions: dict[str, int] | None = None,
    ) -> None:
        self._cash = Decimal(cash)
        self._holdings = tuple(
            Holding(isin=i, exchange=Exchange.NSE, quantity=q, average_price=PRICE)
            for i, q in (holdings or {}).items()
        )
        self._positions = tuple(
            Position(isin=i, exchange=Exchange.NSE, quantity=q, average_price=PRICE, session=DAY)
            for i, q in (positions or {}).items()
        )

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[Position, ...]:
        return self._positions

    def margins(self) -> Margins:
        return Margins(available=self._cash, utilised=Decimal("0"))


def _ctx(session: date, broker: _Broker) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker,  # type: ignore[arg-type]  # the fake satisfies the read surface used
        clock=FrozenClock(session),
    )


def _after_rebalance(params: MomentumV2Parameters, data: _Data) -> MomentumV2Policy:
    """A policy that has made its risk-on rebalance on an all-cash book (no sells, no redeploy)."""
    policy = MomentumV2Policy(data, params)
    decision = policy.decide(_ctx(REBALANCE, _Broker()))
    assert decision.orders and all(o.side is Side.BUY for o in decision.orders)
    assert policy.pending is None
    return policy


def _sold(decision: SessionDecision) -> dict[str, int]:
    return {o.isin: o.quantity for o in decision.orders if o.side is Side.SELL}


def _bought(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.BUY}


def _labels(decision: SessionDecision) -> dict[str, Decimal | None]:
    return {item.label: item.value for item in decision.evidence.items}


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_the_switches_are_off_by_default_and_d13_leaves_them_off() -> None:
    params = MomentumV2Parameters()
    assert not params.band_exit_daily
    assert params.drift_trim_band is None
    assert not params.per_stock_rebalance
    assert not D13.per_stock_rebalance


def test_d13s_repr_is_unchanged_so_its_run_fingerprint_is_unchanged() -> None:
    assert repr(D13) == (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )


def test_the_arms_are_d13_plus_exactly_their_switch() -> None:
    assert repr(D13_DAILY_BAND_EXIT) == repr(D13).removesuffix(")") + ", band_exit_daily=True)"
    assert repr(D13_DRIFT_TRIM) == (
        repr(D13).removesuffix(")") + ", drift_trim_band=Decimal('0.25'))"
    )
    assert replace(D13_DAILY_BAND_EXIT, band_exit_daily=False) == D13
    assert replace(D13_DRIFT_TRIM, drift_trim_band=None) == D13


def test_the_trim_band_is_the_fund_managers_control_band_fixed_a_priori() -> None:
    assert DRIFT_TRIM_BAND == CONTROL_REBALANCE_BAND == Decimal("0.25")


def test_the_switches_are_validated() -> None:
    with pytest.raises(ValueError, match="positive"):
        replace(D13, drift_trim_band=Decimal("0"))
    with pytest.raises(TypeError, match="Decimal"):
        replace(D13, drift_trim_band=0.25)  # type: ignore[arg-type]
    for clash in (
        {"daily_slot_fill": True},
        {"regime_daily_reentry": True},
        {"vol_target_annual": Decimal("0.15")},
        {"industry_gate": True},
    ):
        with pytest.raises(ValueError, match="D13's own toggles"):
            replace(D13_DAILY_BAND_EXIT, **clash)
    with pytest.raises(ValueError):
        replace(D13_DAILY_SLOT_FILL, drift_trim_band=DRIFT_TRIM_BAND)


# ── parity: switches off is D13, byte for byte ───────────────────────────────────────────────────


def test_switches_off_reproduce_d13_byte_for_byte() -> None:
    explicit_off = replace(D13, band_exit_daily=False, drift_trim_band=None)
    assert explicit_off == D13
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M14_6
    assert _replay(explicit_off).digest() == _D13_DIGEST_BEFORE_M14_6


def test_switches_off_the_same_session_is_a_d13_heartbeat() -> None:
    data = _Data()
    policy = _after_rebalance(D13, data)
    held = {NAMES[29]: 100, NAMES[30]: 100}
    decision = policy.decide(_ctx(DAY, _Broker(holdings=held)))
    assert decision.orders == ()
    assert "daily_band_exit" not in _labels(decision)
    assert "drift_trim" not in _labels(decision)


# ── arm 1: daily band exit ───────────────────────────────────────────────────────────────────────


def test_band_exit_sells_rank_31_in_full_and_keeps_rank_30() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    inside, outside = NAMES[29], NAMES[30]  # ranks 30 and 31 today
    assert (_rank(inside), _rank(outside)) == (30, 31)
    decision = policy.decide(_ctx(DAY, _Broker(holdings={inside: 70, outside: 90})))
    assert _sold(decision) == {outside: 90}
    assert _bought(decision) == set()  # an exit session never buys
    assert _labels(decision)["daily_band_exit"] == Decimal(1)
    (entry,) = (e for e in decision.entries if e.decision is Decision.SELL)
    assert entry.isin == outside and (entry.rationale or "").startswith("daily band exit: rank 31")


def test_band_exit_sells_a_holding_unranked_today_as_the_rebalance_would() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    data.missing = {NAMES[0]}
    decision = policy.decide(_ctx(DAY, _Broker(holdings={NAMES[0]: 10, NAMES[1]: 10})))
    assert _sold(decision) == {NAMES[0]: 10}


def test_band_exit_reads_todays_ranking_not_the_rebalances() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    # NAMES[0] ranked first at the rebalance and falls to 40th today; NAMES[35] ranked 36th at
    # the rebalance and is 35th today — still outside the band, so it goes too.
    data.order[DAY] = (*NAMES[1:], NAMES[0])
    held = {NAMES[0]: 10, NAMES[30]: 10, NAMES[35]: 10}
    decision = policy.decide(_ctx(DAY, _Broker(holdings=held)))
    # NAMES[30] ranked 31st at the rebalance and is 30th today: kept.
    assert _sold(decision) == {NAMES[0]: 10, NAMES[35]: 10}


def test_band_exit_never_sells_shares_still_in_settlement() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    decision = policy.decide(_ctx(DAY, _Broker(positions={NAMES[35]: 10})))
    assert decision.orders == ()


def test_band_exit_does_not_run_while_the_regime_has_the_book_parked() -> None:
    data = _Data(off=True)
    policy = MomentumV2Policy(data, D13_DAILY_BAND_EXIT)
    policy.decide(_ctx(REBALANCE, _Broker(holdings={NAMES[35]: 10})))  # risk-off: parks
    assert policy.parked
    decision = policy.decide(_ctx(DAY, _Broker(holdings={NAMES[35]: 10})))
    assert decision.orders == ()
    item = next(i for i in decision.evidence.items if i.label == "per_stock_rebalance")
    assert item.detail["skipped"] == "book parked by the regime filter"


def test_band_exit_proceeds_redeploy_next_session_into_the_basket_less_the_exited_name() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    data.order[DAY] = (*NAMES[1:], NAMES[0])  # NAMES[0], a basket name, falls out of the band
    held = dict.fromkeys(NAMES[:20], 10)
    decision = policy.decide(_ctx(DAY, _Broker(holdings=held, cash="200000")))
    assert set(_sold(decision)) == {NAMES[0]}
    pending = policy.pending
    assert pending is not None
    assert NAMES[0] not in pending and set(pending) == set(NAMES[1:20])
    assert sum(pending.values()) == Decimal(1)
    data.order[NEXT] = data.order[DAY]
    after = dict.fromkeys(NAMES[1:20], 10)
    redeploy = policy.decide(_ctx(NEXT, _Broker(holdings=after, cash="200000")))
    assert _bought(redeploy) and _bought(redeploy) <= set(NAMES[1:20])
    assert _sold(redeploy) == {}
    assert "redeploy_budget" in _labels(redeploy)


def test_a_no_op_band_check_is_journaled_as_evidence() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, data)
    decision = policy.decide(_ctx(DAY, _Broker(holdings={NAMES[0]: 10})))
    assert decision.orders == ()
    assert _labels(decision)["daily_band_exit"] == Decimal(0)
    assert policy.pending is None


def test_the_daily_ranking_goes_through_the_pit_guard() -> None:
    honest = _Data()
    policy = _after_rebalance(D13_DAILY_BAND_EXIT, honest)
    policy._data = _Data(leak_signal=True)  # the same policy, now fed a record dated tomorrow
    with pytest.raises(PitError):
        policy.decide(_ctx(DAY, _Broker(holdings={NAMES[0]: 10})))


# ── arm 2: drift trim ────────────────────────────────────────────────────────────────────────────

#: Book value 2,000,000 at PRICE: cash 1,750,000 + 1,240 + 1,260 shares (250,000). Every basket
#: weight is 0.05, so each target value is 100,000 and the trim trigger is 125,000.
_CASH = "1750000"


def test_a_name_24_percent_over_target_is_not_trimmed_and_26_percent_is_trimmed_to_target() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    under, over = NAMES[0], NAMES[1]
    decision = policy.decide(_ctx(DAY, _Broker(cash=_CASH, holdings={under: 1240, over: 1260})))
    # 1,260 x 100 = 126,000 against 100,000: sell 260 shares, back to exactly 100,000.
    assert _sold(decision) == {over: 260}
    assert _bought(decision) == set()
    assert _labels(decision)["drift_trim"] == Decimal(1)
    (entry,) = (e for e in decision.entries if e.decision is Decision.SELL)
    assert (entry.rationale or "").startswith("drift trim: marked 126000 is 26.00% above")


def test_exactly_25_percent_over_target_is_not_trimmed() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    # 1,250 + 1,250 shares: each 125,000, exactly the band; book value still 2,000,000.
    decision = policy.decide(
        _ctx(DAY, _Broker(cash=_CASH, holdings={NAMES[0]: 1250, NAMES[1]: 1250}))
    )
    assert decision.orders == ()
    assert _labels(decision)["drift_trim"] == Decimal(0)


def test_the_trim_counts_shares_in_settlement_but_sells_only_settled_ones() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    # 1,000 settled + 500 in settlement = 150,000 against a 100,000 target (book 2,000,000):
    # the gap is 500 shares; only the settled 1,000 can be delivered, so 500 are sold.
    decision = policy.decide(
        _ctx(
            DAY,
            _Broker(cash="1850000", holdings={NAMES[0]: 1000}, positions={NAMES[0]: 500}),
        )
    )
    assert _sold(decision) == {NAMES[0]: 500}


def test_a_held_name_outside_the_basket_has_no_target_and_is_never_trimmed() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    outside = NAMES[25]  # rank 26: inside the sell band, outside the top-20 basket
    decision = policy.decide(_ctx(DAY, _Broker(cash="1000000", holdings={outside: 5000})))
    assert decision.orders == ()


def test_a_trim_below_the_minimum_order_value_is_not_placed() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    # Book 100,000: target 5,000; 64 shares = 6,400 is 28 % over, but the trim is 14 shares =
    # 1,400, below the 5,000 minimum order.
    decision = policy.decide(_ctx(DAY, _Broker(cash="93600", holdings={NAMES[0]: 64})))
    assert decision.orders == ()
    item = next(i for i in decision.evidence.items if i.label == "drift_trim")
    assert item.detail["below_minimum_order"] == "1"


def test_drift_trim_also_runs_on_the_rebalance_session_against_the_new_weights() -> None:
    data = _Data()
    policy = MomentumV2Policy(data, D13_DRIFT_TRIM)
    decision = policy.decide(
        _ctx(REBALANCE, _Broker(cash=_CASH, holdings={NAMES[0]: 1240, NAMES[1]: 1260}))
    )
    assert _sold(decision) == {NAMES[1]: 260}
    assert NAMES[1] not in _bought(decision)
    assert policy.pending is not None  # the trim's proceeds are redeployed next session
    d13 = MomentumV2Policy(_Data(), D13).decide(
        _ctx(REBALANCE, _Broker(cash=_CASH, holdings={NAMES[0]: 1240, NAMES[1]: 1260}))
    )
    assert _sold(d13) == {}  # inversion: D13's rebalance never trims an overweight name


def test_trim_proceeds_redeploy_next_session_like_a_rebalances_sells() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    policy.decide(_ctx(DAY, _Broker(cash=_CASH, holdings={NAMES[0]: 1240, NAMES[1]: 1260})))
    pending = policy.pending
    assert pending is not None and set(pending) == set(NAMES[:20])


def test_without_the_switch_the_same_overweight_book_is_left_alone_inversion() -> None:
    data = _Data()
    policy = _after_rebalance(D13, data)
    decision = policy.decide(
        _ctx(DAY, _Broker(cash=_CASH, holdings={NAMES[0]: 1240, NAMES[1]: 1260}))
    )
    assert decision.orders == ()


# ── the run's tally of what fired ────────────────────────────────────────────────────────────────


def _sell(rationale: str) -> JournalEntry:
    from analyst.journal.models import Actor, Sleeve

    return JournalEntry(
        ts=FrozenClock(DAY).now(),
        trading_date=DAY,
        actor=Actor.T0,
        decision=Decision.SELL,
        isin=NAMES[0],
        sleeve=Sleeve.TACTICAL,
        rationale=rationale,
    )


def test_the_tally_counts_each_kind_and_nothing_else() -> None:
    journal = [
        _sell("daily band exit: rank 31, outside the top-30 band; liquidating 5 shares"),
        _sell("drift trim: marked 126000 is 26.00% above its target"),
        _sell("drift trim: marked 130000 is 30.00% above its target"),
        _sell("left the top-30 momentum band; liquidating 5 shares"),
    ]
    assert policy_sell_tally(journal) == {"daily band exit": 1, "drift trim": 2}
    assert policy_sell_tally(journal[3:]) == {}
    assert set(TALLIED_SELL_KINDS) == {"daily band exit", "drift trim"}


def test_a_summary_without_a_tally_renders_as_before() -> None:
    fields = {
        "digest": "d",
        "spec": {},
        "policy": "p",
        "start": DAY,
        "terminal": NEXT,
        "sessions": 2,
        "xirr": Decimal("0.1"),
        "max_drawdown": Decimal("0.1"),
        "excess": Decimal("0"),
        "benchmark_xirr": Decimal("0.1"),
        "benchmark_name": "b",
        "total_charges": Decimal("1"),
        "final_nav": Decimal("1"),
        "round_trips": 0,
        "median_hold_days": 0,
        "replay_digest": "r",
    }
    plain = RunSummary(**fields)  # type: ignore[arg-type]
    assert "policy_sells" not in plain.to_document()
    tallied = RunSummary(**fields, policy_sells={"drift trim": 3})  # type: ignore[arg-type]
    assert RunSummary.from_document(tallied.to_document()) == tallied
    assert RunSummary.from_document(plain.to_document()) == plain


def test_a_per_stock_rebalance_book_refuses_resume_rather_than_drop_its_basket() -> None:
    for params in (D13_DAILY_BAND_EXIT, D13_DRIFT_TRIM):
        policy = MomentumV2Policy(_Data(), params)
        with pytest.raises(NotImplementedError, match="research-only"):
            policy.resume(None)
    MomentumV2Policy(_Data(), D13).resume(None)  # D13 itself still resumes


# ── a name sold this session is never bought this session (study-run bug, 2026-10-15) ────────────
#
# The allocator sizes against *settled* holdings and the free cash; the trim values the whole book,
# shares in settlement included. With 500 settled + 800 in settlement the trim sees 130,000 against
# a 100,000 target and sells 300, while the allocator sees 50,000 and tops the name up. The broker
# refuses a second staged order for one ISIN, which stopped the first study run.


def test_the_redeploy_never_buys_a_name_the_trim_sells_the_same_session() -> None:
    data = _Data()
    policy = _after_rebalance(D13_DRIFT_TRIM, data)
    policy._pending = dict.fromkeys(NAMES[:20], Decimal("0.05"))  # yesterday's sells to redeploy
    name = NAMES[0]
    broker = _Broker(cash="1870000", holdings={name: 500}, positions={name: 800})
    decision = policy.decide(_ctx(DAY, broker))
    assert _sold(decision) == {name: 300}
    assert name not in _bought(decision)
    assert _bought(decision)  # the rest of the basket is still redeployed
    assert _labels(decision)["buys_dropped_sold_today"] == Decimal(1)
    assert all(not (e.decision is Decision.BUY and e.isin == name) for e in decision.entries)


def test_the_rebalance_never_buys_a_name_its_drift_trim_sells() -> None:
    policy = MomentumV2Policy(_Data(), D13_DRIFT_TRIM)
    name = NAMES[0]
    broker = _Broker(cash="1870000", holdings={name: 500}, positions={name: 800})
    decision = policy.decide(_ctx(REBALANCE, broker))
    assert _sold(decision) == {name: 300}
    assert name not in _bought(decision)
    assert _labels(decision)["buys_dropped_sold_today"] == Decimal(1)
