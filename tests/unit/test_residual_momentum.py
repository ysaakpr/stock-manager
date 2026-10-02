"""Round 2, H1: residual momentum, as ops/studies/preregistration-signals-2026-09-29.md §3 fixes it.

The pure score first — a name that is exactly beta x market has nothing idiosyncratic and scores ~0,
an idiosyncratic climb scores positive and a decline negative (so an inverted sign fails), and a
name short of 200 valid sessions in the t-252 .. t-21 span is excluded. Then the point-in-time
boundary: the regression window ends at t-1, so nothing dated the decision session or later can move
a score. Then the wiring: the ranking gives an excluded name the leg's mean, the H1 arm is the M10.7
default with exactly its 12-1 leg swapped, every other arm keeps its run digest, and an offline lake
carries the leg end to end on the seam-consistent price path.
"""

from __future__ import annotations

import math
import random
from dataclasses import fields, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.policies.residual_momentum import (
    MIN_VALID_SESSIONS,
    REGRESSION_SESSIONS,
    SKIP_SESSIONS,
    MarketSession,
    ResidualMomentumError,
    ResidualMomentumPanel,
    residual_momentum,
    with_residual_momentum,
)
from backtest.policies.swing_composite import (
    SwingCompositeParameters,
    SwingRecord,
    composite_scores,
)
from backtest.run import BacktestError, open_swing_lake, run_swing_composite
from backtest.sweep import ARMS, H1_RESIDUAL_MOMENTUM
from dataplatform.corpactions.factors import FactorChain
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, TriPoint, TriSeries, write_tri_l1
from dataplatform.store.l2 import materialize_isin
from tests.unit.test_regime_published import write_l1

_SPAN = REGRESSION_SESSIONS - SKIP_SESSIONS


def _market(seed: int = 7) -> list[float | None]:
    rng = random.Random(seed)
    return [rng.gauss(0.0004, 0.011) for _ in range(REGRESSION_SESSIONS)]


def _noise(seed: int, scale: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, scale) for _ in range(REGRESSION_SESSIONS)]


# ── the pure score ───────────────────────────────────────────────────────────────────────────────


def test_the_pre_registered_constants() -> None:
    """§3 fixes these; a change here is a new hypothesis, not a tweak."""
    assert (REGRESSION_SESSIONS, SKIP_SESSIONS, MIN_VALID_SESSIONS) == (252, 20, 200)
    assert _SPAN == 232  # t-252 .. t-21 inclusive


def test_a_name_that_is_exactly_beta_times_the_market_scores_zero() -> None:
    market = _market()
    for beta in (0.0, 0.6, 1.0, 1.7):
        stock = [beta * m for m in market if m is not None]
        score = residual_momentum(stock, market)
        assert score is not None
        assert abs(score) < Decimal("1e-6"), (beta, score)


def test_an_idiosyncratic_climb_scores_positive_and_a_decline_negative() -> None:
    """The sign test: invert the score (or fit with an intercept, which absorbs the drift) and this
    fails."""
    market = _market()
    noise = _noise(11)
    up = [1.2 * m + 0.002 + e for m, e in zip(market, noise, strict=True) if m is not None]
    down = [1.2 * m - 0.002 + e for m, e in zip(market, noise, strict=True) if m is not None]
    flat = [1.2 * m + e for m, e in zip(market, noise, strict=True) if m is not None]
    up_score, down_score, flat_score = (residual_momentum(s, market) for s in (up, down, flat))
    assert up_score is not None and down_score is not None and flat_score is not None
    assert up_score > Decimal("2")
    assert down_score < Decimal("-2")
    assert down_score < flat_score < up_score


def test_the_market_s_part_of_a_high_beta_name_s_return_is_not_momentum() -> None:
    """In a rising market plain 12-1 ranks the high-beta name first; residual momentum does not."""
    rng = random.Random(3)
    market: list[float | None] = [rng.gauss(0.003, 0.008) for _ in range(REGRESSION_SESSIONS)]
    noise = _noise(5, 0.004)
    high_beta = [2.0 * m + e for m, e in zip(market, noise, strict=True) if m is not None]
    low_beta_trend = [
        0.5 * m + 0.001 + e for m, e in zip(market, noise, strict=True) if m is not None
    ]
    assert sum(high_beta[:_SPAN]) > sum(low_beta_trend[:_SPAN])  # 12-1 prefers the high beta
    hb, lb = residual_momentum(high_beta, market), residual_momentum(low_beta_trend, market)
    assert hb is not None and lb is not None
    assert lb > hb


