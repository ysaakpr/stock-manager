"""The thinly-traded label end to end: a lake fixture, a migrated database, the job, the readers.

A scratch lake of 251 NSE sessions holds three ISINs: one that trades every session, one that
trades one session in four, and one that trades every session we captured — with 30 sessions whose
capture is not PUBLISHED. The job must label the sparse one, leave the dense one alone, and count
the 30 sessions as capture holes of the third, never as a no-trade gap. Then the point-in-time
reader, `/status/thin-trading` and the console's Data panel must all tell the same story.

Scratch database per module, dropped afterwards; skips loudly when the docker Postgres is down.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.quality.thin_trading import (
    WINDOW_SESSIONS,
    run_thin_trading,
    trailing_sessions,
)
from dataplatform.query.thin_trading import label_session_asof, thin_trading_asof
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_thin_trading_{os.getpid()}"
AS_OF: Final = date(2026, 11, 3)
NOW: Final = datetime(2026, 11, 3, 20, 15, tzinfo=IST)
DENSE: Final = "INE100A01011"
SPARSE: Final = "INE200B01012"
HOLEY: Final = "INE300C01013"
SESSIONS: Final = trailing_sessions(trading_calendar(), AS_OF, WINDOW_SESSIONS)
WINDOW: Final = SESSIONS[1:]
HOLES: Final = frozenset(WINDOW[120:150])


def _settings_for(dbname: str, data_root: Path | None = None) -> Settings:
    base = Settings(database_url=with_dbname(Settings().database_url, dbname))
    return (
        base if data_root is None else Settings(database_url=base.database_url, data_root=data_root)
    )


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """L1 `prices_raw` for every session of the window plus the boundary session before it."""
    root = tmp_path_factory.mktemp("lake")
    for i, day in enumerate(SESSIONS):
        isins = [DENSE]
        if i % 4 == 0:
            isins.append(SPARSE)
        if day not in HOLES:
            isins.append(HOLEY)
        table = pa.table(
            {
                "isin": isins,
                "exchange": ["NSE"] * len(isins),
                "series": ["EQ"] * len(isins),
                "trade_date": pa.array([day] * len(isins), pa.date32()),
            }
        )
        part = root / "L1" / "prices_raw" / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        pq.write_table(table, part / "part.parquet")
    return root


@pytest.fixture(scope="module")
def scratch_settings(lake: Path) -> Iterator[Settings]:
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
    settings = _settings_for(SCRATCH_DB, data_root=lake)
    migrate(settings, clock=FrozenClock(NOW))
    with connection(settings) as conn:
        for day in SESSIONS:
            if day in HOLES:
                continue  # our capture of these sessions never published
            conn.execute(
                "INSERT INTO sync_state (source, logical_date, state, attempts, retryable, "
                "first_attempt_at, updated_at, unit) "
                "VALUES ('nse_bhavcopy', %s, 'PUBLISHED', 1, false, %s, %s, '')",
                (day, NOW, NOW),
            )
        for isin, name in ((DENSE, "DENSE LTD"), (SPARSE, "SPARSE LTD"), (HOLEY, "HOLEY LTD")):
            conn.execute(
                "INSERT INTO security_master (isin, name, primary_exchange, status, "
                "first_seen_date, created_at, updated_at) "
                "VALUES (%s, %s, 'NSE', 'ACTIVE', %s, %s, %s)",
                (isin, name, SESSIONS[0], NOW, NOW),
            )
        conn.commit()
    yield settings
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture(scope="module")
def labelled(scratch_settings: Settings, lake: Path) -> Settings:
    with connection(scratch_settings) as conn:
        run = run_thin_trading(conn, clock=FrozenClock(NOW), data_root=lake)
    assert run.as_of == AS_OF and run.written == 3
    return scratch_settings


@pytest.fixture
def conn(labelled: Settings) -> Iterator[Connection]:
    with connection(labelled) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def client(labelled: Settings, conn: Connection, lake: Path) -> Iterator[TestClient]:
    settings = Settings(database_url=labelled.database_url, data_root=lake)
    app.dependency_overrides[settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = lambda: conn
    try:
        with TestClient(app) as live:
            yield live
    finally:
        app.dependency_overrides.clear()


def test_sparse_is_labelled_dense_is_not_and_a_capture_hole_is_no_gap(conn: Connection) -> None:
    labels = thin_trading_asof(conn, AS_OF)
    assert set(labels) == {DENSE, SPARSE, HOLEY}
    assert not labels[DENSE].thin and labels[DENSE].traded_ratio == Decimal(1)
    assert labels[SPARSE].thin and labels[SPARSE].reasons == ("ratio",)
    holey = labels[HOLEY]
    assert holey.capture_holes == len(HOLES)
    assert holey.longest_gap_sessions == 0
    assert holey.traded_ratio == Decimal(1)
    assert not holey.thin
    # A bar is a trade whatever the capture record says: the dense name's hole sessions count.
    assert labels[DENSE].capture_holes == 0
    assert labels[SPARSE].capture_holes > 0


def test_the_reader_is_point_in_time(conn: Connection) -> None:
    assert label_session_asof(conn, AS_OF - timedelta(days=1)) is None
    assert thin_trading_asof(conn, AS_OF - timedelta(days=1)) == {}
    assert label_session_asof(conn, AS_OF + timedelta(days=5)) == AS_OF
    assert set(thin_trading_asof(conn, AS_OF, thin_only=True)) == {SPARSE}


def test_a_rerun_replaces_the_session_rather_than_adding(labelled: Settings, lake: Path) -> None:
    with connection(labelled) as conn:
        run_thin_trading(conn, clock=FrozenClock(NOW), data_root=lake, as_of=AS_OF)
        row = conn.execute(
            "SELECT count(*) FROM thin_trading_label WHERE as_of = %s", (AS_OF,)
        ).fetchone()
    assert row is not None and row[0] == 3


def test_the_status_endpoint_counts_and_lists_the_thin_names(client: TestClient) -> None:
    body = client.get("/status/thin-trading").json()
    assert body["as_of"] == AS_OF.isoformat()
    assert (body["labelled"], body["thin"], body["identity_seams"]) == (3, 1, 0)
    assert body["with_capture_holes"] == 2
    assert [n["isin"] for n in body["names"]] == [SPARSE]
    assert body["names"][0]["name"] == "SPARSE LTD"
    assert body["thresholds"] == {
        "window_sessions": 250,
        "thin_ratio_below": "0.80",
        "thin_gap_sessions": 20,
        "min_observed_sessions": 20,
    }


def test_the_console_data_page_shows_the_panel(client: TestClient) -> None:
    page = client.get("/console/data", params={"date": AS_OF.isoformat()}).text
    assert 'id="thin" data-thin="1"' in page
    assert SPARSE in page and "SPARSE LTD" in page
