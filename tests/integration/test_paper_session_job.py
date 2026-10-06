"""M13.1 — the ``paper_session`` scheduler job against the docker Postgres.

The unit suite proves the session's logic over in-memory stores; this proves the job as the
scheduler runs it — ``run_paper_session_job`` with a real ``JobContext``, the real status interlock
reading ``sync_state``, the real append-only ``decision_journal`` and the ``paper_session`` ledger
(migration 0012), one committed transaction per run — over the frozen fixture market
(``tests/paper_session_support.py``) so it never reads the lake:

* with nothing published the status gate is red: one ``SKIPPED_DATA_RED`` lands in the journal
  and the ledger, and a rerun adds nothing;
* once ``nse_bhavcopy`` is published for the date the same rerun decides it, and a further rerun
  is a no-op — the journal row count does not move;
* the next session rebuilds the book from the ledger's JSON (orders, digest) and decides again;
* the table refuses a red row that claims orders.

Needs the docker postgres (`make up`); skips loudly if it is unreachable. No network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from backtest.paper_session import (
    PAPER_BOOK_ID,
    PAPER_MODE,
    PaperSessionResult,
    PostgresPaperSessionStore,
    RunVerdict,
    SessionOutcome,
    run_paper_session_job,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import JobContext
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from tests.paper_session_support import OCT_FIRST, OCT_SECOND, FixtureWorld

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_migrations).
SCRATCH_DB = f"trading_m13_1_paper_session_{os.getpid()}"
MIGRATED_AT = datetime(2026, 9, 30, 9, 0, tzinfo=IST)


def _settings_for(dbname: str, data_root: Path | None = None) -> Settings:
    base = Settings()
    settings = Settings(database_url=with_dbname(base.database_url, dbname))
    if data_root is not None:
        settings = settings.model_copy(update={"data_root": data_root})
    return settings


@pytest.fixture(scope="module")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """An empty scratch database migrated through 0012, dropped at the end of the module."""
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
    settings = _settings_for(SCRATCH_DB, tmp_path_factory.mktemp("paper_lake"))
    migrate(settings, clock=FrozenClock(MIGRATED_AT))
    yield settings
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


def _run(settings: Settings, day: date, world: FixtureWorld) -> PaperSessionResult:
    """One scheduler run of the job at 20:30 IST on ``day``."""
    context = JobContext(
        job_name="paper_session",
        run_id=uuid4(),
        clock=FrozenClock(datetime(day.year, day.month, day.day, 20, 30, tzinfo=IST)),
        settings=settings,
    )
    return run_paper_session_job(context, world=world)


def _publish(settings: Settings, day: date) -> None:
    with connection(settings) as conn:
        store = SyncStateStore(conn, clock=FrozenClock(datetime(2026, 10, 1, 18, 45, tzinfo=IST)))
        store.begin("nse_bhavcopy", day)
        store.mark_fetched("nse_bhavcopy", day, checksum="fixture", l0_path="L0/fixture.zip")
        store.mark_validated("nse_bhavcopy", day)
        store.mark_normalized("nse_bhavcopy", day)
        store.mark_published("nse_bhavcopy", day)
        conn.commit()


def _journal_rows(settings: Settings, day: date) -> list[tuple[str, str]]:
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT decision, payload->>'mode' FROM decision_journal "
            "WHERE trading_date = %s AND payload->>'paper_book' = %s ORDER BY id",
            (day, PAPER_BOOK_ID),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def test_the_job_is_red_until_published_then_decides_once(scratch: Settings) -> None:
    world = FixtureWorld()

    red = _run(scratch, OCT_FIRST, world)
    assert red.verdict is RunVerdict.SKIPPED_DATA_RED
    assert _journal_rows(scratch, OCT_FIRST) == [("SKIPPED_DATA_RED", PAPER_MODE)]
    assert world.reads == [], "a red day reads no decision data (invariant #10)"

    again = _run(scratch, OCT_FIRST, world)
    assert again.verdict is RunVerdict.STILL_RED
    assert len(_journal_rows(scratch, OCT_FIRST)) == 1, "a still-red rerun is not re-journaled"

    _publish(scratch, OCT_FIRST)
    decided = _run(scratch, OCT_FIRST, world)
    assert decided.verdict is RunVerdict.DECIDED
    rows = _journal_rows(scratch, OCT_FIRST)
    assert rows[0] == ("SKIPPED_DATA_RED", PAPER_MODE), "the journal is append-only"
    assert {decision for decision, _ in rows[1:]} <= {"BUY", "RAIL_BLOCK"}
    assert any(decision == "BUY" for decision, _ in rows[1:])
    assert all(mode == PAPER_MODE for _, mode in rows)

    noop = _run(scratch, OCT_FIRST, world)
    assert noop.verdict is RunVerdict.ALREADY_DECIDED
    assert _journal_rows(scratch, OCT_FIRST) == rows, "a rerun on a decided date writes nothing"

    with connection(scratch) as conn:
        stored = PostgresPaperSessionStore(conn).get(PAPER_BOOK_ID, OCT_FIRST)
    assert stored is not None and stored == decided.record
    assert stored.outcome is SessionOutcome.COMPLETED and stored.orders


def test_the_next_session_rebuilds_the_book_from_the_ledger(scratch: Settings) -> None:
    world = FixtureWorld()
    _publish(scratch, OCT_SECOND)
    second = _run(scratch, OCT_SECOND, world)

    assert second.verdict is RunVerdict.DECIDED
    assert second.book is not None and second.book.positions, "Thursday's orders filled Monday"
    assert [decision for decision, _ in _journal_rows(scratch, OCT_SECOND)] == ["HEARTBEAT"]


def test_the_table_refuses_a_red_row_that_claims_orders(scratch: Settings) -> None:
    with connection(scratch) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "orders, journal_digest, recorded_at) VALUES "
            "('paper_check_book', '2026-10-07', 'SKIPPED_DATA_RED', 'red', false, "
            "'[{\"isin\": \"INE001A01173\"}]'::jsonb, 'x', now())"
        )
