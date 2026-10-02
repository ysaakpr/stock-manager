"""X2: the regime gate reads the published NIFTY 50, point-in-time, and never falls back to a proxy.

Until X2 the regime index was an equal-weight basket of the fifty most-liquid names on the run's
first session, built from raw L1 closes — so a split in one basket name halved its price relative
overnight and could flip the gate on a corporate action. These tests build a tiny offline lake:
raw L1 partitions in which one name splits 1:10, and a published NIFTY 50 series that rises
steadily. The gate must read the published level (risk-on, at the published value), see nothing
knowable after the decision date, and raise — not quietly build the basket — when the published
series is missing or does not reach the date.

Offline: every file is written into ``tmp_path`` with the lake's own writers and schemas.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.run import RegimeSourceError, _RegimeSource, open_swing_lake
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, TriPoint, TriSeries, write_tri_l1
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

_Q = Decimal("0.01")
SPLITTER = "INE002A01018"
STEADY = "INE009A01021"


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSIONS = _weekdays(date(2020, 1, 1), 12)
SPLIT_ON = SESSIONS[8]
#: The published series reaches back well before L1 (the real one starts 1999-06-30), far enough to
#: strike the lake's 200-session mean on the first L1 session.
HISTORY = [day for day in _weekdays(date(2019, 1, 1), 400) if day <= SESSIONS[-1]]


def write_l1(root: Path, session: date, rows: list[tuple[str, Decimal, float | None]]) -> None:
    """One raw NSE ``prices_raw`` partition — rows are ``(isin, close, deliv_pct)``."""
    volume = 100_000
    records = [
        {
            "isin": isin,
            "exchange": "NSE",
            "symbol": isin[:6],
            "series": "EQ",
            "trade_date": session,
            "open": close.quantize(_Q),
            "high": close.quantize(_Q),
            "low": close.quantize(_Q),
            "close": close.quantize(_Q),
            "last": close.quantize(_Q),
            "prev_close": close.quantize(_Q),
            "total_traded_qty": volume,
            "total_traded_value": (close * volume).quantize(_Q),
            "total_trades": volume,
            "deliv_qty": None if deliv is None else int(volume * deliv / 100),
            "deliv_pct": deliv,
        }
        for isin, close, deliv in rows
    ]
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA), path)


def _published(sessions: list[date], *, start: str = "10000", step: str = "10") -> TriSeries:
    level = Decimal(start)
    points = []
    for session in sessions:
        points.append(
            TriPoint(
                index_slug="nifty50",
                index_name="Nifty 50",
                as_of=session,
                tri_value=level,
                method=TRI_METHOD_PUBLISHED,
            )
        )
        level += Decimal(step)
    return TriSeries(
        index_slug="nifty50",
        index_name="Nifty 50",
        method=TRI_METHOD_PUBLISHED,
        points=tuple(points),
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """Two names; SPLITTER splits 1:10 on ``SPLIT_ON``. The published index rises every session."""
    for session in SESSIONS:
        splitter = Decimal("1000") if session < SPLIT_ON else Decimal("100")
        write_l1(tmp_path, session, [(SPLITTER, splitter, None), (STEADY, Decimal("50"), None)])
    write_tri_l1(_published(HISTORY), data_root=tmp_path)
    return tmp_path


def test_the_reading_is_the_published_level_and_its_trailing_mean() -> None:
    source = _RegimeSource(_published(SESSIONS), ma_days=3)
    reading = source.reading(SESSIONS[5])
    assert reading.index_level == Decimal("10050")
    assert reading.moving_average == Decimal("10040")  # mean of 10030, 10040, 10050
    assert reading.risk_on
    assert reading.knowable_date == SESSIONS[5]
    assert source.basis == "tri"  # the lake's published points carry no price-index level


def test_no_level_knowable_after_the_decision_enters_the_mean() -> None:
    """Invariant #7: a crash in the series after the decision date cannot touch the reading."""
    crash = _published(SESSIONS)
    later = tuple(
        point
        if point.as_of <= SESSIONS[5]
        else point.model_copy(update={"tri_value": Decimal("1")})
        for point in crash.points
    )
    source = _RegimeSource(crash.model_copy(update={"points": later}), ma_days=3)
    assert source.reading(SESSIONS[5]).moving_average == Decimal("10040")


def test_the_swing_lake_s_gate_reads_the_published_index_not_the_l1_basket(lake: Path) -> None:
    """The split halves-and-more the L1 basket (risk-off, level far below 1000); NIFTY rose.

    Revert ``open_swing_lake`` to the L1 proxy basket and the reading here is risk-off at a proxy
    level near 550, and this fails.
    """
    swing = open_swing_lake(
        start=SESSIONS[0], end=SESSIONS[-1], floors=[Decimal("0")], data_root=lake
    )
    try:
        reading = swing.regime_source.reading(SESSIONS[10])
        assert reading.index_level == Decimal("10000") + 10 * HISTORY.index(SESSIONS[10])
        assert reading.risk_on
    finally:
        swing.close()


def test_a_missing_published_series_is_a_loud_failure_not_a_proxy(tmp_path: Path) -> None:
    for session in SESSIONS:
        write_l1(tmp_path, session, [(STEADY, Decimal("50"), None)])
    with pytest.raises(RegimeSourceError, match="no published 'nifty50'"):
        open_swing_lake(
            start=SESSIONS[0], end=SESSIONS[-1], floors=[Decimal("0")], data_root=tmp_path
        )


def test_a_decision_date_the_series_does_not_reach_is_a_loud_failure() -> None:
    source = _RegimeSource(_published(SESSIONS[:6]), ma_days=3)
    with pytest.raises(RegimeSourceError, match="no level for"):
        source.reading(SESSIONS[8])


def test_too_short_a_history_for_the_mean_is_a_loud_failure() -> None:
    source = _RegimeSource(_published(SESSIONS), ma_days=200)
    with pytest.raises(RegimeSourceError, match="short of the 200-session"):
        source.reading(SESSIONS[5])


def test_a_computed_series_is_refused() -> None:
    """The gate reads the exchange's published level, never §4.1's computed estimate."""
    published = _published(SESSIONS)
    computed = TriSeries(
        index_slug="nifty50",
        index_name="Nifty 50",
        method="computed_price_plus_div",
        points=tuple(
            point.model_copy(update={"method": "computed_price_plus_div"})
            for point in published.points
        ),
    )
    with pytest.raises(RegimeSourceError, match="published series"):
        _RegimeSource(computed, ma_days=3)
