"""PE timing — the valuation gate on D13's regime seam, and its point-in-time P/E source.

``ops/studies/preregistration-pe-timing-2026-10-19.md`` fixes the rule: the market is *cheap* on a
session when the NIFTY 500 P/E is strictly below the simple mean of its last ``L`` published values
knowable that session. The gate acts in one of three modes (``ENTRY``, ``REPLACE``, ``BOTH``). Each
behaviour test has an inverted twin that fails if the comparison is flipped; the PIT tests prove a
reading on ``t`` never sees a P/E published after ``t``; and the off-by-default tests pin D13's
fingerprint and replay.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.journal.models import Decision
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
    ValuationGate,
    ValuationReading,
    d13_valuation_gate,
    regime_parked,
)
from backtest.replay import SessionDecision
from backtest.sweep import PE_TIMING_ARMS
from backtest.valuation_gate import (
    PE_METHODOLOGY_BREAK,
    PePoint,
    ValuationSource,
    ValuationSourceError,
    splice_factor,
)
from dataplatform.query.pit import Dataset, PitError
from execution.broker import Side
from tests.unit.test_industry_gate import _D13_DIGEST_BEFORE_M16_2, _replay, _ScriptedData
from tests.unit.test_momentum_v2_daily_regime import (
    REBALANCE,
    A,
    B,
    C,
    _Broker,
    _ctx,
    _held,
    _records,
)

D13 = PAPER_RATIFIED_2026_09_06


def _params(gate: ValuationGate, **overrides: object) -> MomentumV2Parameters:
    fields = {
        "top_n": 2,
        "regime_filter": True,
        "redeploy_next_session": True,
        "valuation_gate": gate,
        "valuation_mean_days": 21,
        **overrides,
    }
    return MomentumV2Parameters(**fields)  # type: ignore[arg-type]


def _valuation(pe: str, mean: str = "20", knowable: date = REBALANCE) -> ValuationReading:
    return ValuationReading(
        pe=Decimal(pe), trailing_mean=Decimal(mean), mean_days=21, knowable_date=knowable
    )


class _Data:
    """Rebalance on REBALANCE; a scripted price level vs MA 100, and a scripted P/E vs mean 20."""

    def __init__(self, *, level: str, pe: str | None, leak_pe: bool = False) -> None:
        self.level = Decimal(level)
        self.pe = pe
        self._leak_pe = leak_pe

    def is_rebalance(self, session: date) -> bool:
        return session == REBALANCE

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return Dataset.declaring(
            f"m@{as_of}", _records(as_of), knowable_date=lambda r: r.knowable_date
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        knowable = as_of + timedelta(days=1) if self._leak_pe else as_of
        reading = RegimeReading(
            index_level=self.level,
            moving_average=Decimal("100"),
            knowable_date=as_of,
            valuation=None if self.pe is None else _valuation(self.pe, knowable=knowable),
        )
        return Dataset.declaring(
            f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_through
        )


def _decide(params: MomentumV2Parameters, data: _Data, broker: _Broker) -> SessionDecision:
    return MomentumV2Policy(data, params).decide(_ctx(REBALANCE, broker))


def _buys(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.BUY}


def _sells(decision: SessionDecision) -> set[str]:
    return {o.isin for o in decision.orders if o.side is Side.SELL}


# ── the reading ──────────────────────────────────────────────────────────────────────────────────


def test_cheap_is_strictly_below_the_trailing_mean() -> None:
    assert _valuation("19.99").cheap
    assert not _valuation("20").cheap  # equal reads rich
    assert not _valuation("20.01").cheap


def test_the_reading_refuses_floats_and_non_positive_figures() -> None:
    with pytest.raises(TypeError):
        ValuationReading(
            pe=20.0, trailing_mean=Decimal("20"), mean_days=21, knowable_date=REBALANCE
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        _valuation("0")
    with pytest.raises(ValueError, match="mean_days"):
        ValuationReading(
            pe=Decimal("1"), trailing_mean=Decimal("1"), mean_days=0, knowable_date=REBALANCE
        )


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_the_gate_is_off_by_default_and_absent_from_d13s_fingerprint() -> None:
    assert D13.valuation_gate is None and D13.valuation_mean_days is None
    assert "valuation" not in repr(D13)
    assert repr(D13) == repr(replace(D13, valuation_gate=None))


def test_every_arm_has_its_own_fingerprint() -> None:
    renders = {repr(arm.v2) for arm in PE_TIMING_ARMS} | {repr(D13)}
    assert len(renders) == len(PE_TIMING_ARMS) + 1 == 19
    assert repr(d13_valuation_gate(ValuationGate.BOTH, 63)).endswith(
        "valuation_gate=<ValuationGate.BOTH: 'both'>, valuation_mean_days=63)"
    )
    assert "valuation_splice=True" in repr(d13_valuation_gate(ValuationGate.BOTH, 63, splice=True))


def test_the_gate_is_validated() -> None:
    with pytest.raises(ValueError, match="regime_filter"):
        MomentumV2Parameters(valuation_gate=ValuationGate.ENTRY, valuation_mean_days=21)
    with pytest.raises(ValueError, match="valuation_mean_days must be positive"):
        _params(ValuationGate.ENTRY, valuation_mean_days=None)
    with pytest.raises(ValueError, match="need a valuation_gate"):
        MomentumV2Parameters(valuation_mean_days=21)
    with pytest.raises(ValueError, match="need a valuation_gate"):
        MomentumV2Parameters(valuation_splice=True)
    with pytest.raises(ValueError, match="monthly rebalance only"):
        _params(ValuationGate.BOTH, regime_daily_reentry=True)


def test_a_gate_with_no_reading_from_its_source_fails_loud() -> None:
    with pytest.raises(ValueError, match="carries no P/E reading"):
        _decide(_params(ValuationGate.REPLACE), _Data(level="110", pe=None), _Broker())


# ── REPLACE: the P/E reading parks and re-enters instead of the price regime ────────────────────


def test_replace_holds_the_basket_when_cheap_even_with_the_price_regime_off() -> None:
    decision = _decide(_params(ValuationGate.REPLACE), _Data(level="90", pe="18"), _Broker())
    assert _buys(decision) == {A, B}


def test_replace_parks_when_rich_even_with_the_price_regime_on_inversion() -> None:
    decision = _decide(
        _params(ValuationGate.REPLACE), _Data(level="110", pe="22"), _Broker(holdings=_held(A, C))
    )
    assert _sells(decision) == {A, C} and not _buys(decision)
    assert regime_parked(decision.evidence)
    (item,) = decision.evidence.items
    assert item.detail["pe_cheap"] == "false" and item.detail["price_risk_on"] == "true"
    assert all("P/E 22 >= 21-session mean 20" in e.rationale for e in decision.entries)


def test_replace_parks_at_exactly_the_mean() -> None:
    decision = _decide(_params(ValuationGate.REPLACE), _Data(level="110", pe="20"), _Broker())
    assert regime_parked(decision.evidence)


# ── BOTH: invested only while the price regime is on and the P/E is cheap ───────────────────────


@pytest.mark.parametrize(
    ("level", "pe", "invested"),
    [("110", "18", True), ("110", "22", False), ("90", "18", False), ("90", "22", False)],
)
def test_both_needs_the_price_regime_and_a_cheap_pe(level: str, pe: str, invested: bool) -> None:
    decision = _decide(_params(ValuationGate.BOTH), _Data(level=level, pe=pe), _Broker())
    assert regime_parked(decision.evidence) is not invested
    assert bool(_buys(decision)) is invested


# ── ENTRY: price regime parks; a rich P/E blocks every buy ───────────────────────────────────────


def test_entry_gate_buys_when_cheap_and_the_price_regime_is_on() -> None:
    decision = _decide(_params(ValuationGate.ENTRY), _Data(level="110", pe="18"), _Broker())
    assert _buys(decision) == {A, B}
    gate = next(i for i in decision.evidence.items if i.label == "valuation_gate")
    assert gate.detail["entries_blocked"] == "false"


def test_entry_gate_blocks_every_buy_when_rich_but_keeps_the_band_sells_inversion() -> None:
    # Held: A (inside the band) and C (outside the top-2 band): C is sold, nothing is bought, and
    # nothing is queued for tomorrow's redeploy.
    policy = MomentumV2Policy(_Data(level="110", pe="22"), _params(ValuationGate.ENTRY))
    decision = policy.decide(_ctx(REBALANCE, _Broker(holdings=_held(A, C))))
    assert _sells(decision) == {C}
    assert not _buys(decision)
    assert policy.pending is None
    assert not regime_parked(decision.evidence)
    gate = next(i for i in decision.evidence.items if i.label == "valuation_gate")
    assert gate.detail["entries_blocked"] == "true"


def test_entry_gate_still_parks_on_the_price_regime_whatever_the_pe() -> None:
    decision = _decide(
        _params(ValuationGate.ENTRY), _Data(level="90", pe="18"), _Broker(holdings=_held(A))
    )
    assert regime_parked(decision.evidence)
    assert _sells(decision) == {A}
    assert all(e.rationale.startswith("regime risk-off (index 90") for e in decision.entries)


def test_entry_gate_keeps_a_parked_empty_book_in_cash_while_rich() -> None:
    # Re-entry is blocked: price risk-on, but the P/E is rich and the book holds nothing.
    decision = _decide(_params(ValuationGate.ENTRY), _Data(level="110", pe="22"), _Broker())
    assert decision.orders == ()


def test_a_parked_book_with_nothing_to_sell_journals_why() -> None:
    decision = _decide(_params(ValuationGate.REPLACE), _Data(level="110", pe="25"), _Broker())
    (hold,) = decision.entries
    assert hold.decision is Decision.HOLD
    assert hold.payload["pe"] == "25" and hold.payload["valuation_gate"] == "replace"


# ── point-in-time ────────────────────────────────────────────────────────────────────────────────


def test_a_pe_published_after_the_session_trips_the_guard() -> None:
    with pytest.raises(PitError):
        _decide(
            _params(ValuationGate.REPLACE), _Data(level="110", pe="18", leak_pe=True), _Broker()
        )


def _points(values: list[str], start: date = date(2021, 1, 4)) -> list[PePoint]:
    days = [start + timedelta(days=i) for i in range(len(values))]
    return [PePoint(d, d, Decimal(v)) for d, v in zip(days, values, strict=True)]


def test_the_trailing_mean_reads_only_points_knowable_by_the_session() -> None:
    points = _points(["10", "12", "14", "100"])  # the last point is the future
    source = ValuationSource(points, mean_days=3)
    reading = source.reading(points[2].session)
    assert reading.pe == Decimal("14")
    assert reading.trailing_mean == Decimal("12")  # (10 + 12 + 14) / 3 — 100 never enters
    assert reading.knowable_date == points[2].session


def test_a_point_released_late_is_not_in_the_mean_before_its_release() -> None:
    # Session 3's P/E is dated session 3 but released a day after session 4: on session 4 the mean
    # is struck over what was knowable — sessions 1, 2 and 4.
    days = [date(2021, 1, 4) + timedelta(days=i) for i in range(4)]
    points = [
        PePoint(days[0], days[0], Decimal("10")),
        PePoint(days[1], days[1], Decimal("20")),
        PePoint(days[2], days[3] + timedelta(days=1), Decimal("1000")),
        PePoint(days[3], days[3], Decimal("30")),
    ]
    reading = ValuationSource(points, mean_days=3).reading(days[3])
    assert reading.trailing_mean == Decimal("20")


def test_a_missing_or_short_series_fails_loud() -> None:
    source = ValuationSource(_points(["10", "12"]), mean_days=3)
    with pytest.raises(ValuationSourceError, match="short of the 3-session mean"):
        source.reading(date(2021, 1, 5))
    with pytest.raises(ValuationSourceError, match="no P/E for"):
        source.reading(date(2022, 1, 5))


# ── the splice across NSE's 2021-03-31 earnings-basis change ────────────────────────────────────


def test_the_splice_factor_is_the_implied_earnings_ratio_across_the_break() -> None:
    before, at = date(2021, 3, 30), PE_METHODOLOGY_BREAK
    pe = {before: Decimal("40"), at: Decimal("30")}
    close = {before: Decimal("1000"), at: Decimal("990")}
    # earnings 1000/40 = 25 before, 990/30 = 33 after: pre-break P/Es restate by 25/33.
    assert splice_factor(pe, close, break_session=at) == Decimal(25) / Decimal(33)
    with pytest.raises(ValuationSourceError):
        splice_factor({at: Decimal("30")}, close, break_session=at)


def _around_break() -> list[PePoint]:
    days = [PE_METHODOLOGY_BREAK + timedelta(days=i) for i in range(-3, 3)]
    values = ["40", "40", "40", "30", "30", "30"]
    return [PePoint(d, d, Decimal(v)) for d, v in zip(days, values, strict=True)]


def test_raw_reads_spuriously_cheap_after_the_break_and_the_splice_does_not() -> None:
    points = _around_break()
    session = points[4].session  # one session after the break; the 3-mean straddles it
    raw = ValuationSource(points, mean_days=3).reading(session)
    spliced = ValuationSource(points, mean_days=3, factor=Decimal("0.75")).reading(session)
    assert raw.cheap and raw.trailing_mean == Decimal("100") / 3
    assert not spliced.cheap and spliced.trailing_mean == Decimal("30") and spliced.spliced


def test_the_splice_never_rescales_a_reading_before_the_break() -> None:
    points = _around_break()
    for p in points[:3]:
        if p.session < points[2].session:
            continue
        raw = ValuationSource(points, mean_days=3).reading(p.session)
        spliced = ValuationSource(points, mean_days=3, factor=Decimal("0.75")).reading(p.session)
        assert raw == spliced and not spliced.spliced


def test_a_window_wholly_after_the_break_is_unchanged_by_the_splice() -> None:
    points = _around_break()
    session = points[5].session
    raw = ValuationSource(points, mean_days=2).reading(session)
    spliced = ValuationSource(points, mean_days=2, factor=Decimal("0.75")).reading(session)
    assert raw == spliced and not spliced.spliced


# ── the real replay: D13 unchanged, the gate wired end to end ───────────────────────────────────


class _ScriptedValued(_ScriptedData):
    """The M14.5/M16.2 scripted world, plus a P/E that reads rich in February only."""

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        (reading,) = super().regime(as_of).records
        pe = Decimal("25") if as_of.month == 2 else Decimal("15")
        valued = replace(
            reading,
            valuation=ValuationReading(
                pe=pe, trailing_mean=Decimal("20"), mean_days=21, knowable_date=as_of
            ),
        )
        return Dataset.declaring(
            f"r@{as_of}", (valued,), knowable_date=lambda r: r.knowable_through
        )


def test_gate_off_reproduces_d13s_replay_byte_for_byte() -> None:
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M16_2
    # A source that also serves a P/E changes nothing while the gate is off.
    assert _replay(D13, _ScriptedValued()).digest() == _D13_DIGEST_BEFORE_M16_2


def test_each_gate_mode_replays_and_moves_the_journal() -> None:
    digests = {
        gate: _replay(d13_valuation_gate(gate, 21), _ScriptedValued()).digest()
        for gate in ValuationGate
    }
    assert _D13_DIGEST_BEFORE_M16_2 not in digests.values()
    entry = _replay(d13_valuation_gate(ValuationGate.ENTRY, 21), _ScriptedValued())
    february_buys = [
        e for e in entry.journal if e.decision is Decision.BUY and e.trading_date.month == 2
    ]
    assert not february_buys  # February reads rich: the entry gate buys nothing that month
