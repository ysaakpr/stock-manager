"""M3.1 — one `prices_raw` partition per date holds both exchanges' rows.

`write_prices_raw` paths a partition by `(dataset, date)` only, so before this suite existed a BSE
session written after the NSE session for the same date replaced it wholesale — and every
acceptance in `test_bse_bhavcopy` held, because it wrote BSE into an empty store. These tests write
NSE *and* BSE for one date into one store, and fail if either exchange clobbers the other, if a
rewrite of one exchange touches the other's rows, or if the order the exchanges arrive in changes
the bytes.

Offline and deterministic: synthetic rows, everything under `tmp_path`, no network, no postgres.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store.l1 import PRICES_RAW_DATASET, read_prices_raw, write_prices_raw
from dataplatform.store.paths import l1_partition_path

SESSION: Final = date(2026, 8, 7)
RELIANCE: Final = "INE002A01018"
INFOSYS: Final = "INE009A01021"
TCS: Final = "INE467B01029"


def _row(isin: str, symbol: str, *, series: str, close: str, qty: int) -> PriceRow:
    price = Decimal(close)
    return PriceRow(
        isin=isin,
        symbol=symbol,
        series=series,
        trade_date=SESSION,
        open=price,
        high=price,
        low=price,
        close=price,
        last=price,
        prev_close=price,
        total_traded_qty=qty,
        total_traded_value=price * qty,
        total_trades=max(qty // 10, 1),
    )


def _nse(*, close: str = "1400.50") -> tuple[PriceRow, ...]:
    return (
        _row(RELIANCE, "RELIANCE", series="EQ", close=close, qty=1_000_000),
        _row(INFOSYS, "INFY", series="EQ", close="1500.25", qty=800_000),
    )


def _bse(*, close: str = "1400.75") -> tuple[PriceRow, ...]:
    # BSE's "series" is its group, and the same ISIN prints on both venues.
    return (
        _row(RELIANCE, "RELIANCE", series="A", close=close, qty=40_000),
        _row(TCS, "TCS", series="A", close="3900.00", qty=5_000),
    )


def _by_exchange(data_root: Path) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    for record in read_prices_raw(SESSION, data_root=data_root):
        out.setdefault(str(record["exchange"]), []).append(record)
    return out


def _partition_bytes(data_root: Path) -> bytes:
    return l1_partition_path(PRICES_RAW_DATASET, SESSION, data_root=data_root).read_bytes()


def test_second_exchange_does_not_clobber_the_first(tmp_path: Path) -> None:
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    report = write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=tmp_path)

    rows = _by_exchange(tmp_path)
    assert set(rows) == {"NSE", "BSE"}
    assert len(rows["NSE"]) == 2
    assert len(rows["BSE"]) == 2
    assert report.rows_written == 2
    assert report.rows_preserved == 2


def test_first_write_into_an_empty_store_preserves_nothing(tmp_path: Path) -> None:
    report = write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    assert report.rows_preserved == 0
    assert set(_by_exchange(tmp_path)) == {"NSE"}


def test_rewriting_one_exchange_replaces_only_its_own_rows(tmp_path: Path) -> None:
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=tmp_path)
    # The NSE session is re-derived — the delivery join does exactly this — with a different close
    # and one row fewer: the NSE block must be replaced whole, the BSE block left untouched.
    write_prices_raw(_nse(close="1401.00")[:1], exchange=Exchange.NSE, data_root=tmp_path)

    rows = _by_exchange(tmp_path)
    assert [r["isin"] for r in rows["NSE"]] == [RELIANCE]
    assert rows["NSE"][0]["close"] == Decimal("1401.0000")
    assert sorted(str(r["isin"]) for r in rows["BSE"]) == sorted([RELIANCE, TCS])
    bse_reliance = next(r for r in rows["BSE"] if r["isin"] == RELIANCE)
    assert bse_reliance["close"] == Decimal("1400.7500")


def test_same_isin_on_both_exchanges_keeps_both_prints(tmp_path: Path) -> None:
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=tmp_path)
    reliance = [r for r in read_prices_raw(SESSION, data_root=tmp_path) if r["isin"] == RELIANCE]
    assert {(r["exchange"], r["close"]) for r in reliance} == {
        ("NSE", Decimal("1400.5000")),
        ("BSE", Decimal("1400.7500")),
    }


def test_write_order_does_not_change_the_bytes(tmp_path: Path) -> None:
    nse_first = tmp_path / "nse_first"
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=nse_first)
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=nse_first)

    bse_first = tmp_path / "bse_first"
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=bse_first)
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=bse_first)

    assert _partition_bytes(nse_first) == _partition_bytes(bse_first)


def test_rewriting_an_exchange_with_the_same_rows_is_byte_identical(tmp_path: Path) -> None:
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=tmp_path)
    before = _partition_bytes(tmp_path)
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    assert _partition_bytes(tmp_path) == before


def test_rows_are_ordered_exchange_first(tmp_path: Path) -> None:
    write_prices_raw(_nse(), exchange=Exchange.NSE, data_root=tmp_path)
    write_prices_raw(_bse(), exchange=Exchange.BSE, data_root=tmp_path)
    keys = [
        (str(r["exchange"]), str(r["isin"]), str(r["symbol"]), str(r["series"]))
        for r in read_prices_raw(SESSION, data_root=tmp_path)
    ]
    assert keys == sorted(keys)
    # 'BSE' < 'NSE': the block order is the key's, never the write order's.
    assert keys[0][0] == "BSE"
