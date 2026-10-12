"""M18.1 against a real migrated database: the Ops page's SQL, and its parity with /status/jobs.

The unit suite (`tests/unit/test_console_ops.py`) proves the page over a stub connection; this
module proves the queries it stubbed are the queries Postgres answers — `job_run` aggregation,
the drill-down's ordering and cap, open `alert_condition` rows — and that a credential written
straight into `job_run.error` (bypassing the runner's own mask) never reaches the page.

Scratch database per session, dropped afterwards; never the developer's `trading` database. Skips
loudly when the docker Postgres is unreachable.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler.registry import default_registry
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_m18_1_console_{os.getpid()}"
NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
PLANT: Final = "PLANTEDqq"


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
    settings = _settings_for(SCRATCH_DB)
    migrate(settings, clock=FrozenClock(NOW))
    yield settings
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """Rolled back at the end of the test, so each test sees only its own rows."""
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


def _run(
    conn: Connection,
    job: str,
    state: str,
    started: datetime,
    finished: datetime | None,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, state, instance, started_at, finished_at, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), job, state, "box:7", started, finished, error),
    )


@pytest.fixture
def client(scratch_settings: Settings, conn: Connection, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=scratch_settings.database_url, backup_root=tmp_path / "b", data_root=tmp_path
    )
    app.dependency_overrides[settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = lambda: conn
    try:
        with TestClient(app) as live:
            yield live
    finally:
        app.dependency_overrides.clear()


def _states(html: str) -> dict[str, str]:
    return dict(re.findall(r'<tr data-job="([^"]+)" data-state="([^"]+)"', html))


def test_the_page_and_status_jobs_agree_over_real_rows(
    conn: Connection, client: TestClient
) -> None:
    secret = "password=" + PLANT + "pw"
    eod = NOW.replace(hour=18, minute=30)
    _run(conn, "eod_pipeline", "FAILED", eod, eod + timedelta(minutes=4), f"boom {secret}")
    _run(conn, "daily_snapshot", "SUCCEEDED", NOW - timedelta(days=3), NOW - timedelta(days=3))
    for day in range(1, 61):
        start = eod - timedelta(days=day)
        _run(conn, "eod_pipeline", "SUCCEEDED", start, start + timedelta(minutes=9))
    conn.execute(
        "INSERT INTO alert_condition "
        "(dedup_key, trigger, severity, title, opened_at, last_seen_at, resolved_at) VALUES "
        "('job:eod_pipeline:failed', 'job_failed', 'critical', %s, %s, %s, NULL), "
        "('job:old:failed', 'job_failed', 'warning', 'cleared', %s, %s, %s)",
        (
            f"eod_pipeline FAILED {secret}",
            NOW - timedelta(hours=1),
            NOW,
            NOW - timedelta(days=5),
            NOW - timedelta(days=4),
            NOW - timedelta(days=4),
        ),
    )

    json_states = {j["name"]: j["state"] for j in client.get("/status/jobs").json()["jobs"]}
    html = client.get("/console").text
    assert json_states["eod_pipeline"] == "FAILING"
    assert json_states["daily_snapshot"] == "OVERDUE"
    assert _states(html) == json_states
    assert set(json_states) == {job.name for job in default_registry()}
    assert '<td class="num">4 min</td>' in html  # the failed attempt's duration
    assert "job:eod_pipeline:failed" in html and "job:old:failed" not in html

    drill = client.get("/console/ops/jobs/eod_pipeline").text
    assert drill.count("<tr data-run=") == 50
    assert drill.index("2026-10-12 18:30:00 IST") < drill.index("2026-10-11 18:30:00 IST")

    for path in ("/console", "/console/ops/jobs", "/console/ops/jobs/eod_pipeline"):
        assert PLANT not in client.get(path).text, path
    assert PLANT not in client.get("/status/jobs").text
