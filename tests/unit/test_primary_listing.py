"""M3.2: primary-exchange selection and per-ISIN canonical dedup, asserted offline.

Three acceptance criteria, each pinned to tests that fail if the rule is wrong rather than merely
absent:

1. A dual-listed ISIN resolves to *one* primary, and the decision carries the rule and the per-
   exchange inputs that produced it.
2. Selection is stable over time — a day-to-day near-tie does not churn the primary, while a
   genuine, sustained shift in liquidity does flip it exactly once. The stability tests are written
   so that inverting the hysteresis logic (retaining vs switching) breaks them.
3. Deduping to the canonical daily series is a read-time projection: both exchanges' raw rows are
   still present after it runs, and the cross-exchange pair remains queryable.

No database, no network, no wall clock (the decision date is passed in as `as_of`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from dataplatform.identity.master import Exchange
from dataplatform.identity.primary import (
    DailyLiquidity,
    LiquidityMetric,
    NoLiquidityError,
    PrimaryRule,
    canonical_daily,
    select_primary,
    select_primary_map,
)

_ISIN = "INE009A01021"  # Infosys, genuinely dual-listed on NSE and BSE
_OTHER = "INE002A01018"  # Reliance
_D0 = date(2026, 1, 1)


def _days(start: date, n: int) -> list[date]:
    """`n` consecutive calendar dates from `start` — a stand-in session sequence for the tests."""
    return [start + timedelta(days=i) for i in range(n)]


def _obs(
    isin: str,
    exchange: Exchange,
    trade_date: date,
    turnover: str,
    *,
    volume: int = 0,
    trades: int = 0,
) -> DailyLiquidity:
    return DailyLiquidity(
        isin=isin,
        exchange=exchange,
        trade_date=trade_date,
        turnover=Decimal(turnover),
        volume=volume,
        trades=trades,
    )


# ── acceptance 1: one primary, with rule and inputs recorded ───────────────────────────────────


def test_dual_listed_resolves_to_one_primary_with_inputs_recorded() -> None:
    days = _days(_D0, 5)
    obs = [_obs(_ISIN, Exchange.NSE, d, "1000000") for d in days]
    obs += [_obs(_ISIN, Exchange.BSE, d, "50000") for d in days]

    decision = select_primary(_ISIN, obs, as_of=days[-1])

    assert decision.primary is Exchange.NSE
    # The rule is recorded on the decision, not implied by the code that made it.
    assert "median(TURNOVER)" in decision.rule
    assert decision.metric is LiquidityMetric.TURNOVER
    # Both exchanges' inputs are recorded, with the count each score was taken over.
    nse = decision.score_for(Exchange.NSE)
    bse = decision.score_for(Exchange.BSE)
    assert nse is not None and bse is not None
    assert nse.score == Decimal("1000000")
    assert bse.score == Decimal("50000")
    assert nse.observations == 5
    assert bse.observations == 5
    assert nse.score > bse.score  # the reason NSE won, made checkable


def test_liquidity_decides_not_recency_or_close() -> None:
    # BSE prints last and higher, but NSE trades far more value: liquidity wins.
    days = _days(_D0, 3)
    obs = [_obs(_ISIN, Exchange.NSE, d, "2000000", volume=100000) for d in days]
    obs += [_obs(_ISIN, Exchange.BSE, d, "10000", volume=100) for d in days]

    decision = select_primary(_ISIN, obs, as_of=days[-1])

    assert decision.primary is Exchange.NSE


def test_median_ignores_a_single_block_deal_spike() -> None:
    # BSE's mean turnover would beat NSE because of one huge day; its median does not.
    days = _days(_D0, 5)
    nse = [_obs(_ISIN, Exchange.NSE, d, "1000000") for d in days]
    bse = [_obs(_ISIN, Exchange.BSE, d, "100000") for d in days[:-1]]
    bse.append(_obs(_ISIN, Exchange.BSE, days[-1], "999999999"))  # one block deal

    decision = select_primary(_ISIN, nse + bse, as_of=days[-1])

    assert decision.primary is Exchange.NSE
    bse_score = decision.score_for(Exchange.BSE)
    assert bse_score is not None and bse_score.score == Decimal("100000")


def test_single_listed_isin_is_trivially_primary() -> None:
    days = _days(_D0, 3)
    obs = [_obs(_ISIN, Exchange.NSE, d, "500000") for d in days]

    decision = select_primary(_ISIN, obs, as_of=days[-1])

    assert decision.primary is Exchange.NSE
    assert decision.score_for(Exchange.BSE) is None


def test_tie_break_is_deterministic_and_documented() -> None:
    days = _days(_D0, 3)
    obs = [_obs(_ISIN, Exchange.NSE, d, "100000") for d in days]
    obs += [_obs(_ISIN, Exchange.BSE, d, "100000") for d in days]

    # Default order prefers NSE.
    assert select_primary(_ISIN, obs, as_of=days[-1]).primary is Exchange.NSE
    # A rule that prefers BSE flips a genuine tie the other way — the tie-break is real, not luck.
    bse_first = PrimaryRule(tie_break=(Exchange.BSE, Exchange.NSE))
    decision = select_primary(_ISIN, obs, as_of=days[-1], rule=bse_first)
    assert decision.primary is Exchange.BSE
    assert "tie-break" in decision.reason


def test_no_liquidity_in_window_raises_rather_than_guessing() -> None:
    old = [_obs(_ISIN, Exchange.NSE, _D0, "100000")]
    # as_of is before any observation: nothing is in the window.
    with pytest.raises(NoLiquidityError):
        select_primary(_ISIN, old, as_of=_D0 - timedelta(days=1))


def test_future_sessions_never_reach_the_decision() -> None:
    days = _days(_D0, 4)
    # NSE leads only on a future date; up to as_of, BSE is the sole/leading exchange.
    obs = [_obs(_ISIN, Exchange.BSE, d, "100000") for d in days[:2]]
    obs += [_obs(_ISIN, Exchange.NSE, days[3], "9999999")]  # after as_of

    decision = select_primary(_ISIN, obs, as_of=days[1])

    assert decision.primary is Exchange.BSE
    assert decision.score_for(Exchange.NSE) is None  # the future NSE row was invisible


def test_foreign_isin_in_observations_is_a_loud_error() -> None:
    obs = [_obs(_OTHER, Exchange.NSE, _D0, "100000")]
    with pytest.raises(ValueError, match="per ISIN"):
        select_primary(_ISIN, obs, as_of=_D0)


# ── acceptance 2: stability over time ──────────────────────────────────────────────────────────


def test_near_tie_does_not_churn_day_to_day() -> None:
    # Two exchanges oscillate within the 20% dead-band: NSE ahead one day, BSE the next.
    days = _days(_D0, 12)
    rule = PrimaryRule(lookback=1)  # window of one so the daily oscillation is fully exposed
    primary_by_day: list[Exchange] = []
    incumbent: Exchange | None = None

    for i, d in enumerate(days):
        nse_val, bse_val = ("105000", "100000") if i % 2 == 0 else ("100000", "104000")
        obs = [
            _obs(_ISIN, Exchange.NSE, d, nse_val),
            _obs(_ISIN, Exchange.BSE, d, bse_val),
        ]
        decision = select_primary(_ISIN, obs, as_of=d, rule=rule, incumbent=incumbent)
        incumbent = decision.primary
        primary_by_day.append(decision.primary)

    # Day 0 sets NSE; every later near-tie stays with the incumbent — one primary, no churn.
    assert primary_by_day[0] is Exchange.NSE
    assert set(primary_by_day) == {Exchange.NSE}
    assert all(d is Exchange.NSE for d in primary_by_day)


def test_decisive_sustained_shift_flips_exactly_once() -> None:
    days = _days(_D0, 8)
    rule = PrimaryRule(lookback=1)
    incumbent: Exchange | None = None
    flips: list[tuple[date, Exchange]] = []

    for i, d in enumerate(days):
        # First half NSE dominates; second half BSE dominates by 3x — well past the dead-band.
        if i < 4:
            obs = [_obs(_ISIN, Exchange.NSE, d, "300000"), _obs(_ISIN, Exchange.BSE, d, "100000")]
        else:
            obs = [_obs(_ISIN, Exchange.NSE, d, "100000"), _obs(_ISIN, Exchange.BSE, d, "300000")]
        decision = select_primary(_ISIN, obs, as_of=d, rule=rule, incumbent=incumbent)
        if decision.changed:
            flips.append((d, decision.primary))
        incumbent = decision.primary

    assert flips == [(days[4], Exchange.BSE)]  # flipped once, on the first decisive BSE day


def test_hysteresis_holds_a_challenger_just_inside_the_band() -> None:
    # Challenger BSE leads by exactly 20% — not *more* than the dead-band, so incumbent NSE holds.
    day = _D0
    rule = PrimaryRule(lookback=1, hysteresis=Decimal("0.20"))
    obs = [_obs(_ISIN, Exchange.NSE, day, "100000"), _obs(_ISIN, Exchange.BSE, day, "120000")]

    held = select_primary(_ISIN, obs, as_of=day, rule=rule, incumbent=Exchange.NSE)
    assert held.primary is Exchange.NSE
    assert held.changed is False

    # One rupee past the band flips it — the boundary is exactly where the rule says.
    obs_over = [_obs(_ISIN, Exchange.NSE, day, "100000"), _obs(_ISIN, Exchange.BSE, day, "120001")]
    flipped = select_primary(_ISIN, obs_over, as_of=day, rule=rule, incumbent=Exchange.NSE)
    assert flipped.primary is Exchange.BSE
    assert flipped.changed is True


def test_incumbent_that_stops_trading_is_replaced() -> None:
    days = _days(_D0, 3)
    # NSE was primary but only BSE trades in the current window.
    obs = [_obs(_ISIN, Exchange.BSE, d, "100000") for d in days]

    decision = select_primary(_ISIN, obs, as_of=days[-1], incumbent=Exchange.NSE)

    assert decision.primary is Exchange.BSE
    assert decision.changed is True
    assert "no qualifying session" in decision.reason


def test_first_selection_has_no_incumbent_and_is_not_a_change() -> None:
    obs = [_obs(_ISIN, Exchange.NSE, _D0, "100000"), _obs(_ISIN, Exchange.BSE, _D0, "50000")]
    decision = select_primary(_ISIN, obs, as_of=_D0, incumbent=None)
    assert decision.incumbent is None
    assert decision.changed is False


# ── acceptance 3: both exchanges' raw rows remain queryable; dedup is a read-time view ──────────


@dataclass(frozen=True, slots=True)
class _RawRow:
    """A minimal raw price row — enough for dedup, standing in for a `prices_raw` record."""

    isin: str
    exchange: Exchange
    trade_date: date
    close: Decimal


def test_canonical_series_picks_the_primary_row_per_day() -> None:
    days = _days(_D0, 3)
    rows = [_RawRow(_ISIN, Exchange.NSE, d, Decimal("100")) for d in days] + [
        _RawRow(_ISIN, Exchange.BSE, d, Decimal("101")) for d in days
    ]

    canon = canonical_daily(rows, {_ISIN: Exchange.NSE})

    assert len(canon) == 3  # one row per (isin, date), not two
    assert all(c.exchange is Exchange.NSE for c in canon)
    assert all(not c.fell_back for c in canon)


def test_dedup_does_not_drop_or_mutate_the_raw_rows() -> None:
    days = _days(_D0, 2)
    rows = [
        _RawRow(_ISIN, Exchange.NSE, days[0], Decimal("100")),
        _RawRow(_ISIN, Exchange.BSE, days[0], Decimal("101")),
        _RawRow(_ISIN, Exchange.NSE, days[1], Decimal("102")),
        _RawRow(_ISIN, Exchange.BSE, days[1], Decimal("103")),
    ]
    before = list(rows)

    canonical_daily(rows, {_ISIN: Exchange.NSE})

    # The raw collection is untouched — both exchanges' prints for every day remain queryable.
    assert rows == before
    both = {(r.exchange, r.trade_date) for r in rows}
    assert both == {
        (Exchange.NSE, days[0]),
        (Exchange.BSE, days[0]),
        (Exchange.NSE, days[1]),
        (Exchange.BSE, days[1]),
    }


def test_canonical_falls_back_when_primary_did_not_trade() -> None:
    days = _days(_D0, 2)
    # NSE is primary but only prints on day 0; BSE prints both days.
    rows = [
        _RawRow(_ISIN, Exchange.NSE, days[0], Decimal("100")),
        _RawRow(_ISIN, Exchange.BSE, days[0], Decimal("101")),
        _RawRow(_ISIN, Exchange.BSE, days[1], Decimal("103")),
    ]

    canon = canonical_daily(rows, {_ISIN: Exchange.NSE})
    by_date = {c.trade_date: c for c in canon}

    assert by_date[days[0]].exchange is Exchange.NSE
    assert by_date[days[0]].fell_back is False
    assert by_date[days[1]].exchange is Exchange.BSE  # fell back to the only exchange that traded
    assert by_date[days[1]].fell_back is True
    assert by_date[days[1]].primary is Exchange.NSE  # the primary is still recorded as NSE


def test_canonical_carries_the_chosen_row_through() -> None:
    row_nse = _RawRow(_ISIN, Exchange.NSE, _D0, Decimal("100"))
    row_bse = _RawRow(_ISIN, Exchange.BSE, _D0, Decimal("101"))

    (canon,) = canonical_daily([row_nse, row_bse], {_ISIN: Exchange.NSE})

    assert canon.row is row_nse  # the canonical close comes from NSE, not BSE
    assert canon.row.close == Decimal("100")


def test_canonical_needs_a_primary_for_every_isin() -> None:
    rows = [_RawRow(_ISIN, Exchange.NSE, _D0, Decimal("100"))]
    with pytest.raises(ValueError, match="no primary exchange"):
        canonical_daily(rows, {})


def test_duplicate_raw_row_for_one_session_is_a_contradiction() -> None:
    rows = [
        _RawRow(_ISIN, Exchange.NSE, _D0, Decimal("100")),
        _RawRow(_ISIN, Exchange.NSE, _D0, Decimal("999")),
    ]
    with pytest.raises(ValueError, match="one session is one row"):
        canonical_daily(rows, {_ISIN: Exchange.NSE})


def test_canonical_output_is_sorted_and_stable() -> None:
    days = _days(_D0, 2)
    rows = [
        _RawRow(_OTHER, Exchange.NSE, days[1], Decimal("50")),
        _RawRow(_ISIN, Exchange.NSE, days[0], Decimal("100")),
        _RawRow(_ISIN, Exchange.NSE, days[1], Decimal("102")),
        _RawRow(_OTHER, Exchange.NSE, days[0], Decimal("48")),
    ]
    canon = canonical_daily(rows, {_ISIN: Exchange.NSE, _OTHER: Exchange.NSE})
    keys = [(c.isin, c.trade_date) for c in canon]
    assert keys == sorted(keys)


# ── the bulk map entry point ───────────────────────────────────────────────────────────────────


def test_select_primary_map_decides_each_isin_and_carries_incumbents() -> None:
    days = _days(_D0, 3)
    obs = []
    for d in days:
        obs += [_obs(_ISIN, Exchange.NSE, d, "1000000"), _obs(_ISIN, Exchange.BSE, d, "50000")]
        obs += [_obs(_OTHER, Exchange.BSE, d, "800000"), _obs(_OTHER, Exchange.NSE, d, "40000")]

    decisions = select_primary_map(obs, as_of=days[-1])

    assert decisions[_ISIN].primary is Exchange.NSE
    assert decisions[_OTHER].primary is Exchange.BSE


def test_select_primary_map_skips_an_isin_with_no_liquidity() -> None:
    # _ISIN has liquidity in the window; _OTHER only has a future print.
    obs = [
        _obs(_ISIN, Exchange.NSE, _D0, "100000"),
        _obs(_OTHER, Exchange.NSE, _D0 + timedelta(days=10), "100000"),
    ]
    decisions = select_primary_map(obs, as_of=_D0)
    assert _ISIN in decisions
    assert _OTHER not in decisions  # skipped, not fatal


def test_bad_rule_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="lookback"):
        PrimaryRule(lookback=0)
    with pytest.raises(ValueError, match="hysteresis"):
        PrimaryRule(hysteresis=Decimal("-0.1"))
    with pytest.raises(ValueError, match="min_observations"):
        PrimaryRule(min_observations=0)
    with pytest.raises(ValueError, match="tie_break"):
        PrimaryRule(tie_break=())
