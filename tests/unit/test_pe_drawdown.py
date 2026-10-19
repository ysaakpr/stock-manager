"""PE-drawdown study: the comparators' switches, the calibration and the exposure arithmetic.

Offline: synthetic policies, ledgers and NAVs only. The switches are pinned by inversion tests
(the exposure cap that would buy past its cap, the stricter regime read the wrong way round), and
D13's own fingerprint is pinned unchanged, so no earlier run's digest moves.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.nav import NavSeries, nav_file, write_nav
from backtest.pe_drawdown import (
    ANALOGUE,
    BAND,
    CANDIDATE,
    NIFTY50,
    VOL_TARGET_15,
    StudyError,
    equal_exposure_arm,
    equal_exposure_fraction,
    invested_fractions,
    mix_path,
    tighter_vol_target,
)
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    RegimeReading,
    ValuationGate,
)
from backtest.run_ledger import ledger_path
from backtest.sweep import PE_TIMING_ARMS
from backtest.tax import RunLedger, TaxTrade
from backtest.tax_report import write_run_ledger
from backtest.valuation_gate import CLOSE_SERIES, NIFTY50_PE_SERIES, ValuationSource
from backtest.xirr import Cashflow
from execution.broker import Side
from tests.unit.test_momentum_v2 import _RECORDS, SESSION, _ctx, _Data, _FakeBroker, _holding

# ── fingerprints ───────────────────────────────────────────────────────────────────────────────


def test_d13_fingerprint_is_unchanged_by_the_new_switches() -> None:
    text = repr(PAPER_RATIFIED_2026_09_06)
    for name in ("valuation_series", "regime_monthly_band", "exposure_fraction"):
        assert name not in text


def test_the_candidate_is_the_pe_timing_arm_itself() -> None:
    parent = next(a for a in PE_TIMING_ARMS if a.label == CANDIDATE.label)
    assert repr(parent.v2) == repr(CANDIDATE.v2)
    assert CANDIDATE.v2 is not None
    assert CANDIDATE.v2.valuation_gate is ValuationGate.BOTH
    assert CANDIDATE.v2.valuation_mean_days == 63


def test_vt15_is_the_round_one_m95_configuration() -> None:
    m95 = MomentumV2Parameters(
        top_n=20,
        use_12_1=True,
        sell_band=30,
        regime_filter=True,
        vol_scaled=True,
        redeploy_next_session=True,
        vol_target_annual=Decimal("0.15"),
    )
    assert repr(VOL_TARGET_15.v2) == repr(m95)


def test_comparator_series_and_switches() -> None:
    assert ANALOGUE.v2 is not None and ANALOGUE.v2.valuation_series == CLOSE_SERIES
    assert NIFTY50.v2 is not None and NIFTY50.v2.valuation_series == NIFTY50_PE_SERIES
    assert BAND.v2 is not None and BAND.v2.regime_monthly_band == Decimal("0.02")
    assert equal_exposure_arm(None).v2 == PAPER_RATIFIED_2026_09_06


# ── parameter validation ───────────────────────────────────────────────────────────────────────


def test_switches_are_validated() -> None:
    with pytest.raises(ValueError, match="valuation_series needs"):
        replace(PAPER_RATIFIED_2026_09_06, valuation_series=CLOSE_SERIES)
    with pytest.raises(ValueError, match="turn it on"):
        MomentumV2Parameters(regime_monthly_band=Decimal("0.02"))
    with pytest.raises(ValueError, match="in \\(0, 1\\)"):
        replace(PAPER_RATIFIED_2026_09_06, exposure_fraction=Decimal("1"))
    with pytest.raises(ValueError, match="vol_target_annual"):
        replace(
            PAPER_RATIFIED_2026_09_06,
            exposure_fraction=Decimal("0.5"),
            vol_target_annual=Decimal("0.15"),
        )


def test_the_splice_is_refused_on_another_series() -> None:
    with pytest.raises(ValueError, match="splice is defined"):
        ValuationSource.published(
            through=date(2022, 1, 3), mean_days=63, splice=True, series=NIFTY50_PE_SERIES
        )


# ── the exposure cap ───────────────────────────────────────────────────────────────────────────


def test_exposure_cap_limits_buys_to_its_share_of_capital_inversion() -> None:
    plain = MomentumV2Policy(_Data(_RECORDS), MomentumV2Parameters(top_n=2)).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000")))
    )
    capped = MomentumV2Policy(
        _Data(_RECORDS), MomentumV2Parameters(top_n=2, exposure_fraction=Decimal("0.4"))
    ).decide(_ctx(SESSION, _FakeBroker(cash=Decimal("100000"))))
    spent_plain = sum(o.quantity for o in plain.orders if o.side is Side.BUY) * 100
    spent_capped = sum(o.quantity for o in capped.orders if o.side is Side.BUY) * 100
    assert spent_capped <= 40000 < spent_plain
    assert spent_capped > 40000 - 200  # whole shares only
    assert {i.label: i for i in capped.evidence.items}["exposure_cap"].value == Decimal("0.4")


def test_exposure_cap_trims_an_over_exposed_book() -> None:
    held = (_holding("INE001A01036", 500), _holding("INE002A01018", 500))
    decision = MomentumV2Policy(
        _Data(_RECORDS), MomentumV2Parameters(top_n=2, exposure_fraction=Decimal("0.5"))
    ).decide(_ctx(SESSION, _FakeBroker(cash=Decimal("0"), holdings=held)))
    sells = [o for o in decision.orders if o.side is Side.SELL]
    assert sum(o.quantity for o in sells) == 500  # 100,000 book cut to 50,000
    assert not [o for o in decision.orders if o.side is Side.BUY]


def test_the_redeploy_respects_the_cap() -> None:
    from tests.unit.test_momentum_v2 import NEXT, _TwoDayData

    params = MomentumV2Parameters(
        top_n=2, redeploy_next_session=True, exposure_fraction=Decimal("0.3")
    )
    policy = MomentumV2Policy(_TwoDayData(), params)
    policy.decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("50"), holdings=(_holding("INE467B01029", 100),)))
    )
    day2 = policy.decide(_ctx(NEXT, _FakeBroker(cash=Decimal("20000"))))
    spent = sum(o.quantity for o in day2.orders if o.side is Side.BUY) * 100
    assert spent <= 6000 + 20  # 30 % of a ~20,000 book, never the whole free cash


# ── the stricter monthly regime ────────────────────────────────────────────────────────────────


def _band_decision(level: str) -> list[Side]:
    reading = RegimeReading(
        index_level=Decimal(level), moving_average=Decimal("100"), knowable_date=SESSION
    )
    params = MomentumV2Parameters(top_n=2, regime_filter=True, regime_monthly_band=Decimal("0.02"))
    decision = MomentumV2Policy(_Data(_RECORDS, regime=reading), params).decide(
        _ctx(SESSION, _FakeBroker(cash=Decimal("100000"), holdings=(_holding("INE001A01036", 10),)))
    )
    return [o.side for o in decision.orders]


def test_the_band_parks_between_the_ma_and_the_band_inversion() -> None:
    # 101 is above D13's MA but below 1.02 x MA: the stricter regime parks (sells only).
    assert set(_band_decision("101")) == {Side.SELL}
    # 102 clears the band: invested (buys).
    assert Side.BUY in _band_decision("102")


# ── calibration and the analytic mix ──────────────────────────────────────────────────────────


def test_equal_exposure_fraction() -> None:
    assert equal_exposure_fraction(Decimal("0.4755"), Decimal("0.7509")) == Decimal("0.633")
    assert equal_exposure_fraction(Decimal("0.8"), Decimal("0.75")) is None  # >= 1: D13 itself
    with pytest.raises(StudyError):
        equal_exposure_fraction(Decimal("0.4"), Decimal("0"))


def test_tighter_vol_target_rounds_to_half_a_point() -> None:
    assert tighter_vol_target(Decimal("0.4755"), Decimal("0.709")) == Decimal("0.100")
    assert tighter_vol_target(Decimal("0.50"), Decimal("0.60")) == Decimal("0.125")
    with pytest.raises(StudyError, match="not below 15%"):
        tighter_vol_target(Decimal("0.7"), Decimal("0.6"))


class _FlatRepo:
    def earning_rate(self, day: date) -> Decimal:
        return Decimal("0.0365")


def test_mix_path_endpoints() -> None:
    nav = [(date(2020, 1, 1), Decimal("100")), (date(2020, 1, 2), Decimal("110"))]
    full = mix_path(nav, Decimal("1"), _FlatRepo())  # type: ignore[arg-type]
    assert full[-1][1] == Decimal("110")
    cash = mix_path(nav, Decimal("0"), _FlatRepo())  # type: ignore[arg-type]
    assert cash[-1][1] == Decimal("100") * (Decimal("1") + Decimal("0.0365") / Decimal("365"))
    half = mix_path(nav, Decimal("0.5"), _FlatRepo())  # type: ignore[arg-type]
    assert cash[-1][1] < half[-1][1] < full[-1][1]


def test_invested_fractions_from_ledger_and_nav(tmp_path: Path) -> None:
    digest = "d" * 64
    days = [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    ledger = RunLedger(
        source="synthetic",
        trades=(
            TaxTrade(
                isin="INE001A01036",
                trade_date=days[1],
                side=Side.BUY,
                quantity=60,
                net_amount=Decimal("600"),
                stt=Decimal("1"),
                stt_known=True,
            ),
        ),
        external_flows=(Cashflow(days[0], Decimal("-1000")),),
        terminal_date=days[2],
        terminal_nav=Decimal("1000"),
        terminal_prices={"INE001A01036": Decimal("10")},
    )
    path = ledger_path(tmp_path, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_run_ledger(ledger, path)
    write_nav(
        NavSeries(digest, "pre-tax", tuple((d, Decimal("1000")) for d in days)),
        nav_file(tmp_path, digest),
    )
    got = invested_fractions(tmp_path, digest)
    assert [f for _, f in got] == [Decimal("0"), Decimal("0.6"), Decimal("0.6")]
