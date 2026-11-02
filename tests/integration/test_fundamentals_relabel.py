"""The class-A relabel's provenance against a real migrated database (migration 0024).

A filing stored under a period its document did not state carries a `fundamentals_period_relabel`
row: the rule, the stated and stored periods and every number the check compared. The row is
written uncommitted on the runner's connection (it commits with the filing's checkpoint), and a
re-derivation of the same filing rewrites it rather than failing.
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
from dataplatform.ingest.fundamentals_relabel import recorder
from dataplatform.ingest.xbrl.q4_relabel import RULE, RelabelRecord
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_relabel_{os.getpid()}"
NOW: Final = datetime(2026, 11, 3, 2, 30, tzinfo=IST)


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="module")
def settings() -> Iterator[Settings]:
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
    scratch = _settings_for(SCRATCH_DB)
    migrate(scratch, clock=FrozenClock(NOW))
    yield scratch
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


def _record(difference: str = "0") -> RelabelRecord:
    return RelabelRecord(
        source="nse_xbrl_filing",
        unit="IF84947",
        logical_date=date(2025, 4, 15),
        filing_id="IF84947",
        isin="INE075D01018",
        nature="Standalone",
        stated_start=date(2024, 10, 1),
        stated_end=date(2025, 3, 31),
        stored_start=date(2025, 1, 1),
        stored_end=date(2025, 3, 31),
        rule=RULE,
        check_result={"concepts": {"revenue_from_operations": {"difference": difference}}},
        l0_key="nse_xbrl_filing/2025-04-15/INTEGRATED_FILING_INDAS_1414486_15042025011830_WEB.xml",
    )


def test_the_relabel_is_recorded_only_with_the_callers_commit(settings: Settings) -> None:
    with connection(settings) as conn:
        recorder(conn, FrozenClock(NOW))(_record())
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM fundamentals_period_relabel").fetchone() == (0,)
        recorder(conn, FrozenClock(NOW))(_record())
        conn.commit()
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT rule, stated_start, stored_start, stored_end, "
            "check_result->'concepts'->'revenue_from_operations'->>'difference' "
            "FROM fundamentals_period_relabel WHERE unit = 'IF84947'"
        ).fetchone()
    assert row == (RULE, date(2024, 10, 1), date(2025, 1, 1), date(2025, 3, 31), "0")


def test_a_rederivation_rewrites_the_row(settings: Settings) -> None:
    with connection(settings) as conn:
        recorder(conn, FrozenClock(NOW))(_record("1000"))
        conn.commit()
        rows = conn.execute(
            "SELECT check_result->'concepts'->'revenue_from_operations'->>'difference' "
            "FROM fundamentals_period_relabel"
        ).fetchall()
    assert rows == [("1000",)]


def test_a_stored_period_that_is_not_inside_the_stated_one_is_refused(settings: Settings) -> None:
    with connection(settings) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            recorder(conn, FrozenClock(NOW))(replace(_record(), stored_start=date(2024, 9, 1)))
        conn.rollback()
