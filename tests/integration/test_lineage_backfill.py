"""D2 lineage rebuild: a chain's retired middle is registered, so the chain is not cut there.

`security_master` is built from current snapshots, so an ISIN one reissue issued and the next one
retired is in none of them. BAJFINANCE traded as INE296A01016 to 2016-09-08, INE296A01024 from
2016-09-09 to 2025-06-13 and INE296A01032 after. `isin_lineage.successor_isin` REFERENCES the
master, so before this fix the 2016 edge was dropped: the survivor's L2 started in 2016 instead of
2011 and INE296A01016 no longer resolved to anything. Measured 2026-09-29: 75 of 609 edges.

Scratch Postgres database (skipped without `make up`) and a `tmp_path` lake; no network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.lineage import (
    LINEAGE_BACKFILL,
    LineageEdge,
    LineageStore,
    SkipReason,
    derive_edges,
    read_eq_presence,
    read_equity_spans,
)
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_missing, read_adjusted
from dataplatform.store.migrate import migrate
from dataplatform.store.paths import l2_isin_partition_path

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_lineage_backfill_{os.getpid()}"
CLOCK = FrozenClock(datetime(2026, 9, 29, 18, 30, tzinfo=IST))

_A = "INE296A01016"  # BAJFINANCE to 2016-09-08 — retired, in no snapshot
_B = "INE296A01024"  # 2016-09-09 .. 2025-06-13 — retired, in no snapshot: the chain's middle
_C = "INE296A01032"  # from 2025-06-16 — the survivor, the only one the snapshot lists
_A_DAYS = (date(2011, 1, 3), date(2016, 9, 8))
_B_DAYS = (date(2016, 9, 9), date(2025, 6, 13))
_C_DAYS = (date(2025, 6, 16), date(2025, 6, 17))


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="module")
def scratch_settings() -> Iterator[Settings]:
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()
    migrate(_settings_for(SCRATCH_DB), clock=CLOCK)
    yield _settings_for(SCRATCH_DB)
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """The migrated scratch database holding only the survivor, rolled back after each test."""
    with connection(scratch_settings) as live:
        live.execute(
            "INSERT INTO security_master "
            "(isin, name, primary_exchange, status, first_seen_date, created_at, updated_at) "
            "VALUES (%s, 'Bajaj Finance Limited', 'NSE', 'ACTIVE', %s, %s, %s)",
            (_C, date(2026, 8, 8), CLOCK.now(), CLOCK.now()),
        )
        try:
            yield live
        finally:
            live.rollback()


def _row(isin: str, day: date, symbol: str = "BAJFINANCE") -> PriceRow:
    price = Decimal("900.00")
    return PriceRow(
        isin=isin,
        symbol=symbol,
        series="EQ",
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
    for isin, days in ((_A, _A_DAYS), (_B, _B_DAYS), (_C, _C_DAYS)):
        for day in days:
            write_prices_raw([_row(isin, day)], exchange=Exchange.NSE, data_root=tmp_path)
    return tmp_path


def _derive(lake: Path) -> tuple[LineageEdge, ...]:
    spans, sessions = read_equity_spans(data_root=lake)
    return derive_edges(spans, sessions, {})


def _count(conn: Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_an_unknown_middle_is_registered_and_the_chain_is_continuous(
    conn: Connection, lake: Path
) -> None:
    edges = _derive(lake)
    assert [(e.predecessor_isin, e.successor_isin) for e in edges] == [(_A, _B), (_B, _C)]
    presence = read_eq_presence({e.successor_isin for e in edges}, data_root=lake)

    store = LineageStore(conn, clock=CLOCK)
    report = store.replace_derived(edges, presence=presence)

    assert (report.derived, report.written, report.still_skipped) == (2, 2, 0)
    assert report.registered_intermediates == (_B,)
    row = conn.execute(
        "SELECT name, primary_exchange, status, first_seen_date, last_seen_date, registered_by "
        "FROM security_master WHERE isin = %s",
        (_B,),
    ).fetchone()
    assert row == ("BAJFINANCE", "NSE", "DELISTED", _B_DAYS[0], _B_DAYS[-1], LINEAGE_BACKFILL)
    assert _count(conn, f"SELECT count(*) FROM security_master WHERE isin = '{_A}'") == 0, (
        "the chain's first ISIN is a predecessor, never registered"
    )

    resolver = store.load()
    assert resolver.survivor_of(_A) == _C, "the 2011 ISIN must be marked retired into the survivor"
    assert resolver.chain_to(_C) == (_A, _B, _C)

    # The survivor's L2 spans the whole chain, and neither retired ISIN gets its own partition.
    history = {
        isin: chain
        for isin in {e.successor_isin for e in edges}
        if len(chain := resolver.chain_to(isin)) > 1
    }
    materialize_missing(conn, data_root=lake, history_for=history, survivor_of=resolver.survivor_of)
    bars = read_adjusted(_C, data_root=lake)
    assert bars[0].trade_date == _A_DAYS[0], "the survivor's L2 must start in 2011, not 2016"
    assert [b.trade_date for b in bars] == [*_A_DAYS, *_B_DAYS, *_C_DAYS]
    for retired in (_A, _B):
        assert not l2_isin_partition_path("prices_adjusted", retired, data_root=lake).exists(), (
            f"{retired} is retired: its bars belong to the survivor's partition"
        )


def test_an_unknown_successor_with_no_l1_rows_is_skipped_and_counted(
    conn: Connection, lake: Path
) -> None:
    # A middle ISIN L1 never printed, and a chain end no snapshot lists: neither is invented.
    ghost, stray = "INE296A01099", "INE777Q01011"
    edges = (
        LineageEdge(_A, ghost, date(2016, 9, 9), 0, "BAJFINANCE", None),
        LineageEdge(ghost, _C, date(2025, 6, 16), 0, "BAJFINANCE", None),
        LineageEdge("INE777Q01003", stray, date(2020, 1, 1), 0, "STRAY", None),
    )
    presence = read_eq_presence({e.successor_isin for e in edges}, data_root=lake)
    assert ghost not in presence and stray not in presence

    report = LineageStore(conn, clock=CLOCK).replace_derived(edges, presence=presence)

    assert (report.derived, report.written, report.still_skipped) == (3, 1, 2)
    assert report.registered_intermediates == ()
    assert [(e.successor_isin, reason) for e, reason in report.skipped] == [
        (ghost, SkipReason.NO_L1_EQ_HISTORY),
        (stray, SkipReason.TERMINAL_SUCCESSOR_NOT_IN_MASTER),
    ]
    assert report.skipped_by_reason() == {
        SkipReason.TERMINAL_SUCCESSOR_NOT_IN_MASTER.value: 1,
        SkipReason.NO_L1_EQ_HISTORY.value: 1,
        SkipReason.CHAIN_SURVIVOR_NOT_IN_MASTER.value: 0,
    }
    assert (
        _count(
            conn,
            f"SELECT count(*) FROM security_master WHERE isin IN ('{ghost}', '{stray}')",
        )
        == 0
    )
    assert _count(conn, "SELECT count(*) FROM isin_lineage") == 1


def test_a_rerun_registers_nothing_twice(conn: Connection, lake: Path) -> None:
    edges = _derive(lake)
    presence = read_eq_presence({e.successor_isin for e in edges}, data_root=lake)
    store = LineageStore(conn, clock=CLOCK)

    first = store.replace_derived(edges, presence=presence)
    masters, lineage = (
        _count(conn, "SELECT count(*) FROM security_master"),
        conn.execute(
            "SELECT predecessor_isin, successor_isin, effective_date FROM isin_lineage "
            "ORDER BY predecessor_isin"
        ).fetchall(),
    )
    second = store.replace_derived(edges, presence=presence)

    assert first.registered_intermediates == (_B,)
    assert second.registered_intermediates == (), "the middle is known now — not registered again"
    assert second.written == first.written == 2
    assert _count(conn, "SELECT count(*) FROM security_master") == masters == 2
    assert (
        conn.execute(
            "SELECT predecessor_isin, successor_isin, effective_date FROM isin_lineage "
            "ORDER BY predecessor_isin"
        ).fetchall()
        == lineage
    )
