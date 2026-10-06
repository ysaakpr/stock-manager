"""The columns the price parsers dropped — VWAP, board lot, long name, BSE ex-marker (l1-widen).

Each parser is checked against a frozen fixture and against its sibling price parser, so an
attribute row can never exist for a row the price path would refuse, nor carry another row's key.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.ingest import session_attributes as sa
from dataplatform.ingest.bse import bhavcopy as bse_bhavcopy
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import bhavcopy_udiff, delivery
from dataplatform.store.schemas import (
    SESSION_ATTRIBUTES_SCHEMA,
    column_looks_adjusted,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
NSE_UDIFF: Final = (
    FIXTURES / "nse_bhavcopy" / "udiff" / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip"
)
BSE_UDIFF: Final = FIXTURES / "bse_bhavcopy" / "udiff" / "BhavCopy_BSE_CM_0_0_0_20260807_F_0000.CSV"
SEC_BHAVDATA: Final = FIXTURES / "nse_delivery" / "sec_bhavdata_full_06082026.csv"
MISDATED: Final = FIXTURES / "nse_delivery" / "sec_bhavdata_full_30092019_MISDATED.csv"
BSE_LEGACY_REAL: Final = FIXTURES / "bse_bhavcopy" / "legacy" / "EQ250517_CSV.ZIP"


def test_nse_udiff_lot_and_name_ride_on_exactly_the_price_rows() -> None:
    payload = NSE_UDIFF.read_bytes()
    attrs = sa.parse_udiff_attributes(
        payload, filename=NSE_UDIFF.name, columns=bhavcopy_udiff.UDIFF_COLUMNS
    )
    prices = bhavcopy_udiff.parse(payload, filename=NSE_UDIFF.name)
    assert [(a.isin, a.symbol, a.series) for a in attrs] == [
        (p.isin, p.symbol, p.series) for p in prices
    ]
    tasty = next(a for a in attrs if a.symbol == "TASTYBITE")
    assert tasty.board_lot == 1
    assert tasty.instrument_name == "TASTY BITE EATABLES LTD"
    assert tasty.trade_date == date(2024, 7, 8)


def test_bse_udiff_attributes_match_the_bse_price_rows() -> None:
    payload = BSE_UDIFF.read_bytes()
    attrs = sa.parse_udiff_attributes(
        payload, filename=BSE_UDIFF.name, columns=bse_bhavcopy.UDIFF_COLUMNS
    )
    prices = bse_bhavcopy.parse_udiff(payload, filename=BSE_UDIFF.name)
    assert [(a.isin, a.symbol) for a in attrs] == [(p.isin, p.symbol) for p in prices]
    assert all(a.instrument_name for a in attrs)


def test_a_legacy_file_is_refused_by_the_udiff_attribute_reader() -> None:
    legacy = FIXTURES / "nse_bhavcopy" / "legacy" / "cm05JUL2024bhav.csv.zip"
    with pytest.raises(ParseError):
        sa.parse_udiff_attributes(
            legacy.read_bytes(), filename=legacy.name, columns=bhavcopy_udiff.UDIFF_COLUMNS
        )


def test_vwap_is_the_files_own_avg_price_on_every_delivery_row() -> None:
    payload = SEC_BHAVDATA.read_bytes()
    vwaps = delivery.parse_vwap(payload, filename=SEC_BHAVDATA.name, trade_date=date(2026, 8, 6))
    rows = delivery.parse(payload, filename=SEC_BHAVDATA.name)
    assert [(v.symbol, v.series) for v in vwaps] == [(r.symbol, r.series) for r in rows]
    first = vwaps[0]
    # `1018GS2026, GS, 06-Aug-2026, ..., AVG_PRICE 104.26, ...` — the fixture's first line
    assert (first.symbol, first.series, first.avg_price) == ("1018GS2026", "GS", Decimal("104.26"))


def test_vwap_from_a_misdated_file_is_refused() -> None:
    with pytest.raises(ParseError, match="fetched as"):
        delivery.parse_vwap(
            MISDATED.read_bytes(), filename=MISDATED.name, trade_date=date(2019, 9, 30)
        )


def test_the_bse_legacy_ex_marker_is_carried_verbatim() -> None:
    payload = BSE_LEGACY_REAL.read_bytes()
    parsed = bse_bhavcopy.parse_legacy_report(
        payload, filename=BSE_LEGACY_REAL.name, trade_date=date(2017, 5, 25)
    )
    markers = {q.close_indicator for q in parsed.quotes}
    assert "" in markers
    assert markers - {""}, "the frozen session carries ex-markers"
    assert markers <= {"", "XD", "XB", "SS", "SA", "XR", "CS"}


def test_the_attribute_schema_holds_no_adjusted_column() -> None:
    assert not any(column_looks_adjusted(name) for name in SESSION_ATTRIBUTES_SCHEMA.names)
    assert not SESSION_ATTRIBUTES_SCHEMA.field("isin").nullable


def test_the_attribute_partition_is_deterministic_and_removed_when_empty(tmp_path: Path) -> None:
    day = date(2024, 7, 8)
    rows = [
        sa.SessionAttribute("INE488B01017", "NSE", "TASTYBITE", "EQ", day, vwap=Decimal("10272.1")),
        sa.SessionAttribute("INE117A01022", "BSE", "ABB", "A", day, board_lot=1),
    ]
    path = sa.write_session_attributes(rows, trade_date=day, data_root=tmp_path)
    assert path is not None
    first = path.read_bytes()
    sa.write_session_attributes(list(reversed(rows)), trade_date=day, data_root=tmp_path)
    assert path.read_bytes() == first
    table = pq.read_table(path, schema=SESSION_ATTRIBUTES_SCHEMA).to_pylist()
    assert [r["exchange"] for r in table] == ["BSE", "NSE"]
    assert table[1]["vwap"] == Decimal("10272.1000")
    sa.write_session_attributes([], trade_date=day, data_root=tmp_path)
    assert not path.exists()
