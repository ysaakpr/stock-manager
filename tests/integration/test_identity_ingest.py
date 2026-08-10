"""M1.7 acceptance against Postgres: the identity master survives being re-ingested.

The unit suite proves the rules; this proves the storage. They fail differently — a window
boundary off by a day is logic, and a re-ingest that quietly rewrites a closed window is SQL —
and the second is the one that destroys history, so it is asserted here against the real schema
rather than a mock of it.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database. Writes inside a test are rolled back: `ingest_snapshot` never
commits (the caller owns the transaction), which is exactly what makes that cleanup free.

Needs the docker postgres (`make up`). If it is unreachable the module skips with a loud reason.
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
    IdentityIngestReport,
    ingest_snapshot,
)
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    ConflictKind,
    DetectedBy,
    Exchange,
    IdentityStore,
    ListingStatus,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Suffixed with the pid: a build wave runs several agents against one Postgres, and a fixed
#: name means one session's `DROP DATABASE ... WITH (FORCE)` kills another mid-test.
SCRATCH_DB = f"trading_m1_7_identity_{os.getpid()}"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_equity_list" / "2026-08-08"

SNAPSHOT_DATE = date(2026, 8, 8)
INGESTED_AT = datetime(2026, 8, 8, 18, 30, tzinfo=IST)

#: The real rename both acceptance criterion 1 and the "history survives" test hang on.
ZYDUS_ISIN = "INE010B01027"
ZYDUS_CHANGE = date(2022, 3, 7)

#: Every column of every master table, so "nothing changed" is asserted on content and not on a
#: count that a compensating pair of edits could satisfy.
MASTER_TABLES = {
    "security_master": "isin, name, primary_exchange, status, face_value_inr, first_seen_date, "
    "last_seen_date, created_at, updated_at",
    "symbol_history": "isin, exchange, symbol, series, valid_from, valid_to, source, recorded_at",
    "exchange_listing": "isin, exchange, security_code, series, lot_size, face_value_inr, "
    "listing_date, delisting_date, status, recorded_at",
}


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url.get_secret_value(), dbname))


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

    migrate(_settings_for(SCRATCH_DB), clock=FrozenClock(INGESTED_AT))
    yield _settings_for(SCRATCH_DB)

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """A connection to the migrated scratch database, rolled back at the end of the test."""
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture(scope="session")
def equity_list_text() -> str:
    return (FIXTURES / "EQUITY_L.csv").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def symbol_change_text() -> str:
    return (FIXTURES / "symbolchange.csv").read_text(encoding="utf-8")


@pytest.fixture
def ingested(
    conn: Connection, equity_list_text: str, symbol_change_text: str
) -> IdentityIngestReport:
    """The frozen 2026-08-08 snapshot, ingested into an empty scratch database."""
    return ingest_snapshot(
        conn,
        equity_list=equity_list_text,
        symbol_changes=symbol_change_text,
        snapshot_date=SNAPSHOT_DATE,
        clock=FrozenClock(INGESTED_AT),
    )


def _equity_list(*rows: str) -> str:
    return ",".join(EQUITY_LIST_COLUMNS) + "\n" + "".join(f"{row}\n" for row in rows)


def _snapshot(conn: Connection) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every master table, ordered — the thing idempotence is measured against."""
    return {
        table: conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1, 2, 3").fetchall()
        for table, columns in MASTER_TABLES.items()
    }


# ── the frozen snapshot lands ───────────────────────────────────────────────────────────────


def test_the_snapshot_ingests_into_an_empty_database(
    conn: Connection, ingested: IdentityIngestReport
) -> None:
    assert ingested.securities_seen == 2397
    assert ingested.counts.securities == 2397
    assert ingested.counts.listings == 2397
    assert ingested.counts.windows_inserted == 2886
    assert ingested.counts.windows_closed == 0, "nothing was stored to close"
    assert ingested.is_clean, ingested.conflicts

    counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone() for table in MASTER_TABLES
    }
    assert counts == {
        "security_master": (2397,),
        "symbol_history": (2886,),
        "exchange_listing": (2397,),
    }


def test_every_stored_window_belongs_to_a_stored_security(
    conn: Connection, ingested: IdentityIngestReport
) -> None:
    """The FK proves it, but an orphaned window is the shape a symbol-keyed join would need."""
    orphans = conn.execute(
        "SELECT count(*) FROM symbol_history sh "
        "LEFT JOIN security_master sm USING (isin) WHERE sm.isin IS NULL"
    ).fetchone()
    assert orphans == (0,)


def test_a_windows_source_says_which_file_produced_it(
    conn: Connection, ingested: IdentityIngestReport
) -> None:
    rows = conn.execute(
        "SELECT symbol, source, valid_to FROM symbol_history WHERE isin = %s ORDER BY valid_from",
        (ZYDUS_ISIN,),
    ).fetchall()
    assert rows == [
        ("CADILAHC", "nse_symbol_change", date(2022, 3, 6)),
        ("ZYDUSLIFE", "nse_equity_list", None),
    ]


