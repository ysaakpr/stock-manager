"""D2 lineage rebuild over a MANUAL edge: kept, applied, and never silently overruled.

AvenuesAI's reissue INE483S01020 -> INE483S01038 (effective 2026-10-19) is the first MANUAL
`isin_lineage` row (`repair_avenuesai`, #142). L1 contiguity derives the same pair, and before this
fix `replace_derived` inserted it again and stopped on the one-edge-per-predecessor index. These
tests drive stage 1 of `identity.lineage_rebuild` (`write_lineage`, the code `rebuild()` calls)
against a scratch Postgres database (skipped without `make up`); no lake, no network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.lineage import LineageConflictError, LineageEdge, LineageStore
from dataplatform.identity.lineage_rebuild import write_lineage
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_lineage_manual_{os.getpid()}"
CLOCK = FrozenClock(datetime(2026, 11, 1, 13, 0, tzinfo=IST))
_RECORDED = datetime(2026, 10, 29, 18, 54, tzinfo=IST)

_OLDEST = "INE483S01012"
_OLD = "INE483S01020"
_NEW = "INE483S01038"
_OTHER = "INE483S01046"
_SWITCH = date(2026, 10, 19)


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
    """A fresh store holding the AvenuesAI ISINs and the MANUAL edge exactly as #142 wrote it.

    `write_lineage` commits a queued conflict on purpose, so each test starts from a cleared
    database rather than relying on a rollback.
    """
    with connection(scratch_settings) as live:
        live.execute("DELETE FROM identity_reconciliation")
        live.execute("DELETE FROM isin_lineage")
        live.execute("DELETE FROM security_master")
        for isin in (_OLD, _NEW, _OTHER):
            live.execute(
                "INSERT INTO security_master "
                "(isin, name, primary_exchange, status, first_seen_date, created_at, updated_at) "
                "VALUES (%s, 'AvenuesAI Limited', 'NSE', 'ACTIVE', %s, %s, %s)",
                (isin, date(2026, 8, 8), CLOCK.now(), CLOCK.now()),
            )
        live.execute(
            "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, "
            "detected_by, confidence, gap_sessions, symbol_at_change, corroborating_action, "
            "computed_at) VALUES (%s, %s, %s, 'MANUAL', 'DERIVED', 4, 'CCAVENUE', NULL, %s)",
            (_OLD, _NEW, _SWITCH, _RECORDED),
        )
        live.commit()
        try:
            yield live
        finally:
            live.rollback()


def _edge(predecessor: str, successor: str, effective: date, symbol: str) -> LineageEdge:
    return LineageEdge(
        predecessor_isin=predecessor,
        successor_isin=successor,
        effective_date=effective,
        gap_sessions=4 if effective == _SWITCH else 0,
        symbol_at_change=symbol,
        corroborating_action=None,
    )


EARLIER = _edge(_OLDEST, _OLD, date(2017, 9, 1), "INFIBEAM")
#: What L1 contiguity derives for the reissue — the MANUAL edge's own pair and date.
DERIVED_AVENUESAI = _edge(_OLD, _NEW, _SWITCH, "CCAVENUE")
#: A derivation fooled into another successor for the MANUAL predecessor.
RIVAL = _edge(_OLD, _OTHER, _SWITCH, "CCAVENUE")


def _lineage(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, computed_at "
        "FROM isin_lineage ORDER BY predecessor_isin"
    ).fetchall()


def _manual_row() -> tuple[object, ...]:
    return (_OLD, _NEW, _SWITCH, "MANUAL", _RECORDED)


def test_a_rebuild_keeps_the_avenuesai_edge_and_chains_through_it(conn: Connection) -> None:
    store = LineageStore(conn, clock=CLOCK)

    report = write_lineage(conn, store, [EARLIER, DERIVED_AVENUESAI], presence={})
    conn.commit()

    rows = _lineage(conn)
    assert _manual_row() in rows
    assert [r for r in rows if r[0] == _OLD] == [_manual_row()]  # not written a second time
    assert [r[:4] for r in rows if r[3] == "L1_CONTIGUITY"] == [
        (_OLDEST, _OLD, date(2017, 9, 1), "L1_CONTIGUITY")
    ]
    assert report.manual_kept == 1
    assert report.agreeing == (DERIVED_AVENUESAI,)
    assert report.written == 1

    resolver = store.load()
    assert resolver.survivor_of(_OLDEST) == _NEW
    assert resolver.chain_to(_NEW) == (_OLDEST, _OLD, _NEW)
    assert _NEW in resolver.survivors()  # what stage 4 stitches L2 over

    # A second rebuild over the same lake changes nothing.
    write_lineage(conn, store, [EARLIER, DERIVED_AVENUESAI], presence={})
    conn.commit()
    assert _lineage(conn) == rows


def test_a_conflict_is_queued_and_raised_and_nothing_is_written(conn: Connection) -> None:
    store = LineageStore(conn, clock=CLOCK)
    write_lineage(conn, store, [EARLIER], presence={})
    conn.commit()
    before = _lineage(conn)

    with pytest.raises(LineageConflictError, match="successor_differs"):
        write_lineage(conn, store, [EARLIER, RIVAL], presence={})

    # The derived rows were not deleted and the rival was not written.
    assert _lineage(conn) == before
    queued = conn.execute(
        "SELECT kind, exchange, on_date, symbols, isins, detected_by, source, resolved "
        "FROM identity_reconciliation"
    ).fetchall()
    assert queued == [
        (
            "LINEAGE_EDGE",
            "NSE",
            _SWITCH,
            ["CCAVENUE"],
            [_OLD, _NEW, _OTHER],
            "LINEAGE_REBUILD",
            "isin_lineage",
            False,
        )
    ]

    # Re-running over the same conflict raises again and queues nothing new.
    with pytest.raises(LineageConflictError):
        write_lineage(conn, store, [EARLIER, RIVAL], presence={})
    assert _count_conflicts(conn) == 1


def test_resolving_the_conflict_lets_the_manual_edge_stand(conn: Connection) -> None:
    store = LineageStore(conn, clock=CLOCK)
    with pytest.raises(LineageConflictError):
        write_lineage(conn, store, [EARLIER, RIVAL], presence={})
    conn.execute(
        "UPDATE identity_reconciliation SET resolved = true, resolved_at = %s, "
        "resolution = 'MANUAL edge stands' WHERE kind = 'LINEAGE_EDGE'",
        (CLOCK.now(),),
    )
    conn.commit()

    report = write_lineage(conn, store, [EARLIER, RIVAL], presence={})
    conn.commit()

    assert [c.derived for c in report.overridden] == [RIVAL]
    rows = _lineage(conn)
    assert [r for r in rows if r[0] == _OLD] == [_manual_row()]
    assert all(r[1] != _OTHER for r in rows)


def _count_conflicts(conn: Connection) -> int:
    row = conn.execute("SELECT count(*) FROM identity_reconciliation").fetchone()
    assert row is not None
    return int(row[0])
