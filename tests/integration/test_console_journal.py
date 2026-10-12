"""M18.4 against a real migrated database: the Journal page's SQL over real `decision_journal` rows.

The unit suite (`tests/unit/test_console_journal.py`) interprets the page's SQL over a stub; this
module proves Postgres answers it the same way — every filter (including `payload->>'book'`), the
keyset walk over the identity sequence, the case aggregation, the evidence citation counts — and
that a credential written straight into a payload and a rationale never reaches the page.

Scratch database per module, dropped afterwards; never the developer's `trading` database. Each
test's rows are rolled back. Skips loudly when the docker Postgres is unreachable.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Final

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

SCRATCH_DB: Final = f"trading_m18_4_console_{os.getpid()}"
NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
PLANT: Final = "PLANTEDkk"
BOOKS: Final = ("FM-MOM-10L", "FM-MOM-1CR")
CASE: Final = "CASE-INT-01"
KINDS: Final = ("BUY", "HEARTBEAT", "SELL", "HOLD", "SKIPPED_DATA_RED")
BUNDLE: Final = b'{"items":[],"schema":1}'
REF: Final = "sha256:" + hashlib.sha256(BUNDLE).hexdigest()

_ROW: Final = re.compile(r'<tr data-entry="(\d+)" data-decision="([A-Z_]+)" data-noop="([01])"')
_NEXT: Final = re.compile(r'<a id="next-page" href="([^"]+)"')


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
    settings = Settings(database_url=scratch_settings.database_url, data_root=tmp_path)
    app.dependency_overrides[settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = lambda: conn
    try:
        with TestClient(app) as live:
            yield live
    finally:
        app.dependency_overrides.clear()


def _seed(conn: Connection) -> list[dict[str, Any]]:
    """Cases, then 140 entries across one case, two M17 streams and the system; returns them."""
    for case_id, state in ((CASE, "ACTIVE"), (BOOKS[0], "FUNDED"), (BOOKS[1], "FUNDED")):
        conn.execute(
            "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (case_id, f"{case_id} title", state, NOW - timedelta(days=30), NOW),
        )
    planted_dsn = "postgresql://" + ":".join(("fm", PLANT + "pw")) + "@db/trading"
    seeded = []
    for i in range(140):
        case = (CASE, BOOKS[0], BOOKS[1], None)[i % 4]
        payload: dict[str, Any] = {"book": case, "mode": "PAPER"} if case in BOOKS else {}
        rationale = f"entry {i}"
        if i == 41:
            payload = {**payload, "note": f"via {planted_dsn}", "api_key": PLANT + "k"}
            rationale = "retried with " + "=".join(("password", PLANT + "pw2"))
        day = date(2026, 9, 1) + timedelta(days=i // 5)
        decision = KINDS[i % len(KINDS)]
        row = conn.execute(
            "INSERT INTO decision_journal (ts, trading_date, case_id, actor, decision, "
            "evidence_snapshot_ref, rationale, payload, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                NOW - timedelta(days=60 - i // 5),
                day,
                case,
                "SYSTEM" if case is None else "T0",
                decision,
                REF if i % 7 == 0 else None,
                rationale,
                Jsonb(payload),
                NOW,
            ),
        ).fetchone()
        assert row is not None
        seeded.append(
            {"id": int(row[0]), "case": case, "book": payload.get("book"), "date": day,
             "decision": decision}
        )  # fmt: skip
    return seeded


def _walk(client: TestClient, **params: str) -> list[int]:
    response = client.get("/console/journal", params=params or None)
    assert response.status_code == 200, response.text
    html = response.text
    seen = [int(m[0]) for m in _ROW.findall(html)]
    while (link := _NEXT.search(html)) is not None and len(seen) < 1000:
        html = client.get(unescape(link[1])).text
        seen += [int(m[0]) for m in _ROW.findall(html)]
    return seen


def test_every_filter_and_the_keyset_walk_match_postgres(
    conn: Connection, client: TestClient
) -> None:
    seeded = _seed(conn)

    def ids(keep: Any) -> list[int]:
        return sorted((e["id"] for e in seeded if keep(e)), reverse=True)

    assert _walk(client) == ids(lambda e: True)
    assert _walk(client, case=CASE) == ids(lambda e: e["case"] == CASE)
    assert _walk(client, manager=BOOKS[0]) == ids(lambda e: e["book"] == BOOKS[0])
    assert _walk(client, kind="HEARTBEAT") == ids(lambda e: e["decision"] == "HEARTBEAT")
    assert _walk(client, date_from="2026-09-10", date_to="2026-09-15") == ids(
        lambda e: date(2026, 9, 10) <= e["date"] <= date(2026, 9, 15)
    )
    assert _walk(client, manager=BOOKS[1], kind="SELL", date_from="2026-09-05") == ids(
        lambda e: e["book"] == BOOKS[1] and e["decision"] == "SELL"
        and e["date"] >= date(2026, 9, 5)
    )  # fmt: skip
    first = client.get("/console/journal").text
    noops = {kind for _, kind, flag in _ROW.findall(first) if flag == "1"}
    assert noops == {"HEARTBEAT", "HOLD", "SKIPPED_DATA_RED"}


def test_a_planted_credential_in_a_real_row_is_masked(conn: Connection, client: TestClient) -> None:
    _seed(conn)
    html = client.get("/console/journal", params={"manager": BOOKS[0]}).text  # entry 41
    assert PLANT not in html
    assert "postgresql://***@db/trading" in html
    assert "&quot;api_key&quot;: &quot;***&quot;" in html
    assert "password=***" in html


def test_cases_and_evidence_citations_come_from_the_journal(
    conn: Connection, client: TestClient
) -> None:
    seeded = _seed(conn)
    html = client.get("/console/journal/cases").text
    stages = dict(re.findall(r'<tr data-case="([^"]+)" data-stage="([^"]+)"', html))
    assert stages == {CASE: "ACTIVE", BOOKS[0]: "FUNDED", BOOKS[1]: "FUNDED"}
    assert f'href="/cases/{CASE}"' in html
    count = sum(1 for e in seeded if e["case"] == CASE)
    assert f"{count}</a></td><td>{max(e['date'] for e in seeded if e['case'] == CASE)}" in html
    # A cited bundle that is not in the store is a 404 to open, not an empty page.
    assert client.get("/console/journal/evidence", params={"ref": REF}).status_code == 404


def test_evidence_citations_are_counted_from_real_rows(
    conn: Connection, client: TestClient, tmp_path: Path
) -> None:
    seeded = _seed(conn)
    digest = REF.removeprefix("sha256:")
    path = tmp_path / "evidence" / digest[:2] / digest[2:4] / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(BUNDLE)
    cited = sorted((e["id"] for i, e in enumerate(seeded) if i % 7 == 0), reverse=True)
    listing = client.get("/console/journal/evidence").text
    row = listing[listing.index(f'<tr data-bundle="{REF}"') :]
    assert f'<td class="num">{len(cited)}</td>' in row[: row.index("</tr>")]
    opened = client.get("/console/journal/evidence", params={"ref": REF}).text
    assert "verified</span>" in opened
    assert re.findall(r">#(\d+)</a>", opened) == [str(i) for i in cited]
