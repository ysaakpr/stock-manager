"""The backtest's L1 reader is pinned to one venue — a BSE bar can never enter an NSE run.

``backtest.run._L1Reader`` used to scope its reads with ``series = 'EQ'`` alone. That selected
exactly the NSE rows, but only as an accident of the sources: an L1 ``prices_raw`` partition holds
whichever venues have been backfilled for that date (there is no exchange in the partition path),
and BSE happens to label its cash segment by group code (A/B/X/XT/T/M/Z/…) rather than ``EQ``.
Nothing enforced that. The day a BSE-side parser mapped a group to ``EQ`` — or a third venue landed
in L1 — BSE bars would have entered the momentum signal, the listing windows, the liquidity screen
and the fill reference bars *silently*, because a wrong-venue bar is a perfectly valid bar.

So the fixture here writes the row that accident cannot survive: a **BSE row labelled
``series = 'EQ'``**, priced and traded, on the same partition as the NSE name and with the largest
turnover in the market. Every ``_L1Reader`` accessor must refuse it. Remove
``exchange = 'NSE'`` from ``_L1Reader._SCOPE`` and every test below fails.

``test_fixture_really_holds_a_bse_eq_row`` pins the fixture's own premise, so the suite cannot go
vacuous if the poison row is ever dropped from the partition by mistake.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.run import _L1Reader
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from execution.broker import Exchange

_PRICE_Q: Final = Decimal("0.0001")

#: The NSE cash-segment name — the only row any accessor may return.
NSE_EQ: Final = "INE100A01010"
#: The poison row: a BSE bar labelled ``EQ``, which is what a BSE parser mapping its group codes
#: onto NSE's series vocabulary would write. Priced, traded, and the most liquid name in the
#: fixture, so an unfiltered ``most_liquid_on`` would rank it *first*.
BSE_EQ: Final = "INE200A01010"
#: A BSE bar as BSE actually prints one today (group ``A``) — excluded by either predicate.
BSE_GROUP: Final = "INE300A01010"

#: The session all three names print on.
SHARED: Final = date(2024, 7, 2)
#: A session only BSE printed on — not a session of the NSE market, so it must not reach the
#: calendar the replay walks or the fill model targets.
BSE_ONLY: Final = date(2024, 7, 3)


def _record(
    isin: str, exchange: str, series: str, *, close: Decimal, qty: int
) -> dict[str, object]:
    """One ``prices_raw`` row: OHLC all at ``close``, turnover ``close * qty``."""
    price = close.quantize(_PRICE_Q)
    return {
        "isin": isin,
        "exchange": exchange,
        "symbol": isin[:6],
        "series": series,
        "trade_date": None,  # filled by _write_partition
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "last": price,
        "prev_close": price,
        "total_traded_qty": qty,
        "total_traded_value": (close * qty).quantize(_PRICE_Q),
        "total_trades": qty,
        "deliv_qty": None,
        "deliv_pct": None,
    }


def _write_partition(data_root: Path, trade_date: date, records: list[dict[str, object]]) -> Path:
    """Write one L1 ``prices_raw`` partition through the real on-disk schema."""
    rows = [{**record, "trade_date": trade_date} for record in records]
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA),
        path,
        compression="snappy",
        version="2.6",
    )
    return path


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A two-session lake: NSE + two BSE rows on ``SHARED``, BSE alone on ``BSE_ONLY``."""
    nse = _record(NSE_EQ, "NSE", "EQ", close=Decimal("100"), qty=1_000)
    bse_eq = _record(BSE_EQ, "BSE", "EQ", close=Decimal("500"), qty=1_000_000)
    bse_group = _record(BSE_GROUP, "BSE", "A", close=Decimal("250"), qty=500_000)
    _write_partition(tmp_path, SHARED, [nse, bse_eq, bse_group])
    _write_partition(tmp_path, BSE_ONLY, [bse_eq, bse_group])
    return tmp_path


@pytest.fixture
def reader(lake: Path) -> Iterator[_L1Reader]:
    reader = _L1Reader(data_root=lake)
    try:
        yield reader
    finally:
        reader.close()


def test_fixture_really_holds_a_bse_eq_row(lake: Path) -> None:
    """The premise: the partition on disk *does* carry a BSE row labelled ``EQ``.

    Without this the whole module could pass vacuously — a fixture that quietly stopped writing the
    poison row would prove nothing about the venue filter.
    """
    path = l1_partition_path(PRICES_RAW_DATASET, SHARED, data_root=lake)
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            "SELECT isin, exchange FROM read_parquet($path) WHERE series = 'EQ' ORDER BY isin",
            {"path": str(path)},
        ).fetchall()
    finally:
        con.close()
    assert rows == [(NSE_EQ, "NSE"), (BSE_EQ, "BSE")]


def test_closes_exclude_the_bse_eq_row(reader: _L1Reader) -> None:
    """The signal and sizing closes — the cross-section the policy ranks on."""
    assert reader.closes_on(SHARED) == {NSE_EQ: Decimal("100.0000")}


def test_reference_bars_exclude_the_bse_eq_row(reader: _L1Reader) -> None:
    """The fill references — a BSE bar here would fill an NSE order at BSE's price."""
    bars = reader.reference_bars_on(SHARED)
    assert set(bars) == {NSE_EQ}
    assert bars[NSE_EQ].exchange is Exchange.NSE


def test_most_liquid_excludes_the_bse_eq_row(reader: _L1Reader) -> None:
    """The benchmark and regime basket. ``BSE_EQ`` is the fixture's most-liquid name by turnover,
    so an unfiltered read would put it at the head of the basket."""
    assert reader.most_liquid_on(SHARED, 5) == [NSE_EQ]


def test_listing_windows_exclude_the_bse_eq_row(reader: _L1Reader) -> None:
    """The survivorship-safe tradeable windows — the PIT universe is built from these."""
    assert [window.isin for window in reader.listing_windows()] == [NSE_EQ]


def test_median_turnover_excludes_the_bse_eq_row(reader: _L1Reader) -> None:
    """The M9.3 liquidity screen — a ₹50 crore BSE print would clear any floor."""
    medians = reader.median_turnover_over(SHARED, BSE_ONLY)
    assert set(medians) == {NSE_EQ}


def test_calendar_excludes_a_bse_only_session(reader: _L1Reader) -> None:
    """``BSE_ONLY`` is not an NSE session: it must not reach the calendar the replay walks."""
    assert reader.all_sessions() == (SHARED,)
    assert reader.trading_sessions(SHARED, BSE_ONLY) == (SHARED,)
