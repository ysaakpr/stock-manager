"""M1.8 — the L1 canonical writer for `prices_raw`.

Drives the writer end to end on the real checked-in fixtures for one session (2026-08-07): the
UDiFF bhavcopy (M1.5) and the matching `sec_bhavdata_full` delivery file (M1.6), joined through a
D2 identity master (M1.7) built from the session's own price rows. Every acceptance criterion of
the task is a test here:

  1. a date's partition rebuilt from L0 is byte-identical on re-run (`test_rebuild_from_l0_*`)
  2. `prices_raw` has no adjusted-price column, asserted structurally (`test_no_adjusted_column`)
  3. every row carries a resolved ISIN; unresolvable delivery rows are quarantined and counted,
     never dropped (`test_unresolved_delivery_is_quarantined_*`, `test_every_row_has_an_isin`)

Offline and deterministic: no network, no postgres, everything under `tmp_path` (conftest B8).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse import bhavcopy, delivery
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PricesRawWriteReport,
    SchemaError,
    read_prices_raw,
    rebuild_prices_raw_from_l0,
    write_prices_raw,
)
from dataplatform.store.paths import Layer, l1_partition_path, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_SCHEMA,
    PriceQuarantineReason,
    column_looks_adjusted,
)

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
SESSION: Final = date(2026, 8, 7)

_BHAVCOPY_FILE: Final = "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
_DELIVERY_FILE: Final = "sec_bhavdata_full_07082026.csv"
_BHAVCOPY_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "nse_bhavcopy" / "udiff" / _BHAVCOPY_FILE
_DELIVERY_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "nse_delivery" / _DELIVERY_FILE


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def price_rows() -> tuple[PriceRow, ...]:
    """The real UDiFF bhavcopy for the session, parsed to canonical price rows."""
    return bhavcopy.parse(_BHAVCOPY_PATH.read_bytes(), filename=_BHAVCOPY_FILE, trade_date=SESSION)


@pytest.fixture
def delivery_rows() -> tuple[DeliveryRow, ...]:
    """The real delivery file for the same session."""
    return delivery.parse(_DELIVERY_PATH.read_bytes(), filename=_DELIVERY_FILE)


@pytest.fixture
def master(price_rows: tuple[PriceRow, ...]) -> IdentityMaster:
    """A master that resolves every symbol in the session to the ISIN its price row carries.

    Built from the price rows themselves so the delivery join is exercised on real (symbol → ISIN)
    pairs — the only sanctioned symbol→ISIN path (invariant #2). One open window per distinct
    (symbol, ISIN), which is what makes a delivery row for a symbol *not* in this file resolve to
    None and land in quarantine.
    """
    windows = {
        (row.symbol, row.isin): SymbolWindow(
            exchange=Exchange.NSE,
            symbol=row.symbol,
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin=row.isin,
        )
        for row in price_rows
    }
    return IdentityMaster(tuple(windows.values()))


@pytest.fixture
def l0(tmp_path: Path) -> L0Store:
    """An L0 store rooted in the test's tmp dir, with the two fixtures written in."""
    store = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    store.put("nse_bhavcopy", SESSION, _BHAVCOPY_FILE, _BHAVCOPY_PATH.read_bytes())
    store.put("nse_sec_bhavdata_full", SESSION, _DELIVERY_FILE, _DELIVERY_PATH.read_bytes())
    return store


# ── acceptance 1: idempotent rebuild from L0 ───────────────────────────────────────────────────


def test_partition_is_written_where_the_layout_says(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    report = write_prices_raw(
        price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path
    )
    assert report.path == l1_partition_path(PRICES_RAW_DATASET, SESSION, data_root=tmp_path)
    assert report.path.exists()
    assert report.rows_written == len(price_rows)


def test_rebuild_from_l0_is_byte_identical(
    l0: L0Store, master: IdentityMaster, tmp_path: Path
) -> None:
    bhav_ref = l0.ref_for("nse_bhavcopy", SESSION, _BHAVCOPY_FILE)
    deliv_ref = l0.ref_for("nse_sec_bhavdata_full", SESSION, _DELIVERY_FILE)

    first = rebuild_prices_raw_from_l0(
        l0, bhav_ref, delivery_ref=deliv_ref, master=master, data_root=tmp_path
    )
    first_bytes = first.path.read_bytes()
    second = rebuild_prices_raw_from_l0(
        l0, bhav_ref, delivery_ref=deliv_ref, master=master, data_root=tmp_path
    )
    assert second.path.read_bytes() == first_bytes


def test_rebuild_and_direct_write_agree(
    l0: L0Store,
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    """The L0-driven path and the in-memory path produce the identical partition."""
    direct = write_prices_raw(
        price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path
    ).path.read_bytes()

    other_root = tmp_path / "via_l0"
    bhav_ref = l0.ref_for("nse_bhavcopy", SESSION, _BHAVCOPY_FILE)
    deliv_ref = l0.ref_for("nse_sec_bhavdata_full", SESSION, _DELIVERY_FILE)
    via_l0 = rebuild_prices_raw_from_l0(
        l0, bhav_ref, delivery_ref=deliv_ref, master=master, data_root=other_root
    ).path.read_bytes()
    assert via_l0 == direct


def test_rows_are_sorted_deterministically(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    records = read_prices_raw(SESSION, data_root=tmp_path)
    keys = [(r["isin"], r["symbol"], r["series"]) for r in records]
    assert keys == sorted(keys)


# ── acceptance 2: no adjusted-price column ─────────────────────────────────────────────────────


def test_no_adjusted_column(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    schema = pq.read_schema(l1_partition_path(PRICES_RAW_DATASET, SESSION, data_root=tmp_path))
    assert not any(column_looks_adjusted(name) for name in schema.names), schema.names
    # The obvious adjusted-price names are specifically absent.
    for forbidden in ("adj_close", "adjusted_close", "close_adj", "cum_price_factor"):
        assert forbidden not in schema.names


def test_declared_schema_itself_carries_no_adjusted_column() -> None:
    assert not any(column_looks_adjusted(name) for name in PRICES_RAW_SCHEMA.names)


# ── acceptance 3: every row has an ISIN; unresolvable rows quarantined ──────────────────────────


def test_every_row_has_an_isin(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    records = read_prices_raw(SESSION, data_root=tmp_path)
    assert records
    assert all(rec["isin"] for rec in records)
    # The isin column is non-nullable in the declared schema — a row without one cannot be written.
    assert not PRICES_RAW_SCHEMA.field("isin").nullable


def _rel(symbol: str, series: str, isin: str) -> PriceRow:
    return PriceRow(
        isin=isin,
        symbol=symbol,
        series=series,
        trade_date=SESSION,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        last=Decimal("100.50"),
        prev_close=Decimal("100.00"),
        total_traded_qty=1000,
        total_traded_value=Decimal("100500.00"),
        total_trades=42,
    )


def _deliv(symbol: str, series: str, qty: int | None, pct: str | None) -> DeliveryRow:
    return DeliveryRow(
        symbol=symbol,
        series=series,
        trade_date=SESSION,
        deliv_qty=qty,
        deliv_pct=Decimal(pct) if pct is not None else None,
    )


def test_unresolved_delivery_is_quarantined_and_counted(tmp_path: Path) -> None:
    """A delivery symbol the master has never seen is quarantined, counted, and never dropped."""
    price = [_rel("RELIANCE", "EQ", "INE002A01018")]
    master = IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE002A01018",
            ),
        )
    )
    deliv = [
        _deliv("RELIANCE", "EQ", 500, "50.00"),
        _deliv("GHOSTCO", "EQ", 10, "10.00"),  # master has never seen this symbol → no ISIN
    ]

    report = write_prices_raw(price, delivery_rows=deliv, master=master, data_root=tmp_path)

    assert report.delivery_rows == 2
    assert report.delivery_joined == 1
    assert report.delivery_unresolved == 1
    assert report.delivery_orphaned == 0
    # The counts reconcile: nothing was dropped.
    assert (
        report.delivery_rows
        == report.delivery_joined + report.delivery_unresolved + report.delivery_orphaned
    )

    assert report.quarantine_path is not None
    assert report.quarantine_path == partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    )
    quarantined = pq.read_table(report.quarantine_path).to_pylist()
    assert len(quarantined) == 1
    assert quarantined[0]["symbol"] == "GHOSTCO"
    assert quarantined[0]["isin"] is None
    assert quarantined[0]["reason"] == PriceQuarantineReason.SYMBOL_UNRESOLVED


def test_resolved_but_unjoined_delivery_is_quarantined_not_dropped(tmp_path: Path) -> None:
    """A delivery row that resolves but matches no price row is an orphan — quarantined, kept."""
    price = [_rel("RELIANCE", "EQ", "INE002A01018")]
    master = IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE002A01018",
            ),
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="TCS",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE467B01029",
            ),
        )
    )
    # TCS resolves to an ISIN, but there is no TCS price row this session.
    deliv = [_deliv("RELIANCE", "EQ", 500, "50.00"), _deliv("TCS", "EQ", 700, "70.00")]

    report = write_prices_raw(price, delivery_rows=deliv, master=master, data_root=tmp_path)

    assert report.delivery_joined == 1
    assert report.delivery_orphaned == 1
    assert report.delivery_unresolved == 0
    assert report.quarantine_path is not None
    quarantined = pq.read_table(report.quarantine_path).to_pylist()
    assert len(quarantined) == 1
    assert quarantined[0]["symbol"] == "TCS"
    assert quarantined[0]["isin"] == "INE467B01029"
    assert quarantined[0]["reason"] == PriceQuarantineReason.NO_MATCHING_PRICE


