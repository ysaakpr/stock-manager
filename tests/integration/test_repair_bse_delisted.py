"""`repair_bse_delisted` against Postgres — the broken 2026-09-06 shape in, a clean master out.

The broken state is rebuilt the way the pre-fix merge wrote it: the fixed merge of the snapshot
(securities, listings, the listed scrips' windows), then an open window from the snapshot date for
every delisted-only ISIN, as the old `derive_master` opened, and the overlaps queued. The snapshot
is the record-verbatim cut of the live 2026-09-06 one (`tests/fixtures/bse_scrip_master/`), landed
in a `tmp_path` L0 and read back through `read_snapshot_from_l0`, as the command reads it.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database; each test rolls back.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    ConflictKind,
    DetectedBy,
    Exchange,
    HistoryPlan,
    IdentityConflict,
    IdentityStore,
    ListingStatus,
    SymbolWindow,
    detect_conflicts,
)
from dataplatform.identity.repair_bse_delisted import (
    ExpectedConflict,
    RepairRefusedError,
    apply_repair,
    plan_repair,
)
from dataplatform.ingest.bse.scrip_master import (
    BSE_SCRIP_MASTER_SOURCE,
    delisted_isins,
    ingest_scrip_master,
    parse_scrip_master,
)
from dataplatform.ingest.bse_scrip_refresh import SCRIP_STATUSES, SOURCE_ID, read_snapshot_from_l0
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_bse_delisted_repair_{os.getpid()}"
SNAPSHOT: Final = date(2026, 9, 6)
CLOCK = FrozenClock(datetime(2026, 9, 6, 20, 41, tzinfo=IST))
FIXTURE: Final = Path(__file__).parents[1] / "fixtures" / "bse_scrip_master" / "2026-09-06"

#: The ten as the live queue holds them; ids are this scratch database's, so not pinned.
EXPECTED: Final = (
    ExpectedConflict("ALPHA", "INE506B01016", "INF174KA1IA5"),
    ExpectedConflict("BAYERCROP", "INE556A01013", "INE462A01022"),
    ExpectedConflict("BOSTON", "INE122C01010", "INE109B01019"),
    ExpectedConflict("CMSINFO", "INE915A01011", "INE925R01014"),
    ExpectedConflict("IDEA", "INE311B01011", "INE669E01016"),
    ExpectedConflict("MILLENNIUM", "INE258D01028", "INE570N01025"),
    ExpectedConflict("ONIDA", "INE328B01015", "INE831A01028"),
    ExpectedConflict("SAWACA", "INE185B01019", "INE248B01023"),
    ExpectedConflict("SUMMITSEC", "INE852A01016", "INE519C01017"),
    ExpectedConflict("SUPHA", "INE647C01016", "INE549S01036"),
)


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
def snapshot(tmp_path: Path) -> str:
    """The fixture's three statuses landed in an L0, read back as the command reads them."""
    l0 = L0Store(clock=CLOCK, data_root=tmp_path)
    for status in SCRIP_STATUSES:
        name = f"ListofScripData_{status}.json"
        l0.put(SOURCE_ID, SNAPSHOT, name, (FIXTURE / name).read_bytes())
    return read_snapshot_from_l0(l0, SNAPSHOT)


@pytest.fixture
def broken(conn: Connection, snapshot: str) -> IdentityStore:
    """The store as the pre-fix 2026-09-06 merge left it: delisted-only ISINs hold open windows."""
    ingest_scrip_master(conn, scrip_master_json=snapshot, snapshot_date=SNAPSHOT, clock=CLOCK)
    scrips, _ = parse_scrip_master(snapshot)
    gone = delisted_isins(scrips)
    store = IdentityStore(conn, clock=CLOCK)
    store.apply_history(
        HistoryPlan(
            inserts=tuple(
                SymbolWindow(
                    Exchange.BSE, s.symbol, SNAPSHOT, None, s.isin, s.group, BSE_SCRIP_MASTER_SOURCE
                )
                for s in scrips
                if s.isin in gone
            )
        )
    )
    for conflict in detect_conflicts(store.load_windows(), source=BSE_SCRIP_MASTER_SOURCE):
        store.record(conflict)
    assert len(store.open_conflicts()) == 10
    return store


