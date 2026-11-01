"""The one-time XBRL re-queue against a real migrated database: the journal and the evidence row.

`fundamentals_requeue_log` (migration 0023) is what makes a re-queue one-time: an item journaled is
never owed again, and a second journal write for it is a no-op rather than an error. And a
`record_evidence` re-record through the real `SyncStateStore` keeps the row's first attempt — so the
row is still outside D28's window — and is what the D31 matcher accepts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import fundamentals_requeue as rq
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.status.deviations import DeviationJudge, GapItem, default_deviations
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_requeue_{os.getpid()}"
NOW: Final = datetime(2026, 11, 2, 2, 30, tzinfo=IST)
INDEX: Final = "nse_integrated_filing_index/2026-04-01/integrated-filing-results_20260401_p01.json"
URL: Final = "https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_INDAS_1_2_WEB.xml"


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


def test_a_journaled_item_is_never_owed_again(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "requeue.yaml"
    path.write_text(
        "version: 1\nrequeues:\n  - id: once\n    action: refetch\n    not_before: 2026-11-02\n"
        "    basis: test\n    filings:\n"
        f"      - {{unit: IF1, filing_date: 2026-04-24, index: {INDEX}}}\n"
        f"      - {{unit: IF1-FY, filing_date: 2026-04-24, index: {INDEX}}}\n"
    )
    clock = FrozenClock(NOW)
    with connection(settings) as conn:
        owed = rq.pending_items(conn, clock.today(), path=path)
        assert [i.unit for i in owed] == ["IF1", "IF1-FY"]
        journal = rq.journal_to(conn, clock)
        journal(owed[0], rq.RequeueOutcome.FAILED, "FAILED: 404")
        journal(owed[0], rq.RequeueOutcome.PUBLISHED, None)  # a second write: no-op, not an error
        assert [i.unit for i in rq.pending_items(conn, clock.today(), path=path)] == ["IF1-FY"]
        rows = conn.execute(
            "SELECT unit, logical_date, action, outcome, detail FROM fundamentals_requeue_log"
        ).fetchall()
    assert rows == [("IF1", date(2026, 4, 24), "refetch", "FAILED", "FAILED: 404")]


def test_an_unknown_outcome_is_refused_by_the_table(settings: Settings) -> None:
    with connection(settings) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO fundamentals_requeue_log VALUES "
            "('x', 'IF2', '2026-04-24', 'refetch', 'MAYBE', NULL, now())"
        )


def test_a_re_recorded_404_keeps_its_first_attempt_and_is_accepted_by_d31(
    settings: Settings,
) -> None:
    first = datetime(2026, 9, 6, 22, 23, tzinfo=IST)
    plain = f"FetchHTTPError: nse_xbrl_filing: {URL} returned 404, which no retry would change"
    day = date(2026, 4, 24)
    with connection(settings) as conn:
        then = SyncStateStore(conn, clock=FrozenClock(first), calendar=trading_calendar())
        then.begin("nse_xbrl_filing/IF152668", day)
        then.mark_failed("nse_xbrl_filing/IF152668", day, plain)
        now = SyncStateStore(conn, clock=FrozenClock(NOW), calendar=trading_calendar())
        now.begin("nse_xbrl_filing/IF152668", day)
        evidence = "; NSE's index lists this XBRL file as '0 Bytes'"
        row = now.mark_failed("nse_xbrl_filing/IF152668", day, plain + evidence)
        conn.commit()
        assert row.state is SyncState.FAILED and row.retryable
        assert row.first_attempt_at == first and row.attempts == 2
        judge = DeviationJudge(default_deviations(), conn, now, NOW.date())
        item = GapItem(
            source="nse_xbrl_filing",
            logical_date=day,
            reason="FAILED",
            last_error=row.last_error,
            retryable=row.retryable,
            first_attempt_at=row.first_attempt_at,
        )
        verdict = judge.gap(item)
        assert verdict is not None and verdict.id == "d31-xbrl-zero-byte-index" and verdict.accepted
        plain_item = GapItem(
            source="nse_xbrl_filing",
            logical_date=day,
            reason="FAILED",
            last_error=plain,
            retryable=True,
            first_attempt_at=first,
        )
        assert judge.gap(plain_item) is None  # old and without evidence: an error