def test_no_quarantine_file_when_everything_places(tmp_path: Path) -> None:
    price = [_rel("RELIANCE", "EQ", "INE002A01018")]
    master = IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE002A01018",
            ),
        )
    )
    report = write_prices_raw(
        price,
        delivery_rows=[_deliv("RELIANCE", "EQ", 500, "50.00")],
        master=master,
        data_root=tmp_path,
    )
    assert report.quarantine_path is None
    assert report.quarantined == 0
    assert not partition_path(
        Layer.L1, PRICES_RAW_QUARANTINE_DATASET, SESSION, data_root=tmp_path
    ).exists()


# ── delivery join semantics ────────────────────────────────────────────────────────────────────


def test_delivery_joins_by_isin_and_series(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    records = read_prices_raw(SESSION, data_root=tmp_path)
    by_symbol = {rec["symbol"]: rec for rec in records}
    # 20MICRONS/EQ has a real delivery figure in the fixture (73242 shares, 57.86%).
    row = by_symbol["20MICRONS"]
    assert row["deliv_qty"] == 73242
    assert row["deliv_pct"] == Decimal("57.8600")


def test_absent_delivery_stays_none_not_zero(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    """A '-' delivery figure (BE/BZ series) survives the write as None, distinguishable from 0."""
    absent = next(d for d in delivery_rows if d.deliv_qty is None)
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    records = read_prices_raw(SESSION, data_root=tmp_path)
    row = next(
        rec for rec in records if rec["symbol"] == absent.symbol and rec["series"] == absent.series
    )
    assert row["deliv_qty"] is None
    assert row["deliv_pct"] is None


def test_money_reads_back_as_decimal(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    write_prices_raw(price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path)
    row = read_prices_raw(SESSION, data_root=tmp_path)[0]
    for field in ("open", "high", "low", "close", "last", "prev_close", "total_traded_value"):
        assert isinstance(row[field], Decimal), field


def test_prices_are_written_without_a_master_when_no_delivery(
    price_rows: tuple[PriceRow, ...], tmp_path: Path
) -> None:
    """Prices carry their own ISIN; the master is only needed for the delivery join."""
    report = write_prices_raw(price_rows, data_root=tmp_path)
    assert report.rows_written == len(price_rows)
    assert report.delivery_rows == 0
    assert all(rec["deliv_qty"] is None for rec in read_prices_raw(SESSION, data_root=tmp_path))


# ── loud failures ───────────────────────────────────────────────────────────────────────────────


def test_delivery_without_a_master_is_refused(tmp_path: Path) -> None:
    price = [_rel("RELIANCE", "EQ", "INE002A01018")]
    with pytest.raises(ValueError, match="master"):
        write_prices_raw(
            price, delivery_rows=[_deliv("RELIANCE", "EQ", 1, "1.00")], data_root=tmp_path
        )


def test_empty_batch_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no price rows"):
        write_prices_raw([], data_root=tmp_path)


def test_two_sessions_in_one_batch_are_refused(tmp_path: Path) -> None:
    a = _rel("RELIANCE", "EQ", "INE002A01018")
    b = a.model_copy(update={"trade_date": date(2026, 8, 6)})
    with pytest.raises(ValueError, match="more than one session"):
        write_prices_raw([a, b], data_root=tmp_path)


def test_delivery_for_a_different_session_is_refused(tmp_path: Path) -> None:
    price = [_rel("RELIANCE", "EQ", "INE002A01018")]
    master = IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="RELIANCE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE002A01018",
            ),
        )
    )
    stray = DeliveryRow(
        symbol="RELIANCE",
        series="EQ",
        trade_date=date(2026, 8, 6),
        deliv_qty=1,
        deliv_pct=Decimal("1.00"),
    )
    with pytest.raises(ValueError, match="delivery"):
        write_prices_raw(price, delivery_rows=[stray], master=master, data_root=tmp_path)


