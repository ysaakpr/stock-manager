"""X2: no swing lookback return straddles the L2 seam on mixed price bases.

L2's adjusted series starts on 2016-09-02. Before it the swing features read raw closes, so a
return reaching back across the seam divided an adjusted price by a raw one: HPCL read ₹1,210.40
raw on 2016-09-01 and ₹182.47 adjusted on 2016-09-02 (three bonuses later in its life, cumulative
price factor 4/27), and 307 of the 1,515 names printing that day jumped the same way.

These tests build a synthetic seam in an offline lake — raw L1 throughout, L2 materialized by the
lake's own writer and then cut off at the seam — and read the features on a date whose 252-session
window straddles it. Every name's *true* adjusted price is flat, so every consistent lookback
return is exactly zero:

* ``FUTURE`` splits 1:10 *after* the seam — the HPCL shape: L2's first row already carries the
  0.1 factor, so raw-before / adjusted-after is a fake -90 % 12-1 return.
* ``PAST`` splits 1:2 *before* the seam, inside the window — the raw series itself steps, and L2's
  first row carries factor 1.
* ``NO_L2`` has no L2 at all and splits inside the window.

With the split factors supplied, every one reads 0; without them the straddling windows are excluded
rather than scored. Revert the feature query to ``COALESCE(adj_close, close)`` and ``FUTURE`` reads
-90 %.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from backtest.book_actions import (
    BookActionCalendar,
    RescaleKind,
    ShareRescale,
    current_signal_split_factors,
    signal_split_factors,
)
from backtest.run import _SwingFeatures
from dataplatform.corpactions.factors import FactorChain, build_factor_chain
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.l2 import materialize_isin
from tests.unit.test_regime_published import write_l1

FUTURE = "INE094A01015"
PAST = "INE002A01018"
NO_L2 = "INE009A01021"
STEADY = "INE040A01034"


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSIONS = _weekdays(date(2019, 1, 1), 300)
SEAM = SESSIONS[200]  # L2 starts here; a decision on the last session looks back across it
DECISION = SESSIONS[-1]
FUTURE_EX = SESSIONS[250]  # after the seam: the HPCL shape
PAST_EX = SESSIONS[150]  # before the seam, inside the 252-session window
NO_L2_EX = SESSIONS[120]


def _raw(isin: str, session: date) -> Decimal:
    if isin == FUTURE:
        return Decimal("1000") if session < FUTURE_EX else Decimal("100")
    if isin == PAST:
        return Decimal("2000") if session < PAST_EX else Decimal("1000")
    if isin == NO_L2:
        return Decimal("500") if session < NO_L2_EX else Decimal("250")
    return Decimal("50")


def _split(isin: str, ex_date: date, from_fv: str, to_fv: str) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(from_fv), to_value=Decimal(to_fv)),
        source="nse_corp_actions",
        raw_text=f"FV SPLIT FROM RS.{from_fv}/- TO RS.{to_fv}/-",
        knowable_date=ex_date,
    )


_ACTIONS = {
    FUTURE: _split(FUTURE, FUTURE_EX, "10", "1"),
    PAST: _split(PAST, PAST_EX, "10", "5"),
}

#: The same three splits as the book sees them (``backtest.book_actions`` terms).
_RESCALES = BookActionCalendar(
    [
        ShareRescale(FUTURE, FUTURE_EX, RescaleKind.SPLIT, Decimal("10"), Decimal("1")),
        ShareRescale(PAST, PAST_EX, RescaleKind.SPLIT, Decimal("10"), Decimal("5")),
        ShareRescale(NO_L2, NO_L2_EX, RescaleKind.SPLIT, Decimal("10"), Decimal("5")),
    ]
)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for session in SESSIONS:
        write_l1(
            tmp_path,
            session,
            [(isin, _raw(isin, session), None) for isin in (FUTURE, PAST, NO_L2, STEADY)],
        )
    for isin in (FUTURE, PAST, STEADY):
        action = _ACTIONS.get(isin)
        materialize_isin(
            isin,
            chain=build_factor_chain((action,)) if action else FactorChain(isin=isin, rows=()),
            actions=(action,) if action else (),
            data_root=tmp_path,
        )
    # Cut L2 off at the seam, as the real lake's L2 begins on 2016-09-02.
    for path in tmp_path.glob("L2/prices_adjusted/isin=*/*.parquet"):
        table = pq.read_table(path)
        pq.write_table(table.filter(pc.field("trade_date") >= SEAM), path)
    return tmp_path


def _features(lake: Path, factors: BookActionCalendar | None) -> dict[str, object]:
    with signal_split_factors(factors):
        features = _SwingFeatures(
            data_root=lake, adjusted=True, split_factors=current_signal_split_factors()
        )
    try:
        features.load([DECISION])
        return {record.isin: record for record in features.records(DECISION)}
    finally:
        features.close()


def test_the_fixture_s_l2_starts_at_the_seam_and_carries_the_future_factor(lake: Path) -> None:
    table = pq.read_table(next(lake.glob(f"L2/prices_adjusted/isin={FUTURE}/*.parquet")))
    first = table.sort_by("trade_date").slice(0, 1).to_pylist()[0]
    assert first["trade_date"] == SEAM
    assert first["cum_price_factor"] == Decimal("0.1")


def test_with_split_factors_every_window_across_the_seam_is_on_one_basis(lake: Path) -> None:
    """Every true adjusted series is flat, so every lookback return and proximity is exact."""
    records = _features(lake, _RESCALES)
    assert set(records) == {FUTURE, PAST, NO_L2, STEADY}
    for isin, record in records.items():
        assert record.momentum_12_1 == Decimal("0"), isin  # type: ignore[attr-defined]
        assert record.high_proximity == Decimal("1"), isin  # type: ignore[attr-defined]
        assert record.volatility == Decimal("0"), isin  # type: ignore[attr-defined]
    # The price the book sizes against is still the raw close (invariant #3).
    assert records[FUTURE].price == Decimal("100")  # type: ignore[attr-defined]


def test_without_split_factors_a_window_across_the_seam_is_excluded_not_mixed(lake: Path) -> None:
    """No factor source: an L2 name's pre-seam rows are unusable, so its 12-1 is not computable.

    The name with no L2 at all has no seam and stays, on its raw series (the pre-X2 read).
    """
    records = _features(lake, None)
    assert FUTURE not in records
    assert PAST not in records
    assert STEADY not in records
    assert set(records) == {NO_L2}


def test_the_forecast_features_read_the_same_one_basis_series(lake: Path) -> None:
    """The fitted-forecast cursor had the same ``COALESCE`` seam; it now shares the rule."""
    from backtest.forecast import HORIZON_3M
    from backtest.forecast_run import _FeatureCursor

    with signal_split_factors(_RESCALES):
        cursor = _FeatureCursor(
            horizon=HORIZON_3M, data_root=lake, start=DECISION, end=DECISION, adjusted=True
        )
    try:
        rows = {row.isin: row for row in cursor.take(DECISION)}
    finally:
        cursor.close()
    assert set(rows) == {FUTURE, PAST, NO_L2, STEADY}
    for isin, row in rows.items():
        assert row.mom_12_1 is not None and abs(row.mom_12_1) < 1e-12, isin
        assert row.high_prox is not None and abs(row.high_prox - 1) < 1e-12, isin