def test_the_last_twenty_sessions_are_fitted_but_not_summed() -> None:
    """A burst in t-20 .. t-1 is the skipped month: fitted, never summed.

    The market is flat on the two sessions touched, so the burst cannot move beta and the only way
    it reaches the score is through the sum — which it must not when it lands on t-20.
    """
    market = _market()
    market[_SPAN - 1] = market[_SPAN] = 0.0
    base = [0.9 * m + e for m, e in zip(market, _noise(2), strict=True) if m is not None]
    burst = list(base)
    burst[_SPAN] += 0.30  # session t-20: inside the regression, outside the sum
    moved = list(base)
    moved[_SPAN - 1] += 0.30  # session t-21: the span's last session
    b, s, m = (residual_momentum(x, market) for x in (base, burst, moved))
    assert b is not None and s is not None and m is not None
    assert s == b
    assert m - b > Decimal("0.9")


def test_fewer_than_200_valid_sessions_in_the_span_is_excluded() -> None:
    market = _market()
    stock: list[float | None] = [
        0.8 * m + e for m, e in zip(market, _noise(4), strict=True) if m is not None
    ]
    # 199 valid in the span, every one of the 20 skipped sessions valid: still excluded.
    short = [None if i < _SPAN - (MIN_VALID_SESSIONS - 1) else r for i, r in enumerate(stock)]
    assert sum(1 for r in short[:_SPAN] if r is not None) == MIN_VALID_SESSIONS - 1
    assert residual_momentum(short, market) is None
    enough = [None if i < _SPAN - MIN_VALID_SESSIONS else r for i, r in enumerate(stock)]
    assert residual_momentum(enough, market) is not None
    # A NaN is not a valid session either.
    nan = [math.nan if r is None else r for r in short]
    assert residual_momentum(nan, market) is None


def test_a_window_that_is_not_252_sessions_is_refused() -> None:
    market = _market()
    with pytest.raises(ResidualMomentumError, match="exactly 252"):
        residual_momentum([0.0] * 253, [*market, 0.0])
    with pytest.raises(ResidualMomentumError, match="exactly 252"):
        residual_momentum([0.0] * 251, market[:-1])


def test_a_market_that_never_moved_defines_no_beta() -> None:
    zero: list[float | None] = [0.0] * REGRESSION_SESSIONS
    assert residual_momentum([0.01] * REGRESSION_SESSIONS, zero) is None


# ── the panel: sessions, and the point-in-time boundary ──────────────────────────────────────────


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


CALENDAR = _weekdays(date(2012, 1, 2), 320)
DECISION = CALENDAR[300]


def _levels(seed: int = 1) -> list[Decimal]:
    rng = random.Random(seed)
    level, out = 10000.0, []
    for _ in CALENDAR:
        out.append(Decimal(str(round(level, 4))))
        level *= math.exp(rng.gauss(0.0004, 0.01))
    return out


def _panel(knowable_lag: int = 0) -> ResidualMomentumPanel:
    return ResidualMomentumPanel(
        [
            MarketSession(day, level, CALENDAR[min(i + knowable_lag, len(CALENDAR) - 1)])
            for i, (day, level) in enumerate(zip(CALENDAR, _levels(), strict=True))
        ]
    )


def _closes(beta: float, drift: float, seed: int) -> dict[date, float | None]:
    """A name's closes on the calendar: beta x the market's log return + drift + noise."""
    levels, rng = _levels(), random.Random(seed)
    px = 100.0
    out: dict[date, float | None] = {CALENDAR[0]: px}
    for day, prev, cur in zip(CALENDAR[1:], levels, levels[1:], strict=False):
        px *= math.exp(beta * math.log(float(cur / prev)) + drift + rng.gauss(0.0, 0.01))
        out[day] = px
    return out


def test_the_window_is_the_253_market_closes_strictly_before_the_decision() -> None:
    """Fails if the regression window reaches session t or later."""
    window = _panel().window(DECISION)
    assert len(window) == REGRESSION_SESSIONS + 1
    assert window[-1] == CALENDAR[299] < DECISION
    assert window[0] == CALENDAR[300 - REGRESSION_SESSIONS - 1]
    assert all(day < DECISION for day in window)


