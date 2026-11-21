"""`repair_reissue` against Postgres, on a frozen cut of the real L0 captures (BNAGROCHEM).

BN Agrochem (NSE BNAGROCHEM) split ₹10 → ₹1 ex 2026-11-16 and its ISIN was reissued
INE00HZ01011 → INE00HZ01029. The broken state is not hand-built: the real ingest runs over the cut
— the Saturday 2026-11-14 snapshot (old ISIN), then the 2026-11-21 one (new ISIN) with the L0
series as reissue evidence — and reproduces what the 07:00 IST `identity_refresh` stored live:
the last old capture is a Saturday, no session lies between it and Monday's switch, so the series
refuses (`session_disagrees`), the new ISIN's window is stored from the 2026-04-20 listing date and
the overlap is queued. The repair must then leave precisely the target master.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database; each test rolls back. The L0 lake is a `tmp_path`.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
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
from dataplatform.identity.repair_avenuesai import RepairPlan, RepairRefusedError
from dataplatform.identity.repair_reissue import (
    apply_reissue_repair,
    plan_reissue_repair,
    read_switch,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_reissue_repair_{os.getpid()}"
FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "nse_equity_list" / "2026-11-bnagrochem"
OLD, NEW = "INE00HZ01011", "INE00HZ01029"
SYMBOL: Final = "BNAGROCHEM"
SWITCH: Final = date(2026, 11, 16)
BEFORE: Final = date(2026, 11, 14)
SNAPSHOT: Final = date(2026, 11, 21)
CLOCK_BEFORE = FrozenClock(datetime(2026, 11, 14, 7, 0, tzinfo=IST))
CLOCK = FrozenClock(datetime(2026, 11, 21, 7, 0, tzinfo=IST))
REPAIR_CLOCK = FrozenClock(datetime(2026, 11, 21, 15, 0, tzinfo=IST))
CAPTURES: Final = tuple(date(2026, 11, d) for d in (13, 14, 16, 21))
SESSIONS: Final = tuple(date(2026, 11, d) for d in (12, 13, 16, 17))
CA_FILE: Final = "corporateActions_20261017_20261121.json"

#: The master the repair must leave on NSE, bystanders included.
TARGET: Final = [
    (SYMBOL, OLD, date(2026, 4, 20), date(2026, 11, 15)),
    (SYMBOL, NEW, date(2026, 11, 16), None),
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


def _land(
    root: Path,
    *,
    corporate_actions: bool = True,
    edit_ca: Callable[[list[dict[str, object]]], list[dict[str, object]]] | None = None,
    edit_session: Callable[[date, str], str] | None = None,
) -> L0Store:
    """The frozen cut landed in L0 exactly where the live payloads sit, optionally altered."""
    store = L0Store(clock=CLOCK, data_root=root)
    for on_date in CAPTURES:
        name = equity_list_filename(on_date)
        store.put(
            NSE_EQUITY_LIST_SOURCE, on_date, name, (FIXTURE / "nse_equity_list" / name).read_bytes()
        )
    changes = (FIXTURE / "nse_symbol_changes" / "symbolchange_20261121.csv").read_bytes()
    for on_date in (BEFORE, SNAPSHOT):
        source, name = identity_l0_files(on_date)[1]
        store.put(source, on_date, name, changes)
    for on_date in SESSIONS:
        name = f"BhavCopy_NSE_CM_0_0_0_{on_date.strftime('%Y%m%d')}_F_0000.csv"
        text = (FIXTURE / "nse_bhavcopy_udiff" / name).read_text(encoding="utf-8")
        if edit_session is not None:
            text = edit_session(on_date, text)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, text.encode("utf-8"))
        store.put("nse_bhavcopy_udiff", on_date, name + ".zip", buffer.getvalue())
    if corporate_actions:
        payload = (FIXTURE / "nse_corp_actions" / CA_FILE).read_bytes()
        if edit_ca is not None:
            payload = json.dumps(edit_ca(json.loads(payload))).encode("utf-8")
        store.put("nse_corp_actions", SNAPSHOT, CA_FILE, payload)
    return store


@pytest.fixture
def lake(tmp_path: Path) -> L0Store:
    return _land(tmp_path)


@pytest.fixture
def broken(conn: Connection, lake: L0Store) -> IdentityStore:
    """The store as the 2026-11-21 07:00 IST ingest left it — produced by the real ingest."""
    equity_list, changes = read_snapshot_from_l0(BEFORE, store=lake)
    ingest_snapshot(
        conn,
        equity_list=equity_list,
        symbol_changes=changes,
        snapshot_date=BEFORE,
        clock=CLOCK_BEFORE,
    )
    equity_list, changes = read_snapshot_from_l0(SNAPSHOT, store=lake)
    report = ingest_snapshot(
        conn,
        equity_list=equity_list,
        symbol_changes=changes,
        snapshot_date=SNAPSHOT,
        clock=CLOCK,
        reissue_evidence=L0EquityListSeries(lake),
    )
    # The defect, reproduced: Saturday's capture has no session after it before the switch.
    assert not report.reissues
    assert {(c.symbols, c.on_date) for c in report.conflicts} == {
        ((SYMBOL,), date(2026, 4, 20)),
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


def _plan(
    conn: Connection,
    lake: L0Store,
    *,
    old: str = OLD,
    new: str = NEW,
    effective: date = SWITCH,
) -> RepairPlan:
    equity_list, changes = read_snapshot_from_l0(SNAPSHOT, store=lake)
    return plan_reissue_repair(
        conn,
        l0=lake,
        old_isin=old,
        new_isin=new,
        effective=effective,
        snapshot_date=SNAPSHOT,
        equity_list=equity_list,
        symbol_changes=changes,
        evidence=L0EquityListSeries(lake),
        note="Test note.",
    )


def test_the_broken_state_is_the_live_shape(conn: Connection, broken: IdentityStore) -> None:
    assert _nse_windows(conn) == [
        (SYMBOL, OLD, date(2026, 4, 20), None),
        (SYMBOL, NEW, date(2026, 4, 20), None),
        ("RELIANCE", "INE002A01018", date(1995, 11, 29), None),
        ("TCS", "INE467B01029", date(2004, 8, 25), None),
    ]
    with pytest.raises(AmbiguousSymbolError):
        broken.load_master().resolve(SYMBOL, date(2026, 11, 17))


def test_the_l0_switch(lake: L0Store) -> None:
    switch = read_switch(lake, old_isin=OLD, new_isin=NEW, effective=SWITCH, snapshot_date=SNAPSHOT)
    assert (switch.symbol, switch.last_old_session, switch.switch, switch.gap_sessions) == (
        SYMBOL,
        date(2026, 11, 13),
        SWITCH,
        0,
    )
    assert (switch.old_face_value, switch.new_face_value) == (Decimal(10), Decimal(1))
    assert switch.face_value_action == "face value ₹10 → ₹1 ex 2026-11-16"
    shown = {tuple(line.key.split("/")[:2]): line.isins for line in switch.evidence}
    assert shown[("nse_bhavcopy_udiff", "2026-11-13")] == (OLD,)
    assert shown[("nse_bhavcopy_udiff", "2026-11-16")] == (NEW,)
    assert shown[("nse_equity_list", "2026-11-14")] == (OLD,)
    assert shown[("nse_equity_list", "2026-11-16")] == (NEW,)
    assert shown[("nse_equity_list", "2026-11-21")] == (NEW,)
    assert shown[("nse_corp_actions", "2026-11-21")] == (OLD,)


def test_the_repair_leaves_exactly_the_target_master(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    plan = _plan(conn, lake)
    assert not plan.already_applied
    assert plan.describe()[0] == "repair_reissue 2026-11-21: plan (one transaction)"
    assert [(r.window.symbol, r.window.isin, r.window.valid_from) for r in plan.deletes] == [
        (SYMBOL, NEW, date(2026, 4, 20)),
    ]

    counts = apply_reissue_repair(conn, plan, clock=REPAIR_CLOCK)
    assert (counts.edges, counts.deleted, counts.inserted, counts.closed, counts.resolved) == (
        1,
        1,
        1,
        1,
        1,
    )

    assert _nse_windows(conn) == TARGET
    assert conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence, "
        "gap_sessions, symbol_at_change, corroborating_action FROM isin_lineage"
    ).fetchall() == [(OLD, NEW, SWITCH, "MANUAL", "DERIVED", 0, SYMBOL, None)]
    assert _open_conflicts(conn) == []
    (reason,) = conn.execute(
        "SELECT resolution FROM identity_reconciliation WHERE resolved"
    ).fetchone() or (None,)
    assert isinstance(reason, str)
    assert reason.startswith(f"repair_reissue: ISIN reissued {OLD} -> {NEW} under NSE {SYMBOL}")
    assert "last old session 2026-11-13" in reason and "face value ₹10 → ₹1" in reason
    assert reason.endswith("Test note.")

    # The boundary's direction: the old ISIN through the last old session and the weekend, the
    # new one from the switch. Inverting the dates, or the edge, fails every line here.
    master = broken.load_master()
    assert master.resolve(SYMBOL, date(2026, 11, 13)) == OLD
    assert master.resolve(SYMBOL, date(2026, 11, 15)) == OLD
    assert master.try_resolve_in_force(SYMBOL, date(2026, 11, 13)) == OLD
    for day in (16, 17, 20, 21):
        assert master.resolve(SYMBOL, date(2026, 11, day)) == NEW
        assert master.try_resolve_in_force(SYMBOL, date(2026, 11, day)) == NEW


def test_a_second_run_and_the_normal_ingest_are_no_ops(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    apply_reissue_repair(conn, _plan(conn, lake), clock=REPAIR_CLOCK)

    again = _plan(conn, lake)
    assert again.already_applied
    assert apply_reissue_repair(conn, again, clock=REPAIR_CLOCK).deleted == 0
    assert again.describe() == ["repair_reissue 2026-11-21: already applied — nothing to do"]

    # With the edge in place, re-ingesting the snapshot — and next Saturday's, which still lists
    # BNAGROCHEM from its 2026-04-20 listing date — re-opens nothing and queues nothing.
    equity_list, changes = read_snapshot_from_l0(SNAPSHOT, store=lake)
    for on_date in (SNAPSHOT, date(2026, 11, 28)):
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
        assert len(plan.deletes) == 1
        assert any("insert 1 isin_lineage edge" in line for line in plan.describe())
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            apply_reissue_repair(conn, plan, clock=REPAIR_CLOCK)
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


def test_an_inverted_pair_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    """The ISINs swapped: the switch session does not trade the 'new' one. Nothing is planned."""
    with pytest.raises(RepairRefusedError, match=f"trades {OLD} under no symbol"):
        _plan(conn, lake, old=NEW, new=OLD)
    assert not conn.execute("SELECT 1 FROM isin_lineage").fetchall()


@pytest.mark.parametrize(
    ("effective", "message"),
    [
        (date(2026, 11, 17), f"{NEW} trades before the switch"),  # a day late
        (date(2026, 11, 13), f"trades {NEW} under no symbol"),  # the last old session
        (date(2026, 11, 14), "0 bhavcopies for 2026-11-14"),  # Saturday: no session at all
    ],
)
def test_a_switch_date_the_sessions_do_not_show_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store, effective: date, message: str
) -> None:
    with pytest.raises(RepairRefusedError, match=message):
        _plan(conn, lake, effective=effective)


def test_another_issuer_code_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    with pytest.raises(RepairRefusedError, match="not one issuer code"):
        _plan(conn, lake, old="INE002A01018")


def test_both_isins_in_one_session_is_refused(
    conn: Connection, broken: IdentityStore, tmp_path: Path
) -> None:
    def both(on_date: date, text: str) -> str:
        if on_date != SWITCH:
            return text
        row = next(line for line in text.splitlines() if f",{SYMBOL}," in line)
        return text + row.replace(NEW, OLD) + "\n"

    lake = _land(tmp_path / "altered", edit_session=both)
    with pytest.raises(RepairRefusedError, match=f"{OLD} trades on or after the switch"):
        _plan(conn, lake)


def test_an_unexplained_face_value_change_is_refused(
    conn: Connection, broken: IdentityStore, tmp_path: Path
) -> None:
    lake = _land(tmp_path / "altered", corporate_actions=False)
    with pytest.raises(RepairRefusedError, match="L0 does not explain the face value"):
        _plan(conn, lake)


def test_a_corporate_action_on_another_ex_date_is_refused(
    conn: Connection, broken: IdentityStore, tmp_path: Path
) -> None:
    def late(records: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{**r, "exDate": "17-Nov-2026"} for r in records]

    lake = _land(tmp_path / "altered", edit_ca=late)
    with pytest.raises(RepairRefusedError, match="contradicts the switch"):
        _plan(conn, lake)


def test_a_stale_window_not_written_by_the_snapshot_ingest_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.execute(
        "UPDATE symbol_history SET recorded_at = %s WHERE isin = %s",
        (datetime(2026, 11, 14, 7, 0, tzinfo=IST), NEW),
    )
    with pytest.raises(RepairRefusedError, match="was recorded"):
        _plan(conn, lake)


def test_an_unrelated_open_row_on_either_isin_is_refused(
    conn: Connection, broken: IdentityStore, lake: L0Store
) -> None:
    conn.execute(
        "INSERT INTO identity_reconciliation (kind, exchange, on_date, symbols, isins, "
        "detected_by, source, raised_at) VALUES ('SYMBOL_TO_ISIN', 'BSE', %s, %s, %s, 'INGEST', "
        "'bse_scrip_master', %s)",
        (date(2026, 9, 6), [SYMBOL], sorted([OLD, NEW]), CLOCK.now()),
    )
    with pytest.raises(RepairRefusedError, match="is not this reissue's"):
        _plan(conn, lake)
