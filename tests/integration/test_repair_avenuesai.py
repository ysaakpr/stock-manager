"""`repair_avenuesai` against Postgres, on a frozen cut of the real L0 captures.

The broken state is not hand-built: the real ingest is run over the cut — the 2026-10-12 snapshot
(INE483S01020, INFIBEAM renamed CCAVENUE), then the 2026-10-24 one (INE483S01038) with the L0
series as reissue evidence — and it reproduces exactly what the 07:00 IST `identity_refresh`
stored live: no boundary (the suspension breaks the series), both listing-date windows on the new
ISIN, and the two overlaps queued. The repair must then leave precisely the target master.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database; each test rolls back. The L0 lake is a `tmp_path`.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.ingest import (
    NSE_EQUITY_LIST_SOURCE,
    L0EquityListSeries,
    equity_list_filename,
    identity_l0_files,
    ingest_snapshot,
    read_snapshot_from_l0,
)
from dataplatform.identity.master import AmbiguousSymbolError, IdentityStore
from dataplatform.identity.repair_avenuesai import (
    AVENUESAI,
    ExpectedConflict,
    Reissue,
    RepairPlan,
    RepairRefusedError,
    StaleWindow,
    apply_repair,
    check_evidence,
    plan_repair,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_avenuesai_repair_{os.getpid()}"
FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "nse_equity_list" / "2026-10-avenuesai"
OLD, NEW = "INE483S01020", "INE483S01038"
BEFORE: Final = date(2026, 10, 12)
SNAPSHOT: Final = date(2026, 10, 24)
CLOCK_BEFORE = FrozenClock(datetime(2026, 10, 12, 19, 30, tzinfo=IST))
CLOCK = FrozenClock(datetime(2026, 10, 24, 7, 0, tzinfo=IST))
REPAIR_CLOCK = FrozenClock(datetime(2026, 10, 29, 18, 30, tzinfo=IST))
CAPTURES: Final = tuple(date(2026, 10, d) for d in (12, 13, 14, 15, 16, 17, 19, 24))
SESSIONS: Final = tuple(date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19))

#: The live spec with the live row ids dropped: this scratch database numbers its own rows.
REISSUE: Final = replace(
    AVENUESAI,
    stale=tuple(replace(s, row_id=None) for s in AVENUESAI.stale),
    conflicts=tuple(replace(c, reconciliation_id=None) for c in AVENUESAI.conflicts),
)

#: The master the repair must leave on NSE, bystanders included.
TARGET: Final = [
    ("CCAVENUE", OLD, date(2026, 2, 3), date(2026, 10, 18)),
    ("CCAVENUE", NEW, date(2026, 10, 19), None),
    ("INFIBEAM", OLD, date(2016, 4, 4), date(2026, 2, 2)),
    ("RELIANCE", "INE002A01018", date(1995, 11, 29), None),
    ("TCS", "INE467B01029", date(2004, 8, 25), None),
]


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
def lake(tmp_path: Path) -> L0Store:
    """The frozen cut landed in L0 exactly where the live payloads sit."""
    store = L0Store(clock=CLOCK, data_root=tmp_path)
    for on_date in CAPTURES:
        name = equity_list_filename(on_date)
        store.put(
            NSE_EQUITY_LIST_SOURCE,
            on_date,
            name,
            (FIXTURE / "nse_equity_list" / name).read_bytes(),
        )
    changes = (FIXTURE / "nse_symbol_changes" / "symbolchange_20261024.csv").read_bytes()
    for on_date in (BEFORE, SNAPSHOT):
        source, name = identity_l0_files(on_date)[1]
        store.put(source, on_date, name, changes)
    for on_date in SESSIONS:
        name = f"BhavCopy_NSE_CM_0_0_0_{on_date.strftime('%Y%m%d')}_F_0000.csv"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, (FIXTURE / "nse_bhavcopy_udiff" / name).read_bytes())
        store.put("nse_bhavcopy_udiff", on_date, name + ".zip", buffer.getvalue())
    return store


def _snapshot(lake: L0Store, on_date: date) -> tuple[str, str]:
    return read_snapshot_from_l0(on_date, store=lake)


@pytest.fixture
def broken(conn: Connection, lake: L0Store) -> IdentityStore:
    """The store as the 2026-10-24 07:00 IST ingest left it — produced by the real ingest."""
    equity_list, changes = _snapshot(lake, BEFORE)
    ingest_snapshot(
        conn,
        equity_list=equity_list,
        symbol_changes=changes,
        snapshot_date=BEFORE,
        clock=CLOCK_BEFORE,
    )
    equity_list, changes = _snapshot(lake, SNAPSHOT)
    report = ingest_snapshot(
        conn,
        equity_list=equity_list,
        symbol_changes=changes,
        snapshot_date=SNAPSHOT,
        clock=CLOCK,
        reissue_evidence=L0EquityListSeries(lake),
    )
    # The defect, reproduced: the series cannot bridge the suspension and there is no edge.
    assert not report.reissues
    assert {(c.symbols, c.on_date) for c in report.conflicts} == {
        (("INFIBEAM",), date(2016, 4, 4)),
        (("CCAVENUE",), date(2026, 2, 3)),
    }
    return IdentityStore(conn, clock=CLOCK)


def _nse_windows(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT symbol, isin, valid_from, valid_to FROM symbol_history WHERE exchange = 'NSE' "
        "ORDER BY symbol, valid_from, isin"
    ).fetchall()


def _open_conflicts(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT symbols, detected_by FROM identity_reconciliation WHERE NOT resolved ORDER BY id"
    ).fetchall()


def _plan(conn: Connection, lake: L0Store, *, reissue: Reissue = REISSUE) -> RepairPlan:
    equity_list, changes = _snapshot(lake, SNAPSHOT)
    return plan_repair(
        conn,
        l0=lake,
        equity_list=equity_list,
        symbol_changes=changes,
        snapshot_date=SNAPSHOT,
        reissue=reissue,
        evidence=L0EquityListSeries(lake),
    )


def test_the_broken_state_is_the_live_shape(conn: Connection, broken: IdentityStore) -> None:
    assert _nse_windows(conn) == [
        ("CCAVENUE", OLD, date(2026, 2, 3), None),
        ("CCAVENUE", NEW, date(2026, 2, 3), None),
        ("INFIBEAM", OLD, date(2016, 4, 4), date(2026, 2, 2)),
        ("INFIBEAM", NEW, date(2016, 4, 4), date(2026, 2, 2)),
        ("RELIANCE", "INE002A01018", date(1995, 11, 29), None),
        ("TCS", "INE467B01029", date(2004, 8, 25), None),
    ]
    with pytest.raises(AmbiguousSymbolError):
        broken.load_master().resolve("CCAVENUE", date(2026, 10, 26))


def test_the_repair_leaves_exactly_the_target_master(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    plan = _plan(conn, lake)
    assert not plan.already_applied
    assert [(r.window.symbol, r.window.isin) for r in plan.deletes] == [
        ("INFIBEAM", NEW),
        ("CCAVENUE", NEW),
    ]

    counts = apply_repair(conn, plan, clock=REPAIR_CLOCK)
    assert (counts.edges, counts.deleted, counts.inserted, counts.closed, counts.resolved) == (
        1,
        2,
        1,
        1,
        2,
    )

    assert _nse_windows(conn) == TARGET
    assert not conn.execute(
        "SELECT 1 FROM symbol_history WHERE isin = %s AND valid_from < %s", (NEW, AVENUESAI.switch)
    ).fetchall()
    assert conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, "
        "gap_sessions, symbol_at_change, corroborating_action FROM isin_lineage"
    ).fetchall() == [(OLD, NEW, date(2026, 10, 19), "MANUAL", "DERIVED", 4, "CCAVENUE", None)]
    assert _open_conflicts(conn) == []
    reasons = conn.execute(
        "SELECT symbols, resolution FROM identity_reconciliation WHERE resolved ORDER BY id"
    ).fetchall()
    assert [s for s, _ in reasons] == [["INFIBEAM"], ["CCAVENUE"]]
    assert all(r.startswith("repair_avenuesai: ISIN reissued") for _, r in reasons)

    # The boundary's direction: the old ISIN through the last old session, the new one from the
    # switch. Inverting the dates, or the edge, fails every line here.
    master = broken.load_master()
    assert master.resolve("CCAVENUE", date(2026, 10, 12)) == OLD
    assert master.resolve("CCAVENUE", date(2026, 10, 18)) == OLD
    for day in (19, 26, 27, 28, 29):
        assert master.resolve("CCAVENUE", date(2026, 10, day)) == NEW
        assert master.try_resolve_in_force("CCAVENUE", date(2026, 10, day)) == NEW
    assert master.try_resolve_in_force("CCAVENUE", date(2026, 10, 12)) == OLD
    assert master.resolve("INFIBEAM", date(2025, 1, 1)) == OLD


def test_a_second_run_and_the_normal_ingest_are_no_ops(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    apply_repair(conn, _plan(conn, lake), clock=REPAIR_CLOCK)

    again = _plan(conn, lake)
    assert again.already_applied
    assert apply_repair(conn, again, clock=REPAIR_CLOCK).deleted == 0
    assert any("already applied" in line for line in again.describe())

    # With the edge in place, re-ingesting the same snapshot — and next Saturday's, which still
    # lists CCAVENUE from its 2016 listing date — re-opens nothing and queues nothing.
    equity_list, changes = _snapshot(lake, SNAPSHOT)
    for on_date in (SNAPSHOT, date(2026, 10, 31)):
        report = ingest_snapshot(
            conn,
            equity_list=equity_list,
            symbol_changes=changes,
            snapshot_date=on_date,
            clock=FrozenClock(datetime.combine(on_date, datetime.min.time(), tzinfo=IST)),
            reissue_evidence=L0EquityListSeries(lake),
        )
        assert report.is_clean, report.conflicts
        assert (report.counts.windows_inserted, report.counts.windows_closed) == (0, 0)
        assert not report.refusals
    assert _nse_windows(conn) == TARGET
    assert _open_conflicts(conn) == []


def test_the_dry_run_plans_inside_a_read_only_transaction(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.commit()  # the broken state is the starting point; the read-only transaction is next
    try:
        before = _nse_windows(conn)
        conn.execute("SET TRANSACTION READ ONLY")
        plan = _plan(conn, lake)
        assert len(plan.deletes) == 2
        assert any("insert 1 isin_lineage edge" in line for line in plan.describe())
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            apply_repair(conn, plan, clock=REPAIR_CLOCK)
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


def test_the_l0_evidence_is_the_switch(lake: L0Store) -> None:
    lines = check_evidence(lake, AVENUESAI)
    shown = {(line.key.split("/")[0], line.key.split("/")[1]): line.isins for line in lines}
    assert shown[("nse_bhavcopy_udiff", "2026-10-12")] == (OLD,)
    assert shown[("nse_bhavcopy_udiff", "2026-10-19")] == (NEW,)
    assert all(shown[("nse_bhavcopy_udiff", f"2026-10-{d}")] == () for d in (13, 14, 15, 16))
    assert shown[("nse_equity_list", "2026-10-12")] == (OLD,)
    assert shown[("nse_equity_list", "2026-10-19")] == (NEW,)
    assert all(shown[("nse_equity_list", f"2026-10-{d}")] == () for d in (13, 14, 15, 16, 17))


def test_an_inverted_edge_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    """The ISINs swapped: L0 shows the 'old' one trading after the 'new' one. Nothing is planned."""
    inverted = replace(REISSUE, old_isin=NEW, new_isin=OLD)
    with pytest.raises(RepairRefusedError, match="L0 does not evidence the switch"):
        _plan(conn, lake, reissue=inverted)
    assert not conn.execute("SELECT 1 FROM isin_lineage").fetchall()


def test_a_switch_date_the_sessions_do_not_show_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    """A switch inside the suspension (2026-10-16) is a session that did not trade the new ISIN."""
    early = replace(REISSUE, switch=date(2026, 10, 16), gap_sessions=3)
    with pytest.raises(RepairRefusedError, match="not new only"):
        _plan(conn, lake, reissue=early)


def test_a_store_that_differs_from_expected_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.execute(
        "DELETE FROM symbol_history WHERE isin = %s AND symbol = 'INFIBEAM'",
        (NEW,),
    )
    before = _nse_windows(conn)
    with pytest.raises(RepairRefusedError, match="not the broken shape"):
        _plan(conn, lake)
    assert _nse_windows(conn) == before


def test_a_stale_window_not_written_by_the_snapshot_ingest_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.execute(
        "UPDATE symbol_history SET recorded_at = %s WHERE isin = %s",
        (datetime(2026, 10, 17, 7, 0, tzinfo=IST), NEW),
    )
    with pytest.raises(RepairRefusedError, match="was recorded"):
        _plan(conn, lake)


def test_an_unexpected_reconciliation_id_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    wrong = replace(
        REISSUE,
        conflicts=(
            ExpectedConflict("INFIBEAM", date(2016, 4, 4), 99_999),
            REISSUE.conflicts[1],
        ),
    )
    with pytest.raises(RepairRefusedError, match="expected open INGEST rows"):
        _plan(conn, lake, reissue=wrong)


def test_an_unrelated_open_row_on_either_isin_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.execute(
        "INSERT INTO identity_reconciliation (kind, exchange, on_date, symbols, isins, "
        "detected_by, source, raised_at) VALUES ('SYMBOL_TO_ISIN', 'BSE', %s, %s, %s, 'INGEST', "
        "'bse_scrip_master', %s)",
        (date(2026, 9, 6), ["CCAVENUE"], sorted([OLD, NEW]), CLOCK.now()),
    )
    with pytest.raises(RepairRefusedError, match="is not this reissue's"):
        _plan(conn, lake)


def test_a_stale_row_id_other_than_expected_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    wrong = replace(
        REISSUE,
        stale=(
            StaleWindow("INFIBEAM", date(2016, 4, 4), date(2026, 2, 2), "nse_symbol_change", 1),
            REISSUE.stale[1],
        ),
    )
    with pytest.raises(RepairRefusedError, match="expected id=1"):
        _plan(conn, lake, reissue=wrong)
