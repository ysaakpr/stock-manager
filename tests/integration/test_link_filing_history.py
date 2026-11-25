"""M18.1: `link_filing_history` writes in one transaction, all or nothing; a re-run is a no-op.

A scratch database holds KOTAKBANK's live shape: the survivor in the master, the retired middle
`INE237A01028` only as the predecessor of the stored L1 edge, and NSE's filed `INE237A01010`
nowhere. The facts are the unit tests' accepted ones; this file is about the store.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.filing_isin import FilingIsinStatus, load_filing_resolver
from dataplatform.identity.lineage import LINEAGE_BACKFILL, EqPresence
from dataplatform.identity.link_filing_history import (
    LinkPlan,
    LinkRefusedError,
    apply_plan,
    plan_links,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from tests.unit.test_link_filing_history import (
    HUDCO,
    HUDCO_FACTS,
    KOTAK,
    KOTAK_FACTS,
    KOTAK_MID,
    KOTAK_NOW,
    KOTAK_OLD,
)

SCRATCH_DB = f"trading_link_filing_history_{os.getpid()}"
CLOCK: Final = FrozenClock(datetime(2026, 11, 25, 11, 0, tzinfo=IST))
PRESENCE: Final = {
    KOTAK_MID: EqPresence(KOTAK_MID, date(2010, 9, 20), date(2026, 1, 13), "KOTAKBANK")
}


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
    with connection(scratch_settings) as live:
        live.execute("DELETE FROM isin_lineage")
        live.execute("DELETE FROM security_master")
        for isin, name in ((KOTAK_NOW, "Kotak Mahindra Bank Ltd"), (HUDCO.isin, "HUDCO")):
            live.execute(
                "INSERT INTO security_master "
                "(isin, name, primary_exchange, status, first_seen_date, created_at, updated_at) "
                "VALUES (%s, %s, 'NSE', 'ACTIVE', %s, %s, %s)",
                (isin, name, date(2026, 8, 8), CLOCK.now(), CLOCK.now()),
            )
        live.execute(
            "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, "
            "detected_by, confidence, gap_sessions, symbol_at_change, corroborating_action, "
            "computed_at) VALUES (%s, %s, %s, 'L1_CONTIGUITY', 'CORROBORATED', 0, 'KOTAKBANK', "
            "'SPLIT', %s)",
            (KOTAK_MID, KOTAK_NOW, date(2026, 1, 14), CLOCK.now()),
        )
        live.commit()
        try:
            yield live
        finally:
            live.rollback()


def _plan(conn: Connection, facts: object = KOTAK_FACTS) -> LinkPlan:
    return plan_links(
        conn,
        [KOTAK],
        [HUDCO],
        pre_era={KOTAK_OLD: facts},  # type: ignore[dict-item]
        alias_facts={HUDCO.filed_isin: HUDCO_FACTS},
        presence=PRESENCE,
    )


def _lineage(conn: Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT predecessor_isin, successor_isin, effective_date, detected_by, confidence "
        "FROM isin_lineage ORDER BY predecessor_isin"
    ).fetchall()


def test_apply_registers_the_middle_and_writes_the_manual_edge(conn: Connection) -> None:
    plan = _plan(conn)
    assert not plan.refused
    assert [s.isin for s in plan.register] == [KOTAK_MID]

    assert apply_plan(conn, plan, clock=CLOCK) == (1, 1)
    conn.commit()

    assert _lineage(conn) == [
        (KOTAK_OLD, KOTAK_MID, date(2010, 9, 20), "MANUAL", "DERIVED"),
        (KOTAK_MID, KOTAK_NOW, date(2026, 1, 14), "L1_CONTIGUITY", "CORROBORATED"),
    ]
    assert conn.execute(
        "SELECT status, registered_by, first_seen_date, last_seen_date FROM security_master "
        "WHERE isin = %s",
        (KOTAK_MID,),
    ).fetchone() == ("DELISTED", LINEAGE_BACKFILL, date(2010, 9, 20), date(2026, 1, 13))
    assert conn.execute(
        "SELECT count(*) FROM security_master WHERE isin = %s", (KOTAK_OLD,)
    ).fetchone() == (0,)

    # The filing resolver now carries both shapes to the live equity.
    resolver = load_filing_resolver(conn, aliases=[HUDCO])
    kotak = resolver.resolve(KOTAK_OLD, date(2026, 11, 20))
    assert (kotak.isin, kotak.status) == (KOTAK_NOW, FilingIsinStatus.REKEYED)
    assert resolver.resolve(HUDCO.filed_isin, date(2026, 11, 20)).isin == HUDCO.isin


def test_a_second_run_is_a_no_op(conn: Connection) -> None:
    apply_plan(conn, _plan(conn), clock=CLOCK)
    conn.commit()
    before = _lineage(conn)

    again = _plan(
        conn,
        replace(
            KOTAK_FACTS,
            old_edges=((KOTAK_OLD, KOTAK_MID, KOTAK.effective_date, "MANUAL"),),
            successor_predecessors=(KOTAK_OLD,),
            successor_in_master=True,
        ),
    )
    assert not again.refused
    assert (again.edges, again.register) == ((), ())
    assert apply_plan(conn, again, clock=CLOCK) == (0, 0)
    conn.commit()
    assert _lineage(conn) == before


def test_one_refused_entry_writes_nothing(conn: Connection) -> None:
    """The old ISIN turns out to be another security's: the whole plan is refused, unwritten."""
    plan = _plan(conn, replace(KOTAK_FACTS, old_in_master=True))
    assert plan.refused
    before = _lineage(conn)
    with pytest.raises(LinkRefusedError, match="nothing written"):
        apply_plan(conn, plan, clock=CLOCK)
    conn.rollback()
    assert _lineage(conn) == before
    assert conn.execute(
        "SELECT count(*) FROM security_master WHERE isin = %s", (KOTAK_MID,)
    ).fetchone() == (0,)


def test_an_unregistrable_middle_is_refused(conn: Connection) -> None:
    """Without L1 EQ bars the middle cannot be registered, so the edge has nowhere to land."""
    plan = plan_links(
        conn,
        [KOTAK],
        [],
        pre_era={KOTAK_OLD: KOTAK_FACTS},
        alias_facts={},
        presence={},
    )
    assert plan.refused
    assert "no_l1_eq_history" in " ".join(plan.refusals[KOTAK_OLD]).lower()
