"""M15.4: `pending_migrations` against a real database — the answer the scheduler's guard acts on.

A scratch database per module, never the live `trading` one. Needs the docker postgres (`make up`);
skips loudly if it is unreachable.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from dataplatform.config import Settings
from dataplatform.scheduler.__main__ import schema_refusal
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import MIGRATIONS_DIR, discover, migrate, pending_migrations

pytestmark = pytest.mark.integration

#: Pid-suffixed: several agents run this suite at once against one Postgres.
SCRATCH_DB = f"trading_m15_4_guard_{os.getpid()}"


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
    yield _settings_for(SCRATCH_DB)
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


def test_the_guard_reads_without_writing_then_tracks_migrate(
    scratch: Settings, tmp_path: Path
) -> None:
    # An empty database has applied nothing — and asking must not create the ledger.
    assert [m.version for m in pending_migrations(scratch)] == [m.version for m in discover()]
    with connection(scratch) as conn:
        ledger = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    assert ledger is not None and ledger[0] is None
    refusal = schema_refusal(scratch)
    assert refusal is not None and "make migrate" in refusal

    migrate(scratch)
    assert pending_migrations(scratch) == []
    assert schema_refusal(scratch) is None

    # A file merged but not yet migrated: exactly that one is pending, and the guard refuses.
    staged = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, staged)
    (staged / "9999_not_applied.sql").write_text("SELECT 1;\n")
    assert [m.path.name for m in pending_migrations(scratch, directory=staged)] == [
        "9999_not_applied.sql"
    ]
