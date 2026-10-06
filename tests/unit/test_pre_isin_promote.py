"""Pre-ISIN promotion writes admitted rows to `prices_raw` and accounts for every other row."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from dataplatform.identity.pre_isin import ChainRow
from dataplatform.ingest import pre_isin_promote as pip
from dataplatform.ingest.models import PreIsinPriceRow
from dataplatform.ingest.nse import bhavcopy_legacy
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import (
    PRICES_RAW_DATASET,
    PRICES_RAW_QUARANTINE_DATASET,
    PRICES_RAW_QUARANTINE_SCHEMA,
    PRICES_RAW_SCHEMA,
    PriceQuarantineReason,
)

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy"
E1_DAY: Final = date(2011, 6, 21)


def _inputs() -> tuple[dict[date, tuple[PreIsinPriceRow, ...]], pip.AnchorRows]:
    e1 = FIXTURES / "pre_isin" / "cm21JUN2011bhav.csv.zip"
    e2 = FIXTURES / "legacy" / "cm22JUN2011bhav.csv.zip"
    pre = {E1_DAY: bhavcopy_legacy.parse_pre_isin_prices(e1.read_bytes(), filename=e1.name)}
    post = bhavcopy_legacy.parse(e2.read_bytes(), filename=e2.name)
    anchors = pip.AnchorRows(
        rows=tuple(
            ChainRow(r.symbol, r.series, r.trade_date, r.close, r.prev_close, r.isin) for r in post
        ),
        sessions=(date(2011, 6, 22),),
    )
    return pre, anchors


def test_promotion_accounts_for_every_row_and_writes_only_admitted_ones(tmp_path: Path) -> None:
    pre, anchors = _inputs()
    stats = pip.promote(pre, anchors, renames=(), actions=(), data_root=tmp_path)

    priced = pq.read_table(
        partition_path(Layer.L1, PRICES_RAW_DATASET, E1_DAY, data_root=tmp_path),
        schema=PRICES_RAW_SCHEMA,
    ).to_pylist()
    refused = pq.read_table(
        partition_path(Layer.L1, PRICES_RAW_QUARANTINE_DATASET, E1_DAY, data_root=tmp_path),
        schema=PRICES_RAW_QUARANTINE_SCHEMA,
    ).to_pylist()

    assert len(priced) + len(refused) == len(pre[E1_DAY]), "a row was dropped or duplicated"
    assert stats.rows_by_year[2011]["resolved"] == len(priced)
    assert stats.rows_by_year[2011]["quarantined"] == len(refused)
    assert all(row["isin"] for row in priced)
    assert all(row["total_trades"] is None for row in priced), "a trade count was invented"
    assert all(row["exchange"] == "NSE" for row in priced)
    assert all(
        str(row["reason"]).startswith(PriceQuarantineReason.ISIN_COLUMN_ABSENT_PREFIX)
        for row in refused
    )
    assert all(row["isin"] is None for row in refused)
    # every admitted ISIN is the one the same symbol stated on the next session
    stated = {(r.symbol, r.series): r.isin for r in anchors.rows}
    for row in priced:
        assert row["isin"] in {stated.get((row["symbol"], s)) for s in ("EQ", "BE", "BZ")}


def test_promotion_is_byte_identical_on_rerun(tmp_path: Path) -> None:
    pre, anchors = _inputs()
    pip.promote(pre, anchors, renames=(), actions=(), data_root=tmp_path)
    path = partition_path(Layer.L1, PRICES_RAW_DATASET, E1_DAY, data_root=tmp_path)
    first = path.read_bytes()
    pip.promote(pre, anchors, renames=(), actions=(), data_root=tmp_path)
    assert path.read_bytes() == first


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    pre, anchors = _inputs()
    stats = pip.promote(
        pre,
        anchors,
        renames=(),
        actions=(),
        data_root=tmp_path,
        dry_run=True,
    )
    assert stats.sessions == 1
    assert not any(tmp_path.rglob("*.parquet"))


def test_validate_counts_no_mistake_on_a_clean_isin_era_tape() -> None:
    _, anchors = _inputs()
    out = pip.validate(anchors.rows, cutoffs=[date(2011, 6, 22)], renames=(), actions=())
    assert out["2011-06-22"]["rows_wrong"] == 0  # type: ignore[index]
