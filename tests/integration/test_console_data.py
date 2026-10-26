"""M18.2 against a real migrated database: the Data page's SQL, and its parity with /status/*.

The unit suite (`tests/unit/test_console_data.py`) proves the page over a stub connection; this
module proves the queries it stubbed are the ones Postgres answers — the lineage chain's recursive
walk, the action and factor reads, the open-invalidation filter, the archive list and the
case-insensitive symbol search — and that the sync board, gaps and quality panels agree with their
endpoints over real rows.

Scratch database per module, dropped afterwards; never the developer's `trading` database. Skips
loudly when the docker Postgres is unreachable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_m18_2_console_{os.getpid()}"
NOW: Final = datetime(2026, 10, 10, 20, 0, tzinfo=IST)  # a Saturday
FRIDAY: Final = date(2026, 10, 9)
AT: Final = datetime(2026, 10, 9, 18, 30, tzinfo=IST)
PLANT: Final = "PLANTEDqd"

A: Final = "INE111A01011"  # the oldest ISIN of a three-link chain
B: Final = "INE111A01029"
C: Final = "INE111A01037"  # the live survivor
OTHER: Final = "INE222B01012"  # also traded as the same symbol, years later


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


def _security(conn: Connection, isin: str, name: str, status: str = "ACTIVE") -> None:
    conn.execute(
        "INSERT INTO security_master (isin, name, primary_exchange, status, face_value_inr, "
        "first_seen_date, last_seen_date, created_at, updated_at) "
        "VALUES (%s, %s, 'NSE', %s, 1, %s, NULL, %s, %s)",
        (isin, name, status, date(2010, 1, 4), AT, AT),
    )


def _identity(conn: Connection) -> None:
    planted = "=".join(("password", PLANT + "pw"))  # a test fake, built from fragments
    _security(conn, C, "CHAIN CO LTD")
    _security(conn, B, "CHAIN CO LTD", status="DELISTED")
    _security(conn, OTHER, "OTHER CO LTD")
    for isin, symbol, start, end in (
        (B, "CHAIN", date(2014, 1, 1), date(2019, 12, 31)),
        (C, "CHAIN", date(2020, 1, 1), None),
        (C, "CHAINNEW", date(2022, 5, 1), None),
        (OTHER, "chain", date(2005, 1, 1), date(2009, 12, 31)),
    ):
        conn.execute(
            "INSERT INTO symbol_history (isin, exchange, symbol, series, valid_from, valid_to, "
            "source, recorded_at) VALUES (%s, 'NSE', %s, 'EQ', %s, %s, 'fixture', %s)",
            (isin, symbol, start, end, AT),
        )
    for pred, succ, day in ((A, B, date(2014, 1, 1)), (B, C, date(2020, 1, 1))):
        conn.execute(
            "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, "
            "detected_by, confidence, gap_sessions, symbol_at_change, corroborating_action, "
            "computed_at) VALUES (%s, %s, %s, 'L1_CONTIGUITY', 'CORROBORATED', 0, 'CHAIN', "
            "'SPLIT', %s)",
            (pred, succ, day, AT),
        )
    row = conn.execute(
        "INSERT INTO corporate_actions (isin, ex_date, action_type, ratio_terms, record_date, "
        "knowable_date, source, recorded_at, raw_text, filed_against_isin) "
        "VALUES (%s, %s, 'SPLIT', %s, %s, %s, 'nse_ca', %s, %s, %s) RETURNING id",
        (C, date(2020, 1, 1), Jsonb({"from": 10, "to": 2}), date(2020, 1, 1),
         date(2019, 12, 1), AT, f"FV SPLIT {planted}", B),
    ).fetchone()  # fmt: skip
    assert row is not None
    conn.execute(
        "INSERT INTO corporate_actions (isin, ex_date, action_type, dividend_amount_inr, "
        "knowable_date, source, recorded_at, component) "
        "VALUES (%s, %s, 'DIVIDEND', 7.5, %s, 'nse_ca', %s, 'UNSPECIFIED')",
        (C, date(2024, 8, 2), date(2024, 7, 20), AT),
    )
    conn.execute(
        "INSERT INTO adjustment_factors (isin, ex_date, price_factor, qty_factor, "
        "cum_price_factor, cum_qty_factor, corporate_action_id, structural_break, computed_at) "
        "VALUES (%s, %s, 0.2, 5, 0.2, 5, %s, false, %s)",
        (C, date(2020, 1, 1), row[0], AT),
    )
    conn.execute(
        "INSERT INTO l2_invalidation (isin, reason, from_date, requested_at, resolved, "
        "resolved_at) VALUES (%s, 'open one', NULL, %s, false, NULL), "
        "(%s, 'drained one', NULL, %s, true, %s)",
        (C, AT, C, AT - timedelta(days=1), AT),
    )


def test_an_isin_lookup_over_real_rows_walks_the_whole_chain(
    conn: Connection, client: TestClient
) -> None:
    _identity(conn)
    html = client.get("/console/data/isin", params={"q": B}).text
    edges = re.findall(r'<tr data-lineage="([^"]+)"', html)
    assert edges == [f"{A}|{B}", f"{B}|{C}"]  # backwards to A and forwards to C, oldest first

    html = client.get("/console/data/isin", params={"q": C}).text
    assert re.findall(r'<tr data-lineage="([^"]+)"', html) == [f"{A}|{B}", f"{B}|{C}"]
    assert "CHAIN CO LTD" in html
    assert re.findall(r'<tr data-symbol="([^"]+)"', html) == ["CHAIN", "CHAINNEW"]
    assert re.findall(r'<tr data-ca="\d+" data-type="([A-Z]+)"', html) == ["DIVIDEND", "SPLIT"]
    assert "7.5</td>" in html  # the dividend, as the Decimal Postgres returned
    assert re.findall(r'<tr data-factor="([^"]+)"', html) == ["2020-01-01"]
    assert '<td class="num">0.2</td>' in html
    assert re.findall(r'<tr data-invalidation="\d+"><td>([^<]+)</td>', html) == ["open one"]
    assert PLANT not in html

    html = client.get("/console/data/isin", params={"q": A}).text
    assert "not in security_master" in html
    assert re.findall(r'<tr data-lineage="([^"]+)"', html) == [f"{A}|{B}", f"{B}|{C}"]


def test_a_symbol_search_over_real_rows_lists_every_isin_it_has_been(
    conn: Connection, client: TestClient
) -> None:
    _identity(conn)
    html = client.get("/console/data/isin", params={"q": "Chain"}).text
    assert re.findall(r'<tr data-candidate="([^"]+)"', html) == [C, B, OTHER]  # newest first
    assert 'data-isins="3"' in html
    assert 'id="actions"' not in html


def test_the_board_gaps_quality_and_archives_agree_with_their_endpoints(
    conn: Connection, client: TestClient
) -> None:
    planted = "=".join(("password", PLANT + "pw"))  # a test fake, built from fragments
    for source, day, state, error in (
        ("alpha_feed", date(2026, 10, 5), "PUBLISHED", None),
        ("alpha_feed", date(2026, 10, 7), "FAILED", f"HTTP 500 {planted}"),
        ("alpha_feed", FRIDAY, "PUBLISHED", None),
        ("beta_feed", FRIDAY, "PENDING", None),
        ("beta_feed", date(2026, 10, 6), "GAP", None),
    ):
        conn.execute(
            "INSERT INTO sync_state (source, logical_date, state, attempts, retryable, "
            "last_error, first_attempt_at, updated_at) VALUES (%s, %s, %s, 1, true, %s, %s, %s)",
            (source, day, state, error, AT, AT),
        )
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, source, detail, "
        "raised_at) VALUES (%s, 'price_jump', 'ERROR', 'alpha_feed', %s, %s), "
        "(%s, 'volume_outlier', 'WARN', NULL, '{}'::jsonb, %s)",
        (FRIDAY, Jsonb({"why": planted}), AT, FRIDAY, AT + timedelta(minutes=1)),
    )
    conn.execute(
        "INSERT INTO archive_bundle (logical_date, schema_version, bundle_path, manifest_sha256, "
        "file_count, total_bytes, manifest, published_at) "
        "VALUES (%s, '1', 'archives/2026/10/09', %s, 3, 5120, '{}'::jsonb, %s)",
        (FRIDAY, "ef" * 32, AT + timedelta(hours=1)),
    )

    html = client.get("/console/data").text
    assert 'data-unreadable="true"' not in html
    sync = client.get("/status/sync", params={"date": FRIDAY.isoformat()}).json()
    board = dict(re.findall(r'<tr data-sync-source="([^"]+)" data-state="([^"]+)"', html))
    assert board == {row["source"]: row["state"] for row in sync["rows"]}
    assert board == {"alpha_feed": "PUBLISHED", "beta_feed": "PENDING"}

    gaps = client.get("/status/gaps", params={"from": "2026-10-03", "to": "2026-10-09"}).json()
    page = dict(re.findall(r'<tr data-gap="([^"]+)" data-reason="([^"]+)"', html))
    assert page == {f"{e['source']}|{e['date']}": e["reason"] for e in gaps["unexplained"]}
    assert page["beta_feed|2026-10-06"] == "GAP_ON_A_TRADING_DAY"
    assert page["alpha_feed|2026-10-07"] == "FAILED"

    quality = client.get("/status/quality").json()
    flags = re.findall(r'<tr data-flag="(\d+)"', html)
    assert flags == [str(f["id"]) for f in quality["flags"]]
    assert f'data-open="{quality["open_total"]}"' in html

    assert '<a href="/archives?date=2026-10-09">2026-10-09</a>' in html
    assert "5.0 KiB" in html
    assert PLANT not in html