# ── acceptance 1: resolve across a real symbol change, from the stored master ───────────────


def test_resolve_across_a_real_rename_from_what_was_stored(
    conn: Connection, ingested: IdentityIngestReport
) -> None:
    """Acceptance 1, against Postgres rather than the in-memory derivation."""
    master = IdentityStore(conn, clock=FrozenClock(INGESTED_AT)).load_master()

    assert master.resolve("CADILAHC", date(2015, 6, 1)) == ZYDUS_ISIN
    assert master.resolve("CADILAHC", date(2022, 3, 6)) == ZYDUS_ISIN
    assert master.resolve("ZYDUSLIFE", ZYDUS_CHANGE) == ZYDUS_ISIN
    assert master.resolve("ZYDUSLIFE", SNAPSHOT_DATE) == ZYDUS_ISIN
    assert master.try_resolve("ZYDUSLIFE", date(2022, 3, 6)) is None
    assert master.symbol_as_of(ZYDUS_ISIN, date(2015, 6, 1)) == "CADILAHC"
    assert master.security(ZYDUS_ISIN).status is ListingStatus.ACTIVE
    assert master.listing(ZYDUS_ISIN, Exchange.NSE) is not None


# ── acceptance 2: re-ingest is idempotent and loses no history ─────────────────────────────


def test_re_ingesting_the_same_snapshot_changes_nothing(
    conn: Connection,
    ingested: IdentityIngestReport,
    equity_list_text: str,
    symbol_change_text: str,
) -> None:
    """Acceptance 2. Asserted on full table content, not counts: a compensating pair of edits
    keeps a count identical, and `updated_at` moving on every row would still be a rewrite."""
    before = _snapshot(conn)
    again = ingest_snapshot(
        conn,
        equity_list=equity_list_text,
        symbol_changes=symbol_change_text,
        snapshot_date=SNAPSHOT_DATE,
        # A later clock than the first run: an idempotent ingest must not even restamp a row.
        clock=FrozenClock(datetime(2026, 8, 15, 18, 30, tzinfo=IST)),
    )

    assert again.changed_nothing, again.counts
    assert again.is_clean
    assert _snapshot(conn) == before


def test_a_later_snapshot_appends_the_rename_and_keeps_the_old_window(conn: Connection) -> None:
    """Acceptance 2's other half: a symbol change appends, and prior history still resolves."""
    old = _equity_list("OLDCO,Old Co Limited,EQ,01-JAN-2010,10,1,INE111A01011,10")
    new = _equity_list("NEWCO,New Co Limited,EQ,01-JAN-2010,10,1,INE111A01011,10")
    rename = "New Co Limited,OLDCO,NEWCO,01-JUN-2022\n"

    first = ingest_snapshot(
        conn, equity_list=old, snapshot_date=date(2020, 1, 1), clock=FrozenClock(INGESTED_AT)
    )
    assert first.counts.windows_inserted == 1

    second = ingest_snapshot(
        conn,
        equity_list=new,
        symbol_changes=rename,
        snapshot_date=date(2026, 1, 1),
        clock=FrozenClock(INGESTED_AT),
    )
    assert second.counts.windows_inserted == 1, "the new symbol is appended"
    assert second.counts.windows_closed == 1, "the old window is closed, not deleted"
    assert second.is_clean

    rows = conn.execute(
        "SELECT symbol, valid_from, valid_to FROM symbol_history ORDER BY valid_from"
    ).fetchall()
    assert rows == [
        ("OLDCO", date(2010, 1, 1), date(2022, 5, 31)),
        ("NEWCO", date(2022, 6, 1), None),
    ]

    master = IdentityStore(conn).load_master()
    assert master.resolve("OLDCO", date(2015, 6, 1)) == "INE111A01011"
    assert master.resolve("NEWCO", date(2025, 6, 1)) == "INE111A01011"
    # first_seen widened backwards to the older snapshot; last_seen forward to the newer one.
    security = master.security("INE111A01011")
    assert (security.first_seen_date, security.last_seen_date) == (
        date(2020, 1, 1),
        date(2026, 1, 1),
    )


def test_an_out_of_order_snapshot_widens_the_observed_window_instead_of_rewriting_it(
    conn: Connection,
) -> None:
    """Ingesting an older snapshot after a newer one must not move `last_seen_date` backwards."""
    rows = _equity_list("OLDCO,Old Co Limited,EQ,01-JAN-2010,10,1,INE111A01011,10")
    ingest_snapshot(
        conn, equity_list=rows, snapshot_date=date(2026, 1, 1), clock=FrozenClock(INGESTED_AT)
    )
    ingest_snapshot(
        conn, equity_list=rows, snapshot_date=date(2020, 1, 1), clock=FrozenClock(INGESTED_AT)
    )
    security = IdentityStore(conn).load_master().security("INE111A01011")
    assert (security.first_seen_date, security.last_seen_date) == (
        date(2020, 1, 1),
        date(2026, 1, 1),
    )