def test_the_repair_resolves_exactly_the_ten_and_raises_nothing(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    with pytest.raises(AmbiguousSymbolError):
        broken.load_master().resolve("IDEA", SNAPSHOT, exchange=Exchange.BSE)

    plan = plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED)
    assert not plan.already_applied
    assert {row.window.isin for row in plan.deletes} == {e.delisted_isin for e in EXPECTED}
    assert len(plan.resolutions) == 10
    assert (plan.conflicts_before, plan.conflicts_after) == (10, 0)

    counts = apply_repair(conn, plan, clock=CLOCK)
    assert (counts.deleted, counts.resolved) == (10, 10)

    store = IdentityStore(conn, clock=CLOCK)
    assert store.open_conflicts() == ()
    master = store.load_master()
    for e in EXPECTED:
        assert master.resolve(e.symbol, SNAPSHOT, exchange=Exchange.BSE) == e.listed_isin
        # Only the window went: the dead scrip's security and listing — its SC_CODE — stay.
        assert master.security(e.delisted_isin).status is ListingStatus.DELISTED
        listing = master.listing(e.delisted_isin, Exchange.BSE)
        assert listing is not None and listing.security_code
    assert detect_conflicts(store.load_windows(), source="test") == ()
    reasons = conn.execute(
        "SELECT resolution FROM identity_reconciliation WHERE resolved ORDER BY id"
    ).fetchall()
    assert len(reasons) == 10
    assert all("repair_bse_delisted" in str(r[0]) for r in reasons)


def test_a_second_run_is_a_no_op(conn: Connection, snapshot: str, broken: IdentityStore) -> None:
    apply_repair(
        conn, plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED), clock=CLOCK
    )
    again = plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED)
    assert again.already_applied
    assert apply_repair(conn, again, clock=CLOCK).deleted == 0


def test_a_delisted_window_another_run_wrote_is_refused(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    """A delisted ISIN's window the 2026-09-06 merge did not write is not ours to delete."""
    broken.apply_history(
        HistoryPlan(
            inserts=(
                SymbolWindow(
                    Exchange.BSE,
                    "IDEASPACE",
                    date(2026, 9, 20),
                    None,
                    "INE311B01011",
                    "B",
                    BSE_SCRIP_MASTER_SOURCE,
                ),
            )
        )
    )
    with pytest.raises(RepairRefusedError, match="not the shape"):
        plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED)


def test_a_listed_window_the_derive_does_not_produce_is_refused(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    broken.apply_history(
        HistoryPlan(
            inserts=(
                SymbolWindow(
                    Exchange.BSE, "VI", SNAPSHOT, None, "INE669E01016", "A", BSE_SCRIP_MASTER_SOURCE
                ),
            )
        )
    )
    with pytest.raises(RepairRefusedError, match="not exactly what the fixed derive"):
        plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED)


def test_a_conflict_the_repair_does_not_expect_is_refused(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    """Nine expected, ten in the store: the tenth is not the repair's to resolve unasked."""
    with pytest.raises(RepairRefusedError, match="not the expected ones"):
        plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED[:9])


def test_an_open_bse_row_the_repair_cannot_name_is_refused(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    """A queued BSE row no window explains is a different defect; it is never closed alongside."""
    broken.record(
        IdentityConflict(
            kind=ConflictKind.SYMBOL_TO_ISIN,
            exchange=Exchange.BSE,
            on_date=date(2026, 9, 7),
            symbols=("OTHER",),
            isins=("INE002A01018", "INE669E01016"),
            detected_by=DetectedBy.RESOLVE,
            source=BSE_SCRIP_MASTER_SOURCE,
        )
    )
    with pytest.raises(RepairRefusedError, match="cannot name"):
        plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED)


def test_nothing_is_written_when_the_plan_is_refused(
    conn: Connection, snapshot: str, broken: IdentityStore
) -> None:
    before = conn.execute("SELECT count(*) FROM symbol_history").fetchone()
    with pytest.raises(RepairRefusedError):
        plan_repair(conn, scrip_master_json=snapshot, expected=EXPECTED[:9])
    assert conn.execute("SELECT count(*) FROM symbol_history").fetchone() == before
    assert len(broken.open_conflicts()) == 10
