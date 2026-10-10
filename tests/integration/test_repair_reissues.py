"""M18.1: `repair_reissues` against Postgres — the broken 2026-10-10 shape in, a clean master out.

The broken state is rebuilt the way the pre-fix ingest wrote it: the old ISIN's window open from
the listing date, then the new ISIN's window inserted open from the *same* date and the overlap
queued. One reissue has an `isin_lineage` edge (TCC), the other only the L0 equity-list series
(BLSE), so both kinds of boundary evidence are exercised.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database; each test rolls back. The L0 lake is a `tmp_path`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.ingest import (
    EQUITY_LIST_COLUMNS,
    NSE_EQUITY_LIST_SOURCE,
    L0EquityListSeries,
    equity_list_filename,
    ingest_snapshot,
)
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    HistoryPlan,
    IdentityStore,
    ListingStatus,
    Security,
    SymbolWindow,
    detect_conflicts,
)
from dataplatform.identity.repair_reissues import (
    ExpectedReissue,
    RepairRefusedError,
    apply_repair,
    plan_repair,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_m18_1_repair_{os.getpid()}"
CLOCK = FrozenClock(datetime(2026, 10, 10, 7, 0, tzinfo=IST))
SNAPSHOT = date(2026, 10, 10)

TCC_OLD, TCC_NEW, TCC_LISTED, TCC_SWITCH = (
    "INE887D01016",
    "INE887D01024",
    date(2026, 2, 25),
    date(2026, 9, 4),
)
BLSE_OLD, BLSE_NEW, BLSE_LISTED, BLSE_SWITCH = (
    "INE0NLT01010",
    "INE0NLT01028",
    date(2024, 2, 6),
    date(2026, 10, 6),
)
EXPECTED = (
    ExpectedReissue("TCC", TCC_OLD, TCC_NEW, TCC_SWITCH),
    ExpectedReissue("BLSE", BLSE_OLD, BLSE_NEW, BLSE_SWITCH),
)
#: An unrelated security, so "the repair touches only the expected symbols" is observable.
OTHER = "ACME,Acme Limited,EQ,01-JAN-2010,10,1,INE111A01011,10"


def _equity_list(tcc: str, blse: str) -> str:
    rows = (
        f"TCC,TCC Concept Limited,EQ,25-FEB-2026,1,1,{tcc},1",
        f"BLSE,BLS E-Services Limited,EQ,06-FEB-2024,10,1,{blse},10",
        OTHER,
    )
    return ",".join(EQUITY_LIST_COLUMNS) + "\n" + "".join(f"{row}\n" for row in rows)


NEW_LIST = _equity_list(TCC_NEW, BLSE_NEW)


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
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
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def series(tmp_path: Path) -> L0EquityListSeries:
    """The daily L0 captures: BLSE flips on 2026-10-06; TCC had already flipped before them."""
    store = L0Store(clock=CLOCK, data_root=tmp_path)
    for on_date, blse in (
        (date(2026, 10, 1), BLSE_OLD),
        (date(2026, 10, 5), BLSE_OLD),
        (BLSE_SWITCH, BLSE_NEW),
        (SNAPSHOT, BLSE_NEW),
    ):
        text = _equity_list(TCC_NEW, blse)
        store.put(NSE_EQUITY_LIST_SOURCE, on_date, equity_list_filename(on_date), text.encode())
    return L0EquityListSeries(store)


@pytest.fixture
def broken(conn: Connection) -> IdentityStore:
    """The store as the pre-fix 07:00 ingest left it on 2026-10-10."""
    ingest_snapshot(
        conn,
        equity_list=_equity_list(TCC_OLD, BLSE_OLD),
        snapshot_date=date(2026, 9, 1),
        clock=CLOCK,
    )
    store = IdentityStore(conn, clock=CLOCK)
    store.write_securities(
        [
            Security(isin, name, Exchange.NSE, ListingStatus.ACTIVE, SNAPSHOT, SNAPSHOT)
            for isin, name in ((TCC_NEW, "TCC Concept Limited"), (BLSE_NEW, "BLS E-Services"))
        ]
    )
    store.apply_history(
        HistoryPlan(
            inserts=(
                SymbolWindow(
                    Exchange.NSE, "TCC", TCC_LISTED, None, TCC_NEW, "EQ", "nse_equity_list"
                ),
                SymbolWindow(
                    Exchange.NSE, "BLSE", BLSE_LISTED, None, BLSE_NEW, "EQ", "nse_equity_list"
                ),
            )
        )
    )
    for conflict in detect_conflicts(store.load_windows(), source="nse_equity_list"):
        store.record(conflict)
    conn.execute(
        "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, detected_by, "
        "confidence, gap_sessions, symbol_at_change, corroborating_action, computed_at) "
        "VALUES (%s, %s, %s, 'L1_CONTIGUITY', 'CORROBORATED', 0, 'TCC', 'SPLIT', %s)",
        (TCC_OLD, TCC_NEW, TCC_SWITCH, CLOCK.now()),
    )
    # ca_refresh met it too: a RESOLVE row for a held-back action on BLSE's ex-date.
    with pytest.raises(AmbiguousSymbolError):
        store.load_master().try_resolve("BLSE", date(2026, 9, 8))
    return store


def _nse_windows(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT symbol, isin, valid_from, valid_to FROM symbol_history WHERE exchange = 'NSE' "
        "ORDER BY symbol, valid_from, isin"
    ).fetchall()


def _open_conflicts(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT symbols, detected_by FROM identity_reconciliation WHERE NOT resolved ORDER BY id"
    ).fetchall()


def test_the_repair_splits_both_reissues_and_resolves_their_rows(
    conn: Connection, broken: IdentityStore, series: L0EquityListSeries
) -> None:
    assert len(_open_conflicts(conn)) == 3
    plan = plan_repair(
        conn,
        equity_list=NEW_LIST,
        symbol_changes="",
        snapshot_date=SNAPSHOT,
        evidence=series,
        expected=EXPECTED,
    )
    assert {(b.symbol, b.effective, b.evidence) for b in plan.boundaries} == {
        ("TCC", TCC_SWITCH, "isin_lineage"),
        ("BLSE", BLSE_SWITCH, "nse_equity_list_series"),
    }

    counts = apply_repair(conn, plan, clock=CLOCK)
    assert (counts.deleted, counts.inserted, counts.closed, counts.resolved) == (2, 2, 2, 3)

    assert _nse_windows(conn) == [
        ("ACME", "INE111A01011", date(2010, 1, 1), None),
        ("BLSE", BLSE_OLD, BLSE_LISTED, date(2026, 10, 5)),
        ("BLSE", BLSE_NEW, BLSE_SWITCH, None),
        ("TCC", TCC_OLD, TCC_LISTED, date(2026, 9, 3)),
        ("TCC", TCC_NEW, TCC_SWITCH, None),
    ]
    assert _open_conflicts(conn) == []
    reasons = conn.execute("SELECT resolution FROM identity_reconciliation").fetchall()
    assert all(r[0] and r[0].startswith("M18.1 repair_reissues") for r in reasons)

    master = broken.load_master()
    assert master.try_resolve("TCC", date(2026, 8, 1)) == TCC_OLD
    assert master.try_resolve("TCC", date(2026, 10, 1)) == TCC_NEW
    assert master.try_resolve("BLSE", date(2026, 9, 8)) == BLSE_OLD
    assert master.try_resolve("BLSE", BLSE_SWITCH) == BLSE_NEW

    # Idempotent: a second plan is a no-op, and so is the fixed ingest of the same snapshot.
    again = plan_repair(
        conn, equity_list=NEW_LIST, symbol_changes="", snapshot_date=SNAPSHOT, expected=EXPECTED
    )
    assert again.already_applied
    assert apply_repair(conn, again, clock=CLOCK).deleted == 0
    report = ingest_snapshot(
        conn, equity_list=NEW_LIST, snapshot_date=SNAPSHOT, clock=CLOCK, reissue_evidence=series
    )
    assert report.is_clean, report.conflicts
    assert (report.counts.windows_inserted, report.counts.windows_closed) == (0, 0)


def test_the_dry_run_plans_inside_a_read_only_transaction(
    conn: Connection, broken: IdentityStore, series: L0EquityListSeries
) -> None:
    conn.commit()  # the broken state is the starting point; the read-only transaction is next
    try:
        before = _nse_windows(conn)
        conn.execute("SET TRANSACTION READ ONLY")
        plan = plan_repair(
            conn,
            equity_list=NEW_LIST,
            symbol_changes="",
            snapshot_date=SNAPSHOT,
            evidence=series,
            expected=EXPECTED,
        )
        assert len(plan.deletes) == 2 and not plan.already_applied
        assert any("delete 2" in line for line in plan.describe())
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            apply_repair(conn, plan, clock=CLOCK)
        conn.rollback()
        assert _nse_windows(conn) == before
    finally:
        conn.rollback()
        for table in (
            "identity_reconciliation",
            "isin_lineage",
            "symbol_history",
            "exchange_listing",
            "security_master",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def test_a_store_that_differs_from_expected_is_refused(
    conn: Connection, broken: IdentityStore, series: L0EquityListSeries
) -> None:
    conn.execute("DELETE FROM symbol_history WHERE isin = %s", (BLSE_NEW,))
    before = _nse_windows(conn)
    with pytest.raises(RepairRefusedError, match="BLSE: expected one window each"):
        plan_repair(
            conn,
            equity_list=NEW_LIST,
            symbol_changes="",
            snapshot_date=SNAPSHOT,
            evidence=series,
            expected=EXPECTED,
        )
    assert _nse_windows(conn) == before


def test_a_boundary_without_evidence_is_refused(conn: Connection, broken: IdentityStore) -> None:
    """No L0 series: BLSE has no lineage edge, so its boundary is unknown and nothing is planned."""
    with pytest.raises(RepairRefusedError, match="does not match the expected boundaries"):
        plan_repair(
            conn, equity_list=NEW_LIST, symbol_changes="", snapshot_date=SNAPSHOT, expected=EXPECTED
        )


def test_an_unexpected_reconciliation_id_is_refused(
    conn: Connection, broken: IdentityStore, series: L0EquityListSeries
) -> None:
    wrong = (
        ExpectedReissue("TCC", TCC_OLD, TCC_NEW, TCC_SWITCH, reconciliation_id=99_999),
        EXPECTED[1],
    )
    with pytest.raises(RepairRefusedError, match="INGEST reconciliation row 99999"):
        plan_repair(
            conn,
            equity_list=NEW_LIST,
            symbol_changes="",
            snapshot_date=SNAPSHOT,
            evidence=series,
            expected=wrong,
        )


def test_a_store_changed_between_plan_and_apply_is_refused(
    conn: Connection, broken: IdentityStore, series: L0EquityListSeries
) -> None:
    plan = plan_repair(
        conn,
        equity_list=NEW_LIST,
        symbol_changes="",
        snapshot_date=SNAPSHOT,
        evidence=series,
        expected=EXPECTED,
    )
    conn.execute(
        "UPDATE identity_reconciliation SET resolved = true, resolved_at = %s, resolution = 'x' "
        "WHERE id = (SELECT min(id) FROM identity_reconciliation)",
        (CLOCK.now(),),
    )
    with pytest.raises(RepairRefusedError, match="roll back"):
        apply_repair(conn, plan, clock=CLOCK)
