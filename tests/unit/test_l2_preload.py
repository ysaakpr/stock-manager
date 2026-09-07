"""M2.4 — the L2 rebuild reads L1 once per drain, not once per ISIN, and the bytes do not move.

`read_raw_bars_from_l1` scans every `prices_raw` date partition to find one ISIN. L1 is
partitioned by date, so draining N invalidations opened every partition N times — on the server on
2026-09-07 that was ~3,450 ISINs over 2,475 partitions at ~0.45 s each, half an hour to pull 3.5 M
rows a single pass reads in seconds. `preload_raw_bars` is that single pass, kept on the DuckDB
connection `rebuild_invalidated` already threads through the loop.

What these tests pin down is the one thing the optimisation must not change: a preloaded read is
the scan — same rows, same types, same order — so the L2 partition written from it is the same
bytes (acceptance 2, "fully recomputable"). And the two ways it could quietly go wrong: an ISIN
outside the preload must still be served (by the scan), and a preloaded read must not open the lake.

Offline and deterministic: synthetic rows under `tmp_path`, no network, no postgres.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store import l2 as l2_module
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    materialize_isin,
    open_connection,
    preload_raw_bars,
    read_raw_bars_from_l1,
    wipe_adjusted,
)
from dataplatform.store.paths import l2_isin_partition_path

RELIANCE = "INE002A01018"  # NSE EQ on every session, plus a BSE group-A print
INFOSYS = "INE009A01021"  # NSE EQ and a trade-to-trade (BE) row the same day — BE must not count
TCS = "INE467B01029"  # BSE only: no EQ series anywhere, so no bars for L2
HDFC = "INE040A01034"  # NSE EQ, but deliberately left out of the preload
SESSIONS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3))


def _row(isin: str, day: date, *, series: str, close: str) -> PriceRow:
    price = Decimal(close)
    return PriceRow(
        isin=isin,
        symbol=isin[:6],
        series=series,
        trade_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        last=price,
        prev_close=price,
        total_traded_qty=1_000,
        total_traded_value=price * 1_000,
        total_trades=10,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for i, day in enumerate(SESSIONS):
        write_prices_raw(
            [
                _row(RELIANCE, day, series="EQ", close=f"{2400 + i}.50"),
                _row(INFOSYS, day, series="EQ", close=f"{1500 + i}.25"),
                _row(INFOSYS, day, series="BE", close="1.00"),
                _row(HDFC, day, series="EQ", close=f"{1600 + i}.00"),
            ],
            exchange=Exchange.NSE,
            data_root=tmp_path,
        )
        write_prices_raw(
            [
                _row(RELIANCE, day, series="A", close=f"{2401 + i}.00"),
                _row(TCS, day, series="A", close="3900.00"),
            ],
            exchange=Exchange.BSE,
            data_root=tmp_path,
        )
    return tmp_path


def _scan(isin: str, lake: Path) -> tuple[object, ...]:
    """The reference: a fresh connection, no preload, the whole-lake scan."""
    return read_raw_bars_from_l1(isin, data_root=lake)


def test_a_preloaded_read_is_the_scan(lake: Path) -> None:
    con = open_connection()
    try:
        rows = preload_raw_bars(con, [RELIANCE, INFOSYS, TCS], data_root=lake)
        # RELIANCE 3 EQ bars, INFOSYS 3 (its BE rows excluded), TCS 0 (BSE group A is not EQ).
        assert rows == 6
        for isin in (RELIANCE, INFOSYS, TCS):
            assert read_raw_bars_from_l1(isin, con=con, data_root=lake) == _scan(isin, lake)
        assert len(read_raw_bars_from_l1(INFOSYS, con=con, data_root=lake)) == 3
        assert read_raw_bars_from_l1(TCS, con=con, data_root=lake) == ()
    finally:
        con.close()


def test_a_preloaded_read_never_opens_the_lake(lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    con = open_connection()
    try:
        preload_raw_bars(con, [RELIANCE], data_root=lake)

        def _no_scan(*, data_root: Path | None) -> list[Path]:
            raise AssertionError("a covered ISIN must be served from the preload, not the lake")

        monkeypatch.setattr(l2_module, "_l1_partition_files", _no_scan)
        bars = read_raw_bars_from_l1(RELIANCE, con=con, data_root=lake)
        assert [b.trade_date for b in bars] == list(SESSIONS)
    finally:
        con.close()


def test_an_isin_outside_the_preload_falls_back_to_the_scan(lake: Path) -> None:
    con = open_connection()
    try:
        preload_raw_bars(con, [RELIANCE], data_root=lake)
        assert read_raw_bars_from_l1(HDFC, con=con, data_root=lake) == _scan(HDFC, lake)
        assert len(read_raw_bars_from_l1(HDFC, con=con, data_root=lake)) == 3
    finally:
        con.close()


def test_a_second_preload_replaces_the_first(lake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    con = open_connection()
    try:
        preload_raw_bars(con, [RELIANCE], data_root=lake)
        preload_raw_bars(con, [INFOSYS], data_root=lake)

        def _no_scan(*, data_root: Path | None) -> list[Path]:
            raise AssertionError("lake opened")

        monkeypatch.setattr(l2_module, "_l1_partition_files", _no_scan)
        assert len(read_raw_bars_from_l1(INFOSYS, con=con, data_root=lake)) == 3
        with pytest.raises(AssertionError, match="lake opened"):
            read_raw_bars_from_l1(RELIANCE, con=con, data_root=lake)
    finally:
        con.close()


def test_a_fresh_connection_is_never_mistaken_for_a_preload(lake: Path) -> None:
    con = open_connection()
    try:
        assert read_raw_bars_from_l1(RELIANCE, con=con, data_root=lake) == _scan(RELIANCE, lake)
    finally:
        con.close()


def test_preload_on_a_cold_lake_reads_nothing(tmp_path: Path) -> None:
    con = open_connection()
    try:
        assert preload_raw_bars(con, [RELIANCE], data_root=tmp_path) == 0
        assert read_raw_bars_from_l1(RELIANCE, con=con, data_root=tmp_path) == ()
    finally:
        con.close()


def test_materialized_bytes_are_identical_with_and_without_the_preload(lake: Path) -> None:
    """Acceptance 2 across the optimisation: the partition is the same file either way."""
    path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, RELIANCE, data_root=lake)
    materialize_isin(RELIANCE, chain=FactorChain(isin=RELIANCE), actions=(), data_root=lake)
    scanned = path.read_bytes()
    assert wipe_adjusted(data_root=lake) == 1

    con = open_connection()
    try:
        preload_raw_bars(con, [RELIANCE, INFOSYS], data_root=lake)
        report = materialize_isin(
            RELIANCE, chain=FactorChain(isin=RELIANCE), actions=(), con=con, data_root=lake
        )
    finally:
        con.close()
    assert report.rows_written == 3
    assert path.read_bytes() == scanned