# ── acceptance 3: ambiguity lands in the reconciliation queue ───────────────────────────────

#: A recycled symbol, badly dated: ACME is INE111A01011's current symbol *and*, per the rename
#: file, the symbol INE222B01012 traded under until 2020. The two claims overlap from 2010.
_AMBIGUOUS_LIST = _equity_list(
    "ACME,Acme Industries Limited,EQ,01-JAN-2010,10,1,INE111A01011,10",
    "ZENITH,Zenith Works Limited,EQ,01-JAN-2005,10,1,INE222B01012,10",
)
_AMBIGUOUS_CHANGES = "Zenith Works Limited,ACME,ZENITH,01-JAN-2020\n"


def _ingest_ambiguous(conn: Connection) -> IdentityIngestReport:
    return ingest_snapshot(
        conn,
        equity_list=_AMBIGUOUS_LIST,
        symbol_changes=_AMBIGUOUS_CHANGES,
        snapshot_date=date(2026, 8, 8),
        clock=FrozenClock(INGESTED_AT),
    )


def test_an_ambiguous_mapping_lands_in_the_reconciliation_queue(conn: Connection) -> None:
    """Acceptance 3, ingest half: the conflict is a row a human can find, not a log line."""
    report = _ingest_ambiguous(conn)

    assert not report.is_clean
    assert report.counts.conflicts_queued == 1
    assert report.counts.securities == 2, "the other securities still land"

    rows = conn.execute(
        "SELECT kind, exchange, on_date, symbols, isins, detected_by, resolved, raised_at "
        "FROM identity_reconciliation"
    ).fetchall()
    assert rows == [
        (
            ConflictKind.SYMBOL_TO_ISIN.value,
            Exchange.NSE.value,
            date(2010, 1, 1),
            ["ACME"],
            ["INE111A01011", "INE222B01012"],
            DetectedBy.INGEST.value,
            False,
            INGESTED_AT,
        )
    ]


def test_resolving_an_ambiguous_symbol_raises_and_queues_through_the_store(
    conn: Connection,
) -> None:
    """Acceptance 3, resolve half: the caller is stopped *and* the defect is recorded."""
    _ingest_ambiguous(conn)
    master = IdentityStore(conn, clock=FrozenClock(INGESTED_AT)).load_master()

    with pytest.raises(AmbiguousSymbolError) as raised:
        master.resolve("ACME", date(2015, 6, 1))
    assert raised.value.conflict.isins == ("INE111A01011", "INE222B01012")

    queued = conn.execute(
        "SELECT on_date, detected_by, detail->>'note' FROM identity_reconciliation "
        "WHERE detected_by = %s",
        (DetectedBy.RESOLVE.value,),
    ).fetchall()
    assert len(queued) == 1
    assert queued[0][0] == date(2015, 6, 1)
    assert "overlapping" in queued[0][2]


def test_the_queue_records_one_row_per_defect_not_one_per_lookup(conn: Connection) -> None:
    """A backfill meets the same bad symbol on every date it holds; that is one thing to fix."""
    _ingest_ambiguous(conn)
    master = IdentityStore(conn, clock=FrozenClock(INGESTED_AT)).load_master()

    for _ in range(3):
        with pytest.raises(AmbiguousSymbolError):
            master.resolve("ACME", date(2015, 6, 1))
    _ingest_ambiguous(conn)

    assert conn.execute("SELECT count(*) FROM identity_reconciliation").fetchone() == (2,)


def test_dates_outside_the_overlap_still_resolve(conn: Connection) -> None:
    """Ambiguity is scoped to the dates that are actually contested, not to the symbol."""
    _ingest_ambiguous(conn)
    master = IdentityStore(conn, clock=FrozenClock(INGESTED_AT)).load_master()
    assert master.resolve("ACME", date(2025, 1, 1)) == "INE111A01011"
    assert master.resolve("ACME", date(2007, 1, 1)) == "INE222B01012"


def test_open_conflicts_reads_the_queue_back(conn: Connection) -> None:
    _ingest_ambiguous(conn)
    store = IdentityStore(conn, clock=FrozenClock(INGESTED_AT))
    open_now = store.open_conflicts()
    assert [c.kind for c in open_now] == [ConflictKind.SYMBOL_TO_ISIN]
    assert open_now[0].symbols == ("ACME",)

    conn.execute(
        "UPDATE identity_reconciliation SET resolved = true, resolved_at = %s, "
        "resolution = 'corrected by hand'",
        (INGESTED_AT,),
    )
    assert store.open_conflicts() == ()
