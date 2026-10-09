"""M17.2 — the shortlist and digest stores (migration 0019) against the docker Postgres.

The unit suites prove the rule, the digests and the in-memory stores. This proves the tables:

- a shortlist round-trips exactly and re-digests to the digest it was recorded under; recorded
  twice it is one shortlist, and a forged one is refused on read;
- a digest round-trips; a second digest of the same filing is never written, so a filing has
  one digest for every manager; a body with an opinion key is refused by the table itself;
- the window resumes after the last clean run, or at the earliest window start of failed runs;
- all four tables refuse UPDATE, DELETE and TRUNCATE (invariant #12).

Needs the docker postgres (`make up`) and skips loudly if it is unreachable. No network, no lake.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Json

from analyst.commons import (
    CommonsSheets,
    CommonsStoreError,
    DigestFailure,
    DigestRun,
    FilingDigest,
    PostgresCommonsStore,
    PostgresDigestStore,
    PostgresShortlistStore,
    Shortlist,
    build_shortlist,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from tests.unit.test_commons_digests import _run, _stub
from tests.unit.test_commons_digests import _world as _digest_world
from tests.unit.test_commons_sheets import SESSION, FakeSource
from tests.unit.test_commons_shortlist import _sheets, _shortlist_world

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB.
SCRATCH_DB = f"trading_m17_2_commons_{os.getpid()}"
MIGRATED_AT = datetime(2026, 10, 8, 9, 0, tzinfo=IST)
RECORDED_AT = datetime(2026, 10, 8, 22, 5, tzinfo=IST)
TABLES = (
    "commons_shortlist_build",
    "commons_shortlist",
    "commons_filing_digest",
    "commons_digest_run",
)


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
def sheets(scratch: Settings) -> CommonsSheets:
    built = _sheets(_shortlist_world())
    with connection(scratch) as conn:
        PostgresCommonsStore(conn).record(built, recorded_at=RECORDED_AT)
        conn.commit()
    return built


@pytest.fixture(scope="module")
def shortlist(scratch: Settings, sheets: CommonsSheets) -> Shortlist:
    built = build_shortlist(
        sheets, source=FakeSource(_shortlist_world()), clock=FrozenClock(RECORDED_AT)
    )
    with connection(scratch) as conn:
        assert PostgresShortlistStore(conn).record(built, recorded_at=RECORDED_AT) is True
        conn.commit()
    return built


@pytest.fixture(scope="module")
def digested(scratch: Settings, shortlist: Shortlist) -> FilingDigest:
    world = _digest_world()
    with connection(scratch) as conn:
        run = _run(world, PostgresDigestStore(conn), _stub(world))
        conn.commit()
        assert len(run.digested) == 3
        digest = PostgresDigestStore(conn).get("nse_announcements:r-1")
    assert digest is not None
    return digest


def test_a_shortlist_round_trips_and_re_digests(scratch: Settings, shortlist: Shortlist) -> None:
    with connection(scratch) as conn:
        back = PostgresShortlistStore(conn).latest(SESSION)
    assert back == shortlist
    assert back is not None
    back.verify()


def test_the_same_shortlist_twice_is_one(scratch: Settings, shortlist: Shortlist) -> None:
    with connection(scratch) as conn:
        assert PostgresShortlistStore(conn).record(shortlist, recorded_at=RECORDED_AT) is False
        conn.commit()
        row = conn.execute("SELECT count(*) FROM commons_shortlist_build").fetchone()
    assert row is not None and row[0] == 1


def test_a_forged_shortlist_is_refused_on_read(scratch: Settings, shortlist: Shortlist) -> None:
    forged = "e" * 64
    with connection(scratch) as conn:
        conn.execute(
            "INSERT INTO commons_shortlist_build (trading_date, shortlist_digest, build_digest, "
            "shortlist_version, rule_hash, universe_size, coverage, gaps, shortlist_size, "
            "built_at, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)",
            (
                SESSION,
                forged,
                shortlist.build_digest,
                shortlist.shortlist_version,
                shortlist.rule_hash,
                shortlist.universe_size,
                Json(shortlist.coverage),
                Json([]),
                shortlist.built_at,
                RECORDED_AT + timedelta(hours=1),
            ),
        )
        with pytest.raises(CommonsStoreError, match="does not reproduce"):
            PostgresShortlistStore(conn).latest(SESSION)
        conn.rollback()


def test_a_digest_round_trips_and_is_written_once(
    scratch: Settings, digested: FilingDigest
) -> None:
    with connection(scratch) as conn:
        store = PostgresDigestStore(conn)
        assert store.get(digested.filing_id) == digested
        second = digested.model_copy(update={"model": "another-model"})
        assert store.record(second, recorded_at=RECORDED_AT) is False
        conn.commit()
        assert store.get(digested.filing_id) == digested
    # A rebuild of the session is all cache hits: no model call at all.
    world = _digest_world()
    llm = _stub(world)
    with connection(scratch) as conn:
        run = _run(world, PostgresDigestStore(conn), llm)
        conn.commit()
    assert llm.calls == () and run.digested == ()


def test_the_table_refuses_an_opinion_key(scratch: Settings, digested: FilingDigest) -> None:
    body = {**digested.body.model_dump(mode="json"), "recommendation": "BUY"}
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO commons_filing_digest (filing_id, kind, isin, knowable_date, "
                "trading_date, digest_version, input_digest, input_truncated, prompt_digest, "
                "provider, model, body, input_tokens, output_tokens, cache_write_tokens, "
                "cache_read_tokens, digested_at, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, 1, 1, 0, 0, %s, %s)",
                (
                    "forged:1",
                    digested.kind.value,
                    digested.isin,
                    digested.knowable_date,
                    digested.trading_date,
                    digested.digest_version,
                    digested.input_digest,
                    False,
                    digested.prompt_digest,
                    digested.provider,
                    digested.model,
                    Json(body),
                    RECORDED_AT,
                    RECORDED_AT,
                ),
            )
        conn.rollback()


def test_the_window_resumes_after_the_last_clean_run(
    scratch: Settings, digested: FilingDigest
) -> None:
    day = digested.trading_date
    later, latest = day + timedelta(days=1), day + timedelta(days=2)
    with connection(scratch) as conn:
        store = PostgresDigestStore(conn)
        assert store.resume_after(later) == day  # the clean run of the fixture
        failed = DigestRun(
            trading_date=later,
            since=day,
            filing_ids=("x",),
            digested=(),
            cached=(),
            failures=(DigestFailure(filing_id="x", reason="refused"),),
            gaps=(),
            run_digest="d" * 64,
        )
        assert store.record_run(failed, recorded_at=RECORDED_AT) is True
        assert store.record_run(failed, recorded_at=RECORDED_AT) is False
        # A failed run after a clean one: the window still opens after the clean one.
        assert store.resume_after(latest) == day
        conn.rollback()


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("verb", ["UPDATE {t} SET trading_date = trading_date", "DELETE FROM {t}"])
def test_every_table_refuses_mutation(
    scratch: Settings, digested: FilingDigest, table: str, verb: str
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"):
            conn.execute(verb.format(t=table))
        conn.rollback()


@pytest.mark.parametrize("table", TABLES)
def test_every_table_refuses_truncate(
    scratch: Settings, digested: FilingDigest, table: str
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"):
            conn.execute(f"TRUNCATE {table} CASCADE")
        conn.rollback()


def test_the_shortlist_refuses_a_symbol_for_an_isin(
    scratch: Settings, shortlist: Shortlist
) -> None:
    with connection(scratch) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO commons_shortlist (trading_date, shortlist_digest, position, isin, "
                "rank_momentum_12_1, rank_relative_strength_20, rank_earnings_surprise, "
                "rank_log_liquidity, composite) VALUES (%s, %s, 99, 'RELIANCE', 0, 0, 0, 0, 0)",
                (SESSION, shortlist.shortlist_digest),
            )
        conn.rollback()