def test_nothing_dated_the_decision_session_or_later_moves_a_score() -> None:
    """Crash the name on t and after: an honest score cannot see it. Move the window to end at t
    (``bisect_right``) and this fails."""
    panel = _panel()
    closes = _closes(1.1, 0.002, 9)
    crashed = {
        day: (px / 10 if day >= DECISION and px is not None else px) for day, px in closes.items()
    }
    assert panel.score(DECISION, closes) == panel.score(DECISION, crashed)
    # ...while the same crash one session earlier is inside the window and does move it.
    earlier = {
        day: (px / 10 if day >= CALENDAR[299] and px is not None else px)
        for day, px in closes.items()
    }
    assert panel.score(DECISION, closes) != panel.score(DECISION, earlier)


def test_a_market_level_not_knowable_on_the_decision_date_is_refused() -> None:
    with pytest.raises(ResidualMomentumError, match="not knowable"):
        _panel(knowable_lag=2).window(DECISION)


def test_too_short_a_market_history_is_refused() -> None:
    with pytest.raises(ResidualMomentumError, match="short of the 253"):
        _panel().window(CALENDAR[252])


def test_the_panel_reads_the_market_s_sessions_and_scores_a_trend_positive() -> None:
    panel = _panel()
    scores = panel.scores(
        DECISION,
        {
            "TREND": _closes(1.3, 0.003, 21),
            "BETA": _closes(1.3, 0.0, 21),
            "FALL": _closes(1.3, -0.003, 21),
        },
    )
    trend, beta, fall = scores["TREND"], scores["BETA"], scores["FALL"]
    assert trend is not None and beta is not None and fall is not None
    assert fall < beta < trend
    assert trend > Decimal("2")


def test_a_name_that_stopped_printing_is_excluded_on_the_market_calendar() -> None:
    """A 60-session suspension inside the span leaves < 200 valid sessions: excluded, not scored."""
    closes = _closes(1.0, 0.002, 3)
    suspended = {
        day: (None if CALENDAR[120] <= day < CALENDAR[180] else px) for day, px in closes.items()
    }
    assert _panel().score(DECISION, suspended) is None
    assert _panel().score(DECISION, closes) is not None


# ── the composite and the arm ────────────────────────────────────────────────────────────────────


def _record(isin: str, residual: Decimal | None, momentum: str = "0.1") -> SwingRecord:
    return SwingRecord(
        isin=isin,
        high_proximity=Decimal("0.9"),
        delivery_share=Decimal("0.5"),
        momentum_12_1=Decimal(momentum),
        volatility=Decimal("0.02"),
        price=Decimal("100"),
        knowable_date=DECISION,
        residual_momentum=residual,
    )


_RESIDUAL_ONLY = SwingCompositeParameters(
    weight_high=Decimal(0),
    weight_delivery=Decimal(0),
    weight_momentum=Decimal(0),
    weight_residual_momentum=Decimal(1),
)


def test_the_residual_leg_ranks_high_scores_first_and_gives_an_excluded_name_the_mean() -> None:
    records = [
        _record("A", Decimal("3")),
        _record("B", Decimal("-1")),
        _record("C", None),
        _record("D", Decimal("0.5")),
    ]
    scores = composite_scores(records, _RESIDUAL_ONLY)
    # A, D, B rank among themselves (n = 3); C is excluded from the leg and scores its mean, 0.
    assert scores == {"A": Decimal("0.5"), "B": Decimal("-0.5"), "C": Decimal(0), "D": Decimal(0)}


def test_the_residual_leg_is_ignored_at_zero_weight() -> None:
    records = [_record("A", Decimal("3"), "0.1"), _record("B", None, "0.2")]
    baseline = SwingCompositeParameters()
    assert composite_scores(records, baseline) == composite_scores(
        [replace(r, residual_momentum=None) for r in records], baseline
    )


def test_with_residual_momentum_swaps_the_12_1_leg_and_nothing_else() -> None:
    base = SwingCompositeParameters(regime_filter=True, weight_momentum=Decimal("2"))
    h1 = with_residual_momentum(base)
    assert h1.weight_momentum == Decimal(0)
    assert h1.weight_residual_momentum == Decimal("2")
    changed = {f.name for f in fields(base) if getattr(base, f.name) != getattr(h1, f.name)}
    assert changed == {"weight_momentum", "weight_residual_momentum"}


def test_the_h1_arm_is_the_m10_7_composite_with_residual_momentum() -> None:
    arms = {arm.label: arm for arm in ARMS}
    h1, m10_7 = arms[H1_RESIDUAL_MOMENTUM], arms["Swing composite (M10.7)"]
    assert h1.reference == m10_7.label
    assert h1.swing == with_residual_momentum(SwingCompositeParameters())
    assert m10_7.swing is not None and m10_7.swing.weight_residual_momentum == Decimal(0)
    # The sign: a positive weight is residual momentum; a negative one would be its reversal.
    assert h1.swing is not None and h1.swing.weight_residual_momentum > 0


