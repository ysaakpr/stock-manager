"""M17.1 — the Commons store (migration 0018) against the docker Postgres.

The unit suite proves the builder and the in-memory store. This proves the tables:

- a build round-trips exactly: read back, it re-digests to the digest it was recorded under;
- the same build recorded twice is one build, while a changed lake's build sits beside the old one;
- all three tables refuse UPDATE, DELETE and TRUNCATE (invariant #12);
- a stored build that no longer reproduces its digest is refused on read, never served;
- the universe sheet's ISIN column refuses a symbol (invariant #2).

Needs the docker postgres (`make up`) and skips loudly if it is unreachable. No network, no lake.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Json

from analyst.commons import CommonsSheets, CommonsStoreError, PostgresCommonsStore
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from tests.unit.test_commons_sheets import SESSION, _build, _world

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_migrations).
SCRATCH_DB = f"trading_m17_1_commons_{os.getpid()}"
MIGRATED_AT = datetime(2026, 10, 8, 9, 0, tzinfo=IST)
RECORDED_AT = datetime(2026, 10, 8, 21, 5, tzinfo=IST)
TABLES = ("commons_build", "commons_market_sheet", "commons_universe_sheet")


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="module")
def scratch() -> Iterator[Settings]:
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
    settings = _settings_for(SCRATCH_DB)
    migrate(settings, clock=FrozenClock(MIGRATED_AT))
    yield settings
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture(scope="module")
def recorded(scratch: Settings) -> CommonsSheets:
    sheets = _build()
    with connection(scratch) as conn:
        assert PostgresCommonsStore(conn).record(sheets, recorded_at=RECORDED_AT) is True
        conn.commit()
    return sheets


def _count(settings: Settings, table: str) -> int:
    with connection(settings) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_a_build_round_trips_and_re_digests(scratch: Settings, recorded: CommonsSheets) -> None:
    with connection(scratch) as conn:
        back = PostgresCommonsStore(conn).get(SESSION, recorded.build_digest)
    assert back is not None
    assert back == recorded
    back.verify()
    assert _count(scratch, "commons_universe_sheet") == len(recorded.universe)


def test_the_same_build_twice_is_one_build(scratch: Settings, recorded: CommonsSheets) -> None:
    rows = _count(scratch, "commons_universe_sheet")
    with connection(scratch) as conn:
        again = _build(clock=FrozenClock(RECORDED_AT + timedelta(hours=1)))
        assert again.build_digest == recorded.build_digest
        assert PostgresCommonsStore(conn).record(again, recorded_at=RECORDED_AT) is False
        conn.commit()
    assert _count(scratch, "commons_universe_sheet") == rows


def test_a_rebuild_over_a_changed_lake_is_written_beside_the_old_one(
    scratch: Settings, recorded: CommonsSheets
) -> None:
    world = _world()
    world.macro = world.macro[1:]
    changed = _build(world)
    with connection(scratch) as conn:
        store = PostgresCommonsStore(conn)
        assert store.record(changed, recorded_at=RECORDED_AT + timedelta(hours=2)) is True
        conn.commit()
        assert store.digests(SESSION) == (recorded.build_digest, changed.build_digest)
        latest = store.latest(SESSION)
        assert latest is not None and latest.build_digest == changed.build_digest
        assert store.get(SESSION, recorded.build_digest) == recorded


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["UPDATE {t} SET trading_date = trading_date", "DELETE FROM {t}"])
def test_every_commons_table_refuses_mutation(
    scratch: Settings, recorded: CommonsSheets, table: str, verb: str
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"):
            conn.execute(verb.format(t=table))
        conn.rollback()


@pytest.mark.parametrize("table", TABLES)
def test_every_commons_table_refuses_truncate(
    scratch: Settings, recorded: CommonsSheets, table: str
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"):
            conn.execute(f"TRUNCATE {table} CASCADE")
        conn.rollback()


def test_a_build_that_does_not_reproduce_its_digest_is_refused_on_read(
    scratch: Settings, recorded: CommonsSheets
) -> None:
    forged = "f" * 64
    with connection(scratch) as conn:
        conn.execute(
            "INSERT INTO commons_build (trading_date, build_digest, market_digest, "
            "universe_digest, sheet_version, parameters, gaps, universe_size, built_at, "
            "recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s)",
            (
                SESSION,
                forged,
                recorded.market_digest,
                recorded.universe_digest,
                recorded.sheet_version,
                Json(recorded.parameters),
                Json([]),
                recorded.built_at,
                RECORDED_AT,
            ),
        )
        conn.execute(
            "INSERT INTO commons_market_sheet (trading_date, build_digest, sheet) "
            "VALUES (%s, %s, %s)",
            (SESSION, forged, Json(recorded.market.model_dump(mode="json"))),
        )
        with pytest.raises(CommonsStoreError, match="does not reproduce"):
            PostgresCommonsStore(conn).get(SESSION, forged)
        conn.rollback()


def test_the_universe_sheet_refuses_a_symbol_for_an_isin(
    scratch: Settings, recorded: CommonsSheets
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO commons_universe_sheet (trading_date, build_digest, isin, close, "
                "median_traded_value) VALUES (%s, %s, 'RELIANCE', 1, 1)",
                (SESSION, recorded.build_digest),
            )
        conn.rollback()
