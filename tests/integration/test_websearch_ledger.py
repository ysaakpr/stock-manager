"""analyst.websearch's Postgres ledger (migration 0021) against the docker Postgres.

The unit suite proves the router over the in-memory ledger. This proves the SQL: a reservation
that would overshoot the vendor's budget or the monthly total is refused and counts nothing (the
tests fail if either comparison is inverted), a new IST month starts at zero, `direct` is counted
but uncapped and outside the total, the session cap holds, and concurrent reservers never
overshoot.

Needs the docker postgres (`make up`) and skips loudly if it is unreachable. No network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import psycopg
import pytest

from analyst.websearch import Capability, PostgresUsageLedger, Reservation, VendorName
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
        conn.execute(
            "TRUNCATE websearch_vendor_month, websearch_month_total, websearch_session_usage"
        )
    store = PostgresUsageLedger(lambda: connect(scratch, autocommit=True))
    yield store
    store.close()


OK, OVER_VENDOR, OVER_CAP = (
    Reservation.OK,
    Reservation.OVER_VENDOR_BUDGET,
    Reservation.OVER_MONTHLY_CAP,
)


def test_the_budget_is_inclusive_and_an_overshoot_counts_nothing(
    ledger: PostgresUsageLedger,
) -> None:
    """Fails if the SQL comparison is inverted: 2 fit a budget of 3, a third of 2 units does not."""
    fc = VendorName.FIRECRAWL
    assert ledger.reserve_call(fc, OCTOBER, 2, 3, 3000, at=AT) is OK
    assert ledger.reserve_call(fc, OCTOBER, 2, 3, 3000, at=AT) is OVER_VENDOR
    assert ledger.reserve_call(fc, OCTOBER, 1, 3, 3000, at=AT) is OK
    assert ledger.reserve_call(fc, OCTOBER, 1, 3, 3000, at=AT) is OVER_VENDOR
    assert ledger.month_usage(OCTOBER) == {fc: 3}
    assert ledger.month_requests(OCTOBER) == {fc: 2}
    # A refused vendor reservation rolled back its monthly-total bump too.
    assert ledger.month_total(OCTOBER) == 2


def test_the_monthly_cap_spans_vendors_and_rolls_back_cleanly(
    ledger: PostgresUsageLedger,
) -> None:
    """Fails if the total's comparison is inverted: a cap of 2 admits exactly two calls."""
    assert ledger.reserve_call(VendorName.BRAVE, OCTOBER, 1, 1000, 2, at=AT) is OK
    assert ledger.reserve_call(VendorName.TAVILY, OCTOBER, 1, 1000, 2, at=AT) is OK
    assert ledger.reserve_call(VendorName.FIRECRAWL, OCTOBER, 2, 1000, 2, at=AT) is OVER_CAP
    assert ledger.month_total(OCTOBER) == 2
    assert VendorName.FIRECRAWL not in ledger.month_usage(OCTOBER)


def test_a_new_month_starts_at_zero(ledger: PostgresUsageLedger) -> None:
    assert ledger.reserve_call(VendorName.BRAVE, OCTOBER, 1, 1, 3000, at=AT) is OK
    assert ledger.reserve_call(VendorName.BRAVE, OCTOBER, 1, 1, 3000, at=AT) is OVER_VENDOR
    assert ledger.reserve_call(VendorName.BRAVE, NOVEMBER, 1, 1, 3000, at=AT) is OK
    assert ledger.month_usage(OCTOBER) == {VendorName.BRAVE: 1}
    assert ledger.month_usage(NOVEMBER) == {VendorName.BRAVE: 1}
    assert (ledger.month_total(OCTOBER), ledger.month_total(NOVEMBER)) == (1, 1)


def test_direct_is_counted_without_a_cap_and_outside_the_total(
    ledger: PostgresUsageLedger,
) -> None:
    for _ in range(5):
        assert ledger.reserve_call(VendorName.DIRECT, OCTOBER, 1, None, None, at=AT) is OK
    assert ledger.month_requests(OCTOBER) == {VendorName.DIRECT: 5}
    assert ledger.month_total(OCTOBER) == 0


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
            store.reserve_call(VendorName.TAVILY, OCTOBER, 1, 1000, 25, at=AT) is OK
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
    assert ledger.month_total(OCTOBER) == 25


def test_the_tables_refuse_a_bad_month_and_an_unknown_vendor(scratch: Settings) -> None:
    with connect(scratch, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO websearch_vendor_month (vendor, month, updated_at) "
                "VALUES ('brave', DATE '2026-10-02', now())"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO websearch_vendor_month (vendor, month, updated_at) "
                "VALUES ('bing', DATE '2026-10-01', now())"
            )


def test_status_websearch_reports_month_to_date_without_keys(
    scratch: Settings, ledger: PostgresUsageLedger
) -> None:
    from pydantic import SecretStr

    from dataplatform.status.api import app
    from dataplatform.status.queries_websearch import read_websearch_usage

    key = "brave-status-test-key-not-real"
    settings = scratch.model_copy(
        update={
            "brave_search_api_key": SecretStr(key),
            "tavily_api_key": None,
            "websearch_budget_brave": 10,
        }
    )
    for _ in range(3):
        ledger.reserve_call(VendorName.BRAVE, OCTOBER, 1, 10, 3000, at=AT)
    ledger.reserve_call(VendorName.FIRECRAWL, OCTOBER, 2, 1000, 3000, at=AT)
    ledger.reserve_call(VendorName.BRAVE, NOVEMBER, 1, 10, 3000, at=AT)
    ledger.reserve_call(VendorName.DIRECT, OCTOBER, 1, None, None, at=AT)
    with connect(scratch) as conn:
        out = read_websearch_usage(conn, settings, as_of=AT)
    vendors = {v.vendor: v for v in out.vendors}
    assert out.month == OCTOBER
    assert (vendors["brave"].used, vendors["brave"].remaining) == (3, 7)
    assert vendors["brave"].configured and vendors["brave"].in_search_rotation
    assert (vendors["firecrawl"].used, vendors["firecrawl"].requests) == (2, 1)
    assert (vendors["direct"].requests, vendors["direct"].budget) == (1, None)
    assert vendors["direct"].in_fetch_rotation and not vendors["direct"].in_search_rotation
    assert (out.total_requests, out.monthly_request_cap, out.remaining_requests) == (4, 3000, 2996)
    assert vendors["tavily"].used == 0 and not vendors["tavily"].configured
    assert key not in out.model_dump_json()
    assert any(getattr(r, "path", None) == "/status/websearch" for r in app.routes)