def test_an_arm_that_does_not_weight_the_leg_keeps_its_run_digest() -> None:
    """The run ledger keys a run on ``repr(parameters)``: the new field must not appear at zero."""
    params = SwingCompositeParameters()
    old = ", ".join(
        f"{f.name}={getattr(params, f.name)!r}"
        for f in fields(params)
        if f.name != "weight_residual_momentum"
    )
    assert repr(params) == f"SwingCompositeParameters({old})"
    assert "weight_residual_momentum=Decimal('1')" in repr(with_residual_momentum(params))


# ── end to end on an offline lake ────────────────────────────────────────────────────────────────

TREND = "INE002A01018"
BETA = "INE009A01021"
SHORT = "INE040A01034"
LAKE_DAYS = CALENDAR[:302]


def _lake_market() -> TriSeries:
    return TriSeries(
        index_slug="nifty50",
        index_name="Nifty 50",
        method=TRI_METHOD_PUBLISHED,
        points=tuple(
            TriPoint(
                index_slug="nifty50",
                index_name="Nifty 50",
                as_of=day,
                tri_value=level,
                method=TRI_METHOD_PUBLISHED,
            )
            for day, level in zip(CALENDAR, _levels(), strict=True)
        ),
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """Three names on 302 sessions: a trend, a pure-beta name, and one listed for 150 sessions."""
    trend, beta = _closes(1.2, 0.003, 31), _closes(1.2, 0.0, 31)
    for i, day in enumerate(LAKE_DAYS):
        rows: list[tuple[str, Decimal, float | None]] = [
            (TREND, Decimal(str(round(100 * trend[day], 2))), None),  # type: ignore[operator]
            (BETA, Decimal(str(round(100 * beta[day], 2))), None),  # type: ignore[operator]
        ]
        if i >= len(LAKE_DAYS) - 150:
            rows.append((SHORT, Decimal("250"), None))
        write_l1(tmp_path, day, rows)
    write_tri_l1(_lake_market(), data_root=tmp_path)
    # BETA reads through L2 (an identity chain), TREND off raw: both halves of the seam-consistent
    # price path the leg shares with every other swing feature.
    materialize_isin(BETA, chain=FactorChain(isin=BETA, rows=()), actions=(), data_root=tmp_path)
    return tmp_path


def test_the_swing_lake_carries_the_leg_on_every_record(lake: Path) -> None:
    swing = open_swing_lake(
        start=LAKE_DAYS[0], end=LAKE_DAYS[-1], floors=[], data_root=lake, residual_momentum=True
    )
    try:
        swing.features.load([DECISION])
        by_isin = {r.isin: r for r in swing.features.records(DECISION)}
        assert set(by_isin) == {TREND, BETA}  # SHORT is short of the feature history anyway
        trend, beta = by_isin[TREND].residual_momentum, by_isin[BETA].residual_momentum
        assert trend is not None and beta is not None
        assert trend > beta
        assert trend > Decimal("2")
        assert swing.features.residual_momentum
    finally:
        swing.close()


def test_a_lake_opened_without_the_leg_carries_none(lake: Path) -> None:
    swing = open_swing_lake(start=LAKE_DAYS[0], end=LAKE_DAYS[-1], floors=[], data_root=lake)
    try:
        swing.features.load([DECISION])
        assert all(r.residual_momentum is None for r in swing.features.records(DECISION))
    finally:
        swing.close()


def test_an_h1_run_on_a_lake_without_the_leg_fails_loud(lake: Path) -> None:
    swing = open_swing_lake(start=LAKE_DAYS[0], end=LAKE_DAYS[-1], floors=[], data_root=lake)
    try:
        with pytest.raises(BacktestError, match="residual momentum"):
            run_swing_composite(
                start=LAKE_DAYS[0],
                end=LAKE_DAYS[-1],
                parameters=with_residual_momentum(SwingCompositeParameters()),
                data_root=lake,
                lake=swing,
            )
    finally:
        swing.close()


def test_the_leg_needs_the_published_index(tmp_path: Path) -> None:
    write_l1(tmp_path, LAKE_DAYS[0], [(TREND, Decimal("100"), None)])
    with pytest.raises(BacktestError):
        open_swing_lake(
            start=LAKE_DAYS[0],
            end=LAKE_DAYS[0],
            floors=[],
            data_root=tmp_path,
            residual_momentum=True,
        )