def test_schema_drift_fails_loud(
    price_rows: tuple[PriceRow, ...], master: IdentityMaster, tmp_path: Path
) -> None:
    """A table whose schema is not the declared one is refused by `enforce_schema`."""
    from dataplatform.store.schemas import enforce_schema

    drifted = pa.table({"isin": ["INE002A01018"], "surprise": [1]})
    with pytest.raises(SchemaError, match="drifted"):
        enforce_schema(drifted, PRICES_RAW_SCHEMA, dataset=PRICES_RAW_DATASET)


def test_adjusted_column_in_schema_is_refused() -> None:
    from dataplatform.store.schemas import assert_raw_only

    bad = pa.schema([pa.field("isin", pa.string()), pa.field("adj_close", pa.decimal128(20, 4))])
    with pytest.raises(SchemaError, match="adjusted"):
        assert_raw_only(bad)


def test_reading_a_missing_partition_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_prices_raw(SESSION, data_root=tmp_path)


def test_report_reconciles_on_the_full_fixture(
    price_rows: tuple[PriceRow, ...],
    delivery_rows: tuple[DeliveryRow, ...],
    master: IdentityMaster,
    tmp_path: Path,
) -> None:
    """On the real session, every delivery row is either joined, unresolved, or orphaned."""
    report: PricesRawWriteReport = write_prices_raw(
        price_rows, delivery_rows=delivery_rows, master=master, data_root=tmp_path
    )
    assert report.delivery_rows == len(delivery_rows)
    assert (
        report.delivery_rows
        == report.delivery_joined + report.delivery_unresolved + report.delivery_orphaned
    )
    # The master was built from the price rows, so most delivery rows resolve and join.
    assert report.delivery_joined > 0
