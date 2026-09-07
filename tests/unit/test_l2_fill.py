"""M2.5 — L2 covers every ISIN L1 has EQ bars for, not only the ones a corporate action touched.

`rebuild_invalidated` builds exactly the ISINs a recompute flagged; nothing else ever built one. So
a name with no reconciled action — no split, no bonus, no dividend, no reissue — never got a
partition however long it traded. On the server on 2026-09-07 that was 793 of the 2,716 NSE EQ
names then trading (ADANIGREEN, ADANIENSOL, ETERNAL among them), absent from every L2 reader.

`materialize_missing` is the first-time fill. These tests pin down what it must and must not do:
cover every EQ ISIN and only EQ ISINs; leave what is already on disk byte-for-byte alone (so it is
safe to run again and again); skip a retired ISIN whose bars belong to its survivor's stitched
partition, and build that survivor over its whole chain when it is the one missing.

Offline and deterministic: synthetic rows under `tmp_path`, an empty factor store, no network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store.db import Connection
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    isins_with_eq_bars,
    materialize_isin,
    materialize_missing,
    materialized_isins,
    open_connection,
    read_adjusted,
)
from dataplatform.store.paths import l2_isin_partition_path

RELIANCE = "INE002A01018"  # NSE EQ on every session
INFOSYS = "INE009A01021"  # NSE EQ and a trade-to-trade (BE) row the same day — BE must not count
TCS = "INE467B01029"  # BSE only: no EQ series anywhere, so nothing for L2 to cover
HDFC = "INE040A01034"  # NSE EQ; doubles as the "retired" ISIN in the lineage case
SESSIONS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3))


class _EmptyStore:
    """A store with no factors and no reconciled actions — the fill's common case.

    `materialize_missing` reads two tables through the connection it is handed (`load_factor_chain`,
    `load_reconciled_actions`); for a name no corporate action ever touched both are empty, and an
    empty chain adjusts by 1. Standing in for Postgres here keeps the test offline; the DB-backed
    path is exercised in `tests/integration/test_l2_views.py`.
    """

    def execute(self, sql: str, params: object = None) -> _EmptyStore:
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


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
            [_row(TCS, day, series="A", close="3800.00")],
            exchange=Exchange.BSE,
            data_root=tmp_path,
        )
    return tmp_path


def _conn() -> Connection:
    return cast(Connection, _EmptyStore())


def test_the_population_is_every_isin_with_an_eq_bar(lake: Path) -> None:
    con = open_connection()
    try:
        assert isins_with_eq_bars(con, data_root=lake) == tuple(sorted((RELIANCE, INFOSYS, HDFC)))
    finally:
        con.close()


def test_a_cold_lake_has_no_population(tmp_path: Path) -> None:
    con = open_connection()
    try:
        assert isins_with_eq_bars(con, data_root=tmp_path) == ()
    finally:
        con.close()
    assert materialized_isins(data_root=tmp_path) == frozenset()


def test_the_fill_covers_what_is_missing_and_only_that(lake: Path) -> None:
    # RELIANCE was built the ordinary way (say, by the invalidation queue) before the fill ran.
    materialize_isin(
        RELIANCE, chain=FactorChain(isin=RELIANCE, rows=()), actions=(), data_root=lake
    )
    reliance = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, RELIANCE, data_root=lake)
    before = reliance.read_bytes()

    report = materialize_missing(_conn(), data_root=lake)

    assert report.candidates == 3
    assert report.already_materialized == 1
    assert report.skipped_retired == 0
    assert sorted(r.isin for r in report.written) == sorted((INFOSYS, HDFC))
    assert materialized_isins(data_root=lake) == frozenset({RELIANCE, INFOSYS, HDFC})
    # Never built for a BSE-only name: no EQ bars means nothing to adjust, so no partition.
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, TCS, data_root=lake).exists()
    # What was already on disk is not rewritten — the fill is not a rebuild.
    assert reliance.read_bytes() == before


def test_a_filled_name_is_its_raw_eq_series_when_it_has_no_factors(lake: Path) -> None:
    materialize_missing(_conn(), data_root=lake)
    bars = read_adjusted(INFOSYS, data_root=lake)
    # Three EQ sessions, and none of the BE prints: the fill applies the same series scope as the
    # queue's rebuild, so the two paths produce the same partition for the same name.
    assert [b.trade_date for b in bars] == list(SESSIONS)
    assert [b.adj_close for b in bars] == [
        Decimal("1500.25"),
        Decimal("1501.25"),
        Decimal("1502.25"),
    ]
    assert {b.cum_price_factor for b in bars} == {Decimal(1)}


def test_running_the_fill_again_changes_nothing(lake: Path) -> None:
    first = materialize_missing(_conn(), data_root=lake)
    assert len(first.written) == 3
    bytes_after_first = {
        isin: l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=lake).read_bytes()
        for isin in (RELIANCE, INFOSYS, HDFC)
    }

    second = materialize_missing(_conn(), data_root=lake)

    assert second.written == ()
    assert second.already_materialized == 3
    for isin, payload in bytes_after_first.items():
        path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=lake)
        assert path.read_bytes() == payload


def test_a_retired_isin_is_skipped_and_its_survivor_stitched(lake: Path) -> None:
    """Pretend HDFC's ISIN was retired into RELIANCE's: the D2 lineage says so, so the fill must
    not give the retired ISIN a partition of its own — its bars belong to the survivor's."""

    def survivor_of(isin: str) -> str:
        return RELIANCE if isin == HDFC else isin

    report = materialize_missing(
        _conn(),
        data_root=lake,
        history_for={RELIANCE: (HDFC, RELIANCE)},
        survivor_of=survivor_of,
    )

    assert report.skipped_retired == 1
    assert sorted(r.isin for r in report.written) == sorted((RELIANCE, INFOSYS))
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, HDFC, data_root=lake).exists()
    # The survivor's partition carries both ISINs' bars, all keyed to the survivor.
    bars = read_adjusted(RELIANCE, data_root=lake)
    assert len(bars) == 2 * len(SESSIONS)
    assert {b.isin for b in bars} == {RELIANCE}
