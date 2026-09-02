"""M2.2 acceptance against Postgres: NSE and BSE corporate actions ingest into one model.

The unit suites prove the pieces — M2.1 that a purpose string normalizes correctly, M1.7 that a
symbol resolves to an ISIN. This proves the join: that two exchange feeds written in two dialects,
one keyed on ISIN and one on a scrip code, land as `corporate_actions` rows that agree on the
normalized action, each carrying its ISIN (resolved through D2), its ex-date, its source and the
L0 payload it was derived from.

It exercises the real path end to end — bytes into L0, `parse_l0` back out with a checksum, resolve
through the loaded identity master, persist, read back — because the failure that matters here is a
storage or resolution one (a scrip that silently resolves to the wrong ISIN, a jsonb terms blob
that does not round-trip), and a mock of the schema would not show it.

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database. Needs the docker postgres (`make up`); skips loudly if unreachable.
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
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.master import IdentityMaster, IdentityStore
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.corp_actions import (
    build_scrip_index,
    load_corporate_actions,
    write_corporate_actions,
)
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Suffixed with the pid: a build wave runs several agents against one Postgres, and a fixed name
#: means one session's `DROP DATABASE ... WITH (FORCE)` kills another mid-test (ops/BACKLOG.md).
SCRATCH_DB = f"trading_m2_2_corp_actions_{os.getpid()}"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "corp_actions"
NSE_FIXTURE = FIXTURES / "nse" / "2026-08-08" / "corporateActions.json"
BSE_FIXTURE = FIXTURES / "bse" / "2026-08-08" / "defaultdata.json"

INGESTED_AT = datetime(2026, 8, 8, 18, 30, tzinfo=IST)
CLOCK = FrozenClock(INGESTED_AT)

#: The universe both fixtures describe: (ISIN, NSE symbol, BSE scrip code). Seeded into the
#: identity master so the NSE feed's native ISIN validates and the BSE feed's scrip resolves.
UNIVERSE = [
    ("INE002A01018", "RELIANCE", "500325"),
    ("INE009A01021", "INFY", "500209"),
    ("INE081A01020", "TATASTEEL", "500470"),
    ("INE075A01022", "WIPRO", "507685"),
    ("INE001A01036", "HDFC", "500010"),
]

#: The five actions both exchanges describe, keyed by ISIN → (ex_date, normalized type).
EXPECTED = {
    "INE002A01018": (date(2024, 10, 28), ActionType.BONUS),
    "INE009A01021": (date(2024, 11, 5), ActionType.DIVIDEND),
    "INE081A01020": (date(2024, 9, 16), ActionType.SPLIT),
    "INE075A01022": (date(2024, 12, 2), ActionType.RIGHTS),
    "INE001A01036": (date(2023, 7, 13), ActionType.MERGER),
}


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
    """A connection to the migrated scratch database, rolled back at the end of the test."""
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def master(conn: Connection) -> IdentityMaster:
    """An identity master seeded with the fixtures' universe: NSE symbol windows + BSE scrips.

    Seeded directly rather than through M1.7's NSE ingest because that path builds no BSE listings
    (it reads EQUITY_L.csv only), and the BSE scrip→ISIN resolution is exactly what M2.2 must
    exercise. The rows are the minimum the resolver reads: a security, an open NSE symbol window,
    and a BSE listing carrying the scrip code.
    """
    for isin, symbol, scrip in UNIVERSE:
        conn.execute(
            "INSERT INTO security_master "
            "(isin, name, primary_exchange, status, first_seen_date, created_at, updated_at) "
            "VALUES (%s, %s, 'NSE', 'ACTIVE', %s, %s, %s)",
            (isin, symbol, date(2000, 1, 1), INGESTED_AT, INGESTED_AT),
        )
        conn.execute(
            "INSERT INTO symbol_history "
            "(isin, exchange, symbol, series, valid_from, valid_to, source, recorded_at) "
            "VALUES (%s, 'NSE', %s, 'EQ', %s, NULL, 'test', %s)",
            (isin, symbol, date(2000, 1, 1), INGESTED_AT),
        )
        conn.execute(
            "INSERT INTO exchange_listing "
            "(isin, exchange, security_code, series, status, recorded_at) "
            "VALUES (%s, 'BSE', %s, 'A', 'ACTIVE', %s)",
            (isin, scrip, INGESTED_AT),
        )
    return IdentityStore(conn, clock=CLOCK).load_master()


@pytest.fixture
def l0(tmp_path: Path) -> L0Store:
    """An L0 store rooted in a temp dir, so `parse_l0` reads bytes back with a real checksum."""
    return L0Store(clock=CLOCK, data_root=tmp_path)


@pytest.fixture
def ingested(conn: Connection, master: IdentityMaster, l0: L0Store) -> dict[str, object]:
    """Both feeds taken bytes → L0 → parse_l0 → persist. Returns the two parse results.

    The whole pipeline under test, run once per test that needs the stored rows.
    """
    nse_ref = l0.put(
        nse_ca.SOURCE_ID,
        date(2026, 8, 8),
        "corporateActions_20260808.json",
        NSE_FIXTURE.read_bytes(),
        content_type="application/json",
    )
    bse_ref = l0.put(
        bse_ca.SOURCE_ID,
        date(2026, 8, 8),
        "defaultdata_20260808.json",
        BSE_FIXTURE.read_bytes(),
        content_type="application/json",
    )

    nse_result = nse_ca.parse_l0(l0, nse_ref, master=master, clock=CLOCK)
    bse_result = bse_ca.parse_l0(l0, bse_ref, scrip_index=build_scrip_index(master), clock=CLOCK)

    write_corporate_actions(conn, nse_result.actions, clock=CLOCK)
    write_corporate_actions(conn, bse_result.actions, clock=CLOCK)
    return {"nse": nse_result, "bse": bse_result, "nse_key": nse_ref.key, "bse_key": bse_ref.key}


# ── acceptance 1: both feeds parse into the same normalized model ────────────────────────────


def test_both_feeds_land_the_same_normalized_actions(
    conn: Connection, ingested: dict[str, object]
) -> None:
    """Acceptance 1: NSE (ISIN-native) and BSE (scrip-keyed) agree on type and terms per ISIN."""
    nse_rows = {a.isin: a for a in load_corporate_actions(conn, source=nse_ca.SOURCE_ID)}
    bse_rows = {a.isin: a for a in load_corporate_actions(conn, source=bse_ca.SOURCE_ID)}

    assert set(nse_rows) == set(EXPECTED)
    assert set(bse_rows) == set(EXPECTED)

    for isin, (ex_date, action_type) in EXPECTED.items():
        nse, bse = nse_rows[isin], bse_rows[isin]
        assert (nse.ex_date, nse.action_type) == (ex_date, action_type)
        assert (bse.ex_date, bse.action_type) == (ex_date, action_type)
        # The point of "same model": two dialects, byte-identical structured terms and rendering.
        assert nse.terms == bse.terms, isin
        assert nse.describe() == bse.describe(), isin


def test_the_terms_are_the_real_structured_values_not_placeholders(
    conn: Connection, ingested: dict[str, object]
) -> None:
    """A split's face values, a dividend's amount and a rights premium survive the round trip."""
    rows = {a.isin: a for a in load_corporate_actions(conn, source=bse_ca.SOURCE_ID)}
    assert rows["INE081A01020"].describe() == "Face value split from Rs.10 to Rs.1"
    assert rows["INE009A01021"].dividend_amount_inr is not None
    assert (
        rows["INE075A01022"].describe()
        == "Rights issue in the ratio 1:5 at a premium of Rs.90 per share"
    )


# ── acceptance 2: every row carries ISIN (via D2), ex_date, source and L0 lineage ───────────


def test_every_stored_row_carries_isin_exdate_source_and_l0_lineage(
    conn: Connection, ingested: dict[str, object]
) -> None:
    """Acceptance 2, asserted in SQL: no row is missing any of the four, and the ISIN is a real
    security_master key (the FK proves the join is on ISIN, not a symbol)."""
    incomplete = conn.execute(
        "SELECT count(*) FROM corporate_actions "
        "WHERE isin IS NULL OR ex_date IS NULL OR source IS NULL OR l0_key IS NULL "
        "OR raw_text IS NULL"
    ).fetchone()
    assert incomplete == (0,)

    orphans = conn.execute(
        "SELECT count(*) FROM corporate_actions ca "
        "LEFT JOIN security_master sm USING (isin) WHERE sm.isin IS NULL"
    ).fetchone()
    assert orphans == (0,)

    # Ten rows: five actions from each exchange, each naming the L0 payload it came from.
    total = conn.execute("SELECT count(*) FROM corporate_actions").fetchone()
    assert total == (10,)
    l0_keys = {
        row[0] for row in conn.execute("SELECT DISTINCT l0_key FROM corporate_actions").fetchall()
    }
    assert l0_keys == {ingested["nse_key"], ingested["bse_key"]}


def test_bse_rows_were_resolved_from_scrip_to_isin_through_d2(
    conn: Connection, ingested: dict[str, object]
) -> None:
    """The BSE feed carries no ISIN; each stored row's ISIN came from the scrip master, and the
    scrip code it resolved from is kept in source_ref for traceability."""
    rows = load_corporate_actions(conn, source=bse_ca.SOURCE_ID)
    scrip_by_isin = {isin: scrip for isin, _, scrip in UNIVERSE}
    for action in rows:
        assert action.isin in scrip_by_isin
        assert action.source_ref == scrip_by_isin[action.isin]


def test_nse_knowable_date_is_the_broadcast_date_not_the_ex_date(
    conn: Connection, ingested: dict[str, object]
) -> None:
    """Invariant #7's hook: the NSE row is knowable from its broadcast date, before its ex-date."""
    bonus = next(
        a for a in load_corporate_actions(conn, source=nse_ca.SOURCE_ID) if a.isin == "INE002A01018"
    )
    assert bonus.knowable_date == date(2024, 9, 10)
    assert bonus.announcement_date == date(2024, 9, 10)
    assert bonus.knowable_date < bonus.ex_date


