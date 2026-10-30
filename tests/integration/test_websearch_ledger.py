"""analyst.websearch's Postgres ledger (migration 0021) against the docker Postgres.

The unit suite proves the router over the in-memory ledger. This proves the SQL: a reservation
that would overshoot the budget is refused and counts nothing (and the test fails if the
comparison is inverted), a new IST month starts at zero, `direct` is counted but uncapped, the
session cap holds, and concurrent reservers never overshoot.

Needs the docker postgres (`make up`) and skips loudly if it is unreachable. No network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import psycopg
import pytest

from analyst.websearch import Capability, PostgresUsageLedger, VendorName
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import connect, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_websearch_ledger_{os.getpid()}"
AT = datetime(2026, 10, 30, 9, 0, tzinfo=IST)
OCTOBER = date(2026, 10, 1)
NOVEMBER = date(2026, 11, 1)


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
    migrate(settings, clock=FrozenClock(AT))
    yield settings
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def ledger(scratch: Settings) -> Iterator[PostgresUsageLedger]:
    with connect(scratch, autocommit=True) as conn:
        conn.execute("TRUNCATE websearch_vendor_month, websearch_session_usage")
    store = PostgresUsageLedger(lambda: connect(scratch, autocommit=True))
    yield store
    store.close()


def test_the_budget_is_inclusive_and_an_overshoot_counts_nothing(
    ledger: PostgresUsageLedger,
) -> None:
    """Fails if the SQL comparison is inverted: 2 fit a budget of 3, a third of 2 units does not."""
    assert ledger.reserve_month(VendorName.FIRECRAWL, OCTOBER, 2, 3, at=AT) == 2
    assert ledger.reserve_month(VendorName.FIRECRAWL, OCTOBER, 2, 3, at=AT) is None
    assert ledger.reserve_month(VendorName.FIRECRAWL, OCTOBER, 1, 3, at=AT) == 3
    assert ledger.reserve_month(VendorName.FIRECRAWL, OCTOBER, 1, 3, at=AT) is None
    assert ledger.month_usage(OCTOBER) == {VendorName.FIRECRAWL: 3}


def test_a_new_month_starts_at_zero(ledger: PostgresUsageLedger) -> None:
    assert ledger.reserve_month(VendorName.BRAVE, OCTOBER, 1, 1, at=AT) == 1
    assert ledger.reserve_month(VendorName.BRAVE, OCTOBER, 1, 1, at=AT) is None
    assert ledger.reserve_month(VendorName.BRAVE, NOVEMBER, 1, 1, at=AT) == 1
    assert ledger.month_usage(OCTOBER) == {VendorName.BRAVE: 1}
    assert ledger.month_usage(NOVEMBER) == {VendorName.BRAVE: 1}


def test_direct_is_counted_without_a_cap(ledger: PostgresUsageLedger) -> None:
    for n in range(1, 6):
        assert ledger.reserve_month(VendorName.DIRECT, OCTOBER, 1, None, at=AT) == n


def test_the_session_cap(ledger: PostgresUsageLedger) -> None:
    session = date(2026, 10, 30)
    assert ledger.reserve_session(session, Capability.SEARCH, 2, at=AT) == 1
    assert ledger.reserve_session(session, Capability.SEARCH, 2, at=AT) == 2
    assert ledger.reserve_session(session, Capability.SEARCH, 2, at=AT) is None
    assert ledger.reserve_session(session, Capability.FETCH, 2, at=AT) == 1


def test_concurrent_reservers_never_overshoot(
    scratch: Settings, ledger: PostgresUsageLedger
) -> None:
    ledgers = [PostgresUsageLedger(lambda: connect(scratch, autocommit=True)) for _ in range(4)]

    def spend(store: PostgresUsageLedger) -> int:
        return sum(
            store.reserve_month(VendorName.TAVILY, OCTOBER, 1, 25, at=AT) is not None
            for _ in range(20)
        )

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            granted = sum(pool.map(spend, ledgers))
    finally:
        for store in ledgers:
            store.close()
    assert granted == 25
    assert ledger.month_usage(OCTOBER) == {VendorName.TAVILY: 25}


def test_the_tables_refuse_a_bad_month_and_an_unknown_vendor(scratch: Settings) -> None:
    with connect(scratch, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO websearch_vendor_month VALUES ('brave', DATE '2026-10-02', 0, now())"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO websearch_vendor_month VALUES ('bing', DATE '2026-10-01', 0, now())"
            )
