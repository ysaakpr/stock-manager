"""M18.7 against a real migrated database: the Portfolio page's SQL and its dry/live read-back.

The unit suite (`tests/unit/test_console_portfolio.py`) proves the page over a stub connection
and injected journal entries; this module proves what it stubbed is what Postgres answers: the
dry stream written through `StreamJournal` (``m17-dry:<book>`` case ids, ``payload.stream``) and
read back by `read_m17_dry_journal` exactly as the books wrote it, the newest dry desk record for
the session (`paper_session.book_state`), the NSE symbol per ISIN from `symbol_history`, and the
live stream winning once it has entries. The figures are the unit fixture's hand-computed ones.

Scratch database per module, dropped afterwards; never the developer's `trading` database. Skips
loudly when the docker Postgres is unreachable. Closes come from the unit fixture's fake prices
(the lake reader is unit-tested over a parquet partition).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Json

from analyst.fundmanager.job import STREAM_DRY, StreamJournal
from analyst.journal import EvidenceStore, Journal
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.status.api import (
    app,
    clock_source,
    db_connection,
    m17_roster_source,
    settings_source,
)
from dataplatform.status.console.portfolio import portfolio_prices_source
from dataplatform.status.console.portfolio_render import paisa
from dataplatform.status.queries_portfolio import read_m17_dry_journal
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from tests.unit.test_console_portfolio import (
    D2,
    DRY_DESK,
    EXPECTED_ROWS,
    EXPECTED_TOTALS,
    ISIN_B,
    ISIN_D,
    MIRROR,
    NOW,
    PLANT,
    PRIMARY,
    ROSTER,
    SYMBOLS,
    FakePrices,
    desk_state,
    journal,
)

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_m18_7_portfolio_{os.getpid()}"


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


def _seed(conn: Connection) -> None:
    for isin, symbol in SYMBOLS.items():
        conn.execute(
            "INSERT INTO security_master (isin, name, primary_exchange, status, first_seen_date, "
            "created_at, updated_at) VALUES (%s, %s, 'NSE', 'ACTIVE', '2020-01-01', %s, %s)",
            (isin, symbol, NOW, NOW),
        )
        # An old NSE symbol and a BSE one: the current NSE symbol is the one shown.
        for exchange, shown, start, end in (
            ("NSE", f"OLD{symbol}", date(2010, 1, 1), date(2015, 12, 31)),
            ("NSE", symbol, date(2016, 1, 1), None),
            ("BSE", f"{symbol}-B", date(2016, 1, 1), None),
        ):
            conn.execute(
                "INSERT INTO symbol_history (isin, exchange, symbol, series, valid_from, "
                "valid_to, source, recorded_at) VALUES (%s, %s, %s, 'EQ', %s, %s, 'fixture', %s)",
                (isin, exchange, shown, start, end, NOW),
            )
    for book in ROSTER.books:
        for case_id in (book.id, f"{STREAM_DRY}:{book.id}"):
            conn.execute(
                "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
                "VALUES (%s, %s, 'ACTIVE', %s, %s)",
                (case_id, case_id, NOW, NOW),
            )


def _desk(conn: Connection, record: str, on: date, state: Any, recorded: datetime) -> None:
    conn.execute(
        "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, orders, "
        "journal_digest, book_state, book_digest, recorded_at) "
        "VALUES (%s, %s, 'COMPLETED', 'decided', false, %s, %s, %s, %s, %s)",
        (record, on, Json([]), "d" * 64, Json(state), "b" * 64, recorded),
    )


def _client(settings: Settings, conn: Connection, tmp_path: Path) -> TestClient:
    lake = Settings(
        database_url=settings.database_url, backup_root=tmp_path / "b", data_root=tmp_path
    )
    app.dependency_overrides[settings_source] = lambda: lake
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = lambda: conn
    app.dependency_overrides[m17_roster_source] = lambda: ROSTER
    app.dependency_overrides[portfolio_prices_source] = lambda: FakePrices()
    return TestClient(app)


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r'data-([a-z0-9-]+)="([^"]*)"', tag))


def _rows(html: str) -> dict[str, dict[str, str]]:
    out = {}
    for tag in re.findall(r"<tr data-holding=\"[^\"]*\"[^>]*>", html):
        attrs = _attrs(tag)
        out[attrs.pop("holding")] = attrs
    return out


def test_the_dry_stream_reads_back_as_the_books_wrote_it(conn: Connection, tmp_path: Path) -> None:
    _seed(conn)
    written = journal()
    stream = StreamJournal(Journal(conn, evidence=EvidenceStore(tmp_path / "evidence")), STREAM_DRY)
    for entry in written:
        stream.append(entry)
    filed = {
        row[0] for row in conn.execute("SELECT DISTINCT case_id FROM decision_journal").fetchall()
    }
    assert filed and all(case.startswith(f"{STREAM_DRY}:") for case in filed)

    back = read_m17_dry_journal(conn, data_root=tmp_path, roster=ROSTER)
    assert [(e.case_id, e.decision, e.isin, dict(e.payload)) for e in back] == [
        (e.case_id, e.decision, e.isin, dict(e.payload)) for e in written
    ]


def test_the_page_over_postgres_shows_the_dry_stream_with_the_hand_computed_figures(
    scratch_settings: Settings, conn: Connection, tmp_path: Path
) -> None:
    _seed(conn)
    stream = StreamJournal(Journal(conn, evidence=EvidenceStore(tmp_path / "evidence")), STREAM_DRY)
    for entry in journal():
        stream.append(entry)
    # The first dry desk recorded the same session earlier: the newest record is the one read.
    _desk(conn, "m17_dry_fund_managers", D2, {"books": {}}, NOW - timedelta(days=1))
    _desk(conn, DRY_DESK, D2, desk_state(), NOW)

    with _client(scratch_settings, conn, tmp_path) as client:
        html = client.get("/console/portfolio", params={"book": PRIMARY}).text
        mirror = client.get("/console/portfolio", params={"book": MIRROR}).text
        managers = client.get("/status/managers").json()
    app.dependency_overrides.clear()

    assert 'data-stream="m17-dry"' in html and "<strong>DRY RUN</strong>" in html
    assert DRY_DESK in html
    assert managers["books"] and all(b["nav"] is None for b in managers["books"])  # live: empty
    rows = _rows(html)
    for isin, expected in EXPECTED_ROWS.items():
        assert {k: rows[isin][k] for k in expected} == expected, isin
    assert (rows[ISIN_B]["action"], rows[ISIN_D]["action"]) == ("SELL", "BUY")
    tag = re.search(r'<table id="totals"[^>]*>', html)
    assert tag is not None and _attrs(tag.group(0)) == EXPECTED_TOTALS
    for symbol in SYMBOLS.values():
        assert f"<td>{symbol}</td>" in html
    assert "OLD" not in html and "-B</td>" not in html
    assert PLANT not in html
    assert {k: v["action"] for k, v in _rows(mirror).items()} == {ISIN_D: "BUY"}


def test_the_live_stream_wins_once_it_has_entries(
    scratch_settings: Settings, conn: Connection, tmp_path: Path
) -> None:
    _seed(conn)
    live = Journal(conn, evidence=EvidenceStore(tmp_path / "evidence"))
    for entry in journal():
        live.append(entry)
    _desk(conn, "m17_fund_managers", D2, desk_state(), NOW)
    _desk(conn, DRY_DESK, D2, {"books": {}}, NOW)

    with _client(scratch_settings, conn, tmp_path) as client:
        html = client.get("/console/portfolio", params={"book": PRIMARY}).text
        managers = client.get("/status/managers").json()
    app.dependency_overrides.clear()

    assert 'data-stream="m17"' in html and "DRY RUN" not in html
    assert "m17_fund_managers" in html
    tag = re.search(r'<table id="totals"[^>]*>', html)
    assert tag is not None and _attrs(tag.group(0))["nav"] == "999266.26"
    line = next(b for b in managers["books"] if b["book_id"] == PRIMARY)
    assert line["latest_session"] == D2.isoformat()
    assert paisa(Decimal(line["nav"])) == "999266.26"
