"""M18.3 against a real migrated database: the Desk's SQL, and its parity with /status/paper.

The unit suite (`tests/unit/test_console_desk.py`) proves the page over a stub connection; this
module proves the queries it stubbed are the queries Postgres answers — `paper_session`'s newest
accounting book and placed orders, the `order_` ledger newest first, the journal's AUTH_REQUIRED,
and `token_usage` grouped by IST day (a call at 00:10 IST is that IST day, not UTC yesterday),
windowed to 30 days, with `numeric` sums arriving as `Decimal`.

Scratch database per module, dropped afterwards; never the developer's `trading` database. Skips
loudly when the docker Postgres is unreachable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Json

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.status.models_desk import BrokerSessionState
from dataplatform.status.queries_desk import (
    read_broker_session,
    read_model_cost,
    read_orders,
    read_paper_positions,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_m18_3_desk_{os.getpid()}"
NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
PLANT: Final = "PLANTEDdi"
ISIN_A, ISIN_B = "INE002A01018", "INE009A01021"
BOOK: Final = "momentum_paper"
MANAGER: Final = "FM-SWING-BRK-10L"


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


def _seed_reference(conn: Connection) -> None:
    for isin in (ISIN_A, ISIN_B):
        conn.execute(
            "INSERT INTO security_master (isin, name, primary_exchange, status, first_seen_date, "
            "created_at, updated_at) VALUES (%s, %s, 'NSE', 'ACTIVE', '2020-01-01', %s, %s)",
            (isin, f"name {isin}", NOW, NOW),
        )
    for case_id in ("case_core_1", MANAGER):
        conn.execute(
            "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
            "VALUES (%s, %s, 'ACTIVE', %s, %s)",
            (case_id, case_id, NOW, NOW),
        )


def _paper(
    conn: Connection, on: date, *, book: Mapping[str, object], orders: list[dict[str, str | None]]
) -> None:
    conn.execute(
        "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, orders, "
        "journal_digest, book_state, book_digest, recon, expected_book, recorded_at) "
        "VALUES (%s, %s, 'COMPLETED', 'decided', %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            BOOK, on, bool(orders), Json(orders), "d" * 64, Json({"cash": "1"}), "b" * 64,
            Json({"status": "CLEAN", "key": f"RECON:{on}", "terms": "0" * 16}), Json(dict(book)),
            NOW,
        ),
    )  # fmt: skip


def _order(conn: Connection, uid: str, isin: str, state: str, staged: datetime) -> None:
    conn.execute(
        "INSERT INTO order_ (order_uid, case_id, isin, exchange, sleeve, side, order_type, "
        "quantity, state, broker, staged_at, staged_for_date, updated_at, rejection_reason, "
        "executed_at, filled_quantity) "
        "VALUES (%s, 'case_core_1', %s, 'NSE', 'CORE', 'BUY', 'MARKET', 5, %s, 'SIM', %s, %s, "
        "%s, %s, %s, %s)",
        (uid, isin, state, staged, staged.date() + timedelta(days=1), staged,
         None if state != "RAIL_BLOCKED" else "=".join(("password", PLANT)),
         staged if state == "EXECUTED" else None, 5 if state == "EXECUTED" else 0),
    )  # fmt: skip


def _usage(conn: Connection, ts: datetime, inr: str, usd: str | None) -> None:
    conn.execute(
        "INSERT INTO token_usage (ts, provider, model, purpose, case_id, tokens_in, tokens_out, "
        "cost_inr, cost_usd, recorded_at) VALUES (%s, 'anthropic', 'claude-opus-5-5', "
        "'fm_decision', %s, 100, 20, %s, %s, %s)",
        (ts, MANAGER, Decimal(inr), None if usd is None else Decimal(usd), ts),
    )


def _journal(conn: Connection, on: date, decision: str, rationale: str | None = None) -> None:
    conn.execute(
        "INSERT INTO decision_journal (ts, trading_date, actor, decision, rationale, payload, "
        "recorded_at) VALUES (%s, %s, 'SYSTEM', %s, %s, %s, %s)",
        (NOW, on, decision, rationale,
         Json({"reauth": "Re-authenticate.", "deferred_count": "2",
               "streak_started": on.isoformat()} if decision == "AUTH_REQUIRED" else {}),
         NOW),
    )  # fmt: skip


def test_the_desk_reads_answer_over_real_rows(conn: Connection) -> None:
    _seed_reference(conn)
    old = {"cash": "10", "positions": [{"isin": ISIN_A, "quantity": "1", "cost_basis": "5"}]}
    new = {
        "cash": "125000.500000",
        "positions": [{"isin": ISIN_B, "quantity": "40", "cost_basis": "60123.456789"}],
    }
    _paper(conn, date(2026, 10, 9), book=old, orders=[])
    placed = [{"isin": ISIN_A, "side": "BUY", "quantity": "7", "exchange": "NSE",
               "order_type": "MARKET", "limit_price": None, "tag": "MOMENTUM_REBAL"}]  # fmt: skip
    _paper(conn, date(2026, 10, 12), book=new, orders=placed)

    (positions,) = read_paper_positions(conn)
    assert positions.trading_date == date(2026, 10, 12)
    assert positions.cash == Decimal("125000.500000")
    assert [(p.isin, p.quantity, p.cost_basis) for p in positions.positions] == [
        (ISIN_B, 40, Decimal("60123.456789"))
    ]

    base = NOW.replace(hour=19)
    _order(conn, "ord-1", ISIN_A, "EXECUTED", base - timedelta(days=1))
    _order(conn, "ord-2", ISIN_B, "STAGED", base - timedelta(minutes=5))
    _order(conn, "ord-3", ISIN_A, "RAIL_BLOCKED", base)
    orders = read_orders(conn, as_of=NOW, limit=2)
    assert [o.reference for o in orders.ledger] == ["ord-3", "ord-2"]
    assert orders.ledger[0].tags == ["SIM", "CORE"]
    assert orders.ledger[0].rejection_reason is not None
    assert PLANT not in orders.ledger[0].rejection_reason
    (paper_order,) = orders.paper
    assert (paper_order.state, paper_order.tags) == ("PLACED", ["SIM", "MOMENTUM_REBAL"])

    # 00:10 IST on the 12th is 18:40 UTC on the 11th: it is the 12th's spend.
    _usage(conn, datetime(2026, 10, 11, 18, 40, tzinfo=UTC), "0.1", "0.001")
    _usage(conn, NOW.replace(hour=9), "0.2", None)
    _usage(conn, NOW.replace(hour=1) - timedelta(days=1), "1.000001", "0.000002")
    _usage(conn, NOW - timedelta(days=31), "999", "9")  # outside the 30-day window
    cost = read_model_cost(conn, as_of=NOW)
    by_day = {line.day: line for line in cost.lines}
    assert set(by_day) == {date(2026, 10, 12), date(2026, 10, 11)}
    today = by_day[date(2026, 10, 12)]
    assert (today.calls, today.usd_calls, today.tokens_in) == (2, 1, 200)
    assert type(today.cost_inr) is Decimal and today.cost_inr == Decimal("0.3")
    assert today.cost_usd == Decimal("0.001")
    assert cost.total_inr == Decimal("1.300001") and cost.total_usd == Decimal("0.001002")

    assert read_broker_session(conn).state is BrokerSessionState.NO_LAPSE_RECORDED
    _journal(conn, date(2026, 10, 9), "AUTH_REQUIRED", "session expired")
    _journal(conn, date(2026, 10, 12), "HEARTBEAT")
    assert read_broker_session(conn).state is BrokerSessionState.CLEAR
    _journal(conn, date(2026, 10, 12), "AUTH_REQUIRED", "=".join(("token", PLANT)))
    broker = read_broker_session(conn)
    assert broker.state is BrokerSessionState.AUTH_REQUIRED
    assert broker.deferred_count == 2 and broker.streak_started == date(2026, 10, 12)
    assert broker.reason is not None and PLANT not in broker.reason


def test_the_desk_page_agrees_with_status_paper_over_real_rows(
    conn: Connection, client: TestClient
) -> None:
    _seed_reference(conn)
    _paper(conn, date(2026, 10, 12), book={"cash": "1", "positions": []}, orders=[])
    _journal(conn, date(2026, 10, 12), "AUTH_REQUIRED", "session expired")

    payload = client.get("/status/paper").json()
    response = client.get("/console/desk")
    assert response.status_code == 200, response.text
    html = response.text
    rows = re.findall(r'<tr data-paper-book="([^"]+)" data-healthy="([^"]+)"', html)
    assert rows == [(b["book_id"], str(b["healthy"]).lower()) for b in payload["books"]]
    assert rows == [(BOOK, "true")]
    assert '<div class="banner bad" id="broker-session" data-state="AUTH_REQUIRED">' in html
    assert '<div class="banner ok" id="kill-switch" data-state="ARMED">' in html
    assert PLANT not in html