# ── leftovers are surfaced, never silently dropped ──────────────────────────────────────────


def test_unclassifiable_and_unresolvable_rows_are_surfaced_not_dropped(
    ingested: dict[str, object],
) -> None:
    """A subject with no known action keyword is queued; a scrip the master never saw is unresolved.
    Neither becomes a row, and neither is lost — both are returned for a human to act on."""
    nse_result = ingested["nse"]
    bse_result = ingested["bse"]

    queued_subjects = {entry.raw_text for entry in nse_result.queued}  # type: ignore[attr-defined]
    assert "Annual General Meeting" in queued_subjects

    unresolved_scrips = {u.source_ref for u in bse_result.unresolved}  # type: ignore[attr-defined]
    assert unresolved_scrips == {"999999"}


# ── idempotence: re-ingesting the same feed writes nothing ──────────────────────────────────


def test_re_ingesting_the_same_feed_changes_nothing(
    conn: Connection, master: IdentityMaster, l0: L0Store, ingested: dict[str, object]
) -> None:
    """Idempotent per (isin, ex_date, action_type, source): a second run of an unchanged feed
    inserts zero rows and does not restamp the ones already there."""
    before = conn.execute(
        "SELECT isin, ex_date, action_type, source, recorded_at FROM corporate_actions "
        "ORDER BY isin, source, ex_date"
    ).fetchall()

    nse_ref = l0.ref_for(nse_ca.SOURCE_ID, date(2026, 8, 8), "corporateActions_20260808.json")
    again = nse_ca.parse_l0(
        l0, nse_ref, master=master, clock=FrozenClock(datetime(2026, 9, 1, 9, 0, tzinfo=IST))
    )
    counts = write_corporate_actions(
        conn, again.actions, clock=FrozenClock(datetime(2026, 9, 1, 9, 0, tzinfo=IST))
    )

    assert counts.inserted == 0
    assert counts.skipped == len(again.actions)
    after = conn.execute(
        "SELECT isin, ex_date, action_type, source, recorded_at FROM corporate_actions "
        "ORDER BY isin, source, ex_date"
    ).fetchall()
    assert after == before


# ── acceptance 3: historical depth is recorded in the source register ───────────────────────


def test_historical_depth_is_recorded_for_both_ca_sources() -> None:
    """Acceptance 3: the measured depth of each feed is in source_register.yaml, so the M2 golden
    suite reads a measurement rather than an assumption about how far back the sources reach."""
    register = load_register()
    for source_id in (nse_ca.SOURCE_ID, bse_ca.SOURCE_ID):
        source = next(s for s in register.sources if s.id == source_id)
        assert source.history is not None, source_id
        assert source.history.method
        assert source.history.note
        assert source.parser.task == "M2.2"
