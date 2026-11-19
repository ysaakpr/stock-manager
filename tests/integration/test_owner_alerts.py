"""OPS-ALERTS against real tables: the owner triggers are writable, and "once" survives a restart.

`tests/unit/test_owner_alerts.py` proves the builders and the diff in memory; this proves that the
0027 CHECK admits every owner trigger, that `send_once` holds its key in `alert_condition` so a
fresh alerter — a restarted scheduler — sends nothing twice, that a failed send records nothing,
and that the reads the summary and the paper-activity message run are valid SQL on the schema.

Runs against a scratch database created for the session and dropped afterwards; each test's
connection is rolled back. Needs the docker postgres (`make up`); skips loudly without it.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Iterator, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from dataplatform.alert_triggers import ConditionLedger, TickReport, Trigger, reconcile, send_once
from dataplatform.alerts import BaseAlerter, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.owner_alerts import (
    SwitchState,
    kill_switch_conditions,
    kill_switch_resolution,
    read_jobs_day,
    read_open_alerts,
    read_paper_activity,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_ops_alerts_{os.getpid()}"
NOW = datetime(2026, 11, 19, 8, 0, tzinfo=IST)
D13 = "momentum_v2_paper_2026_09_06"


class RecordingAlerter(BaseAlerter):
    channel = "test"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(clock=FrozenClock(NOW), dedup_window=timedelta(0))
        self.sent: list[tuple[Severity, str, str, str]] = []
        self.fail = fail

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        if self.fail:
            raise ConnectionError("relay down")
        self.sent.append((severity, title, body, dedup_key))


class NoPrices:
    def closes(self, session: date, isins: Collection[str]) -> Mapping[str, Decimal]:
        return {}

    def previous_session(self, session: date) -> date | None:
        return None

    def next_session(self, session: date) -> date | None:
        return session + timedelta(days=1)


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
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
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


def _send(conn: Connection, alerter: RecordingAlerter, key: str) -> TickReport:
    report = TickReport()
    send_once(
        ledger=ConditionLedger(conn),
        alerter=alerter,
        trigger=Trigger.OWNER_EVENT,
        key=key,
        severity=Severity.INFO,
        title="Morning summary",
        body="All normal.",
        at=NOW,
        report=report,
    )
    return report


def test_a_one_shot_message_is_sent_once_across_restarts(conn: Connection) -> None:
    first, second = RecordingAlerter(), RecordingAlerter()
    assert _send(conn, first, "owner:summary:2026-11-19").ok
    assert _send(conn, second, "owner:summary:2026-11-19").ok
    assert len(first.sent) == 1 and second.sent == []
    row = conn.execute(
        "SELECT trigger, resolved_at IS NOT NULL FROM alert_condition WHERE dedup_key = %s",
        ("owner:summary:2026-11-19",),
    ).fetchone()
    assert row == ("owner_event", True)


def test_a_failed_send_records_nothing(conn: Connection) -> None:
    report = _send(conn, RecordingAlerter(fail=True), "owner:summary:2026-11-20")
    assert not report.ok
    assert not ConditionLedger(conn).has_key("owner:summary:2026-11-20")


def test_every_owner_state_trigger_is_writable_and_clears(conn: Connection) -> None:
    ledger, alerter = ConditionLedger(conn), RecordingAlerter()
    tripped = kill_switch_conditions([SwitchState(D13, tripped=True, source="MANUAL")])
    for conditions in (tripped, []):
        report = TickReport()
        reconcile(
            Trigger.OWNER_KILL_SWITCH,
            conditions,
            ledger=ledger,
            alerter=alerter,
            at=NOW,
            report=report,
            resolution=kill_switch_resolution(lambda account: None),
        )
        assert report.ok, report.errors
    assert [s[1] for s in alerter.sent] == [
        "Trading halted: Current momentum book (D13)",
        "Trading resumed: Current momentum book (D13)",
    ]
    for trigger in (Trigger.OWNER_LOSS, Trigger.OWNER_STALE_DATA):
        ledger.record_event(trigger, f"owner:probe:{trigger.value}", Severity.WARNING, "x", NOW)


def test_the_summary_and_activity_reads_run_on_the_schema(conn: Connection) -> None:
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, state, instance, started_at, finished_at, error) "
        "VALUES (gen_random_uuid(), 'eod_pipeline', 'FAILED', 'test', %s, %s, 'boom'), "
        "(gen_random_uuid(), 'tri_evening', 'SUCCEEDED', 'test', %s, %s, NULL)",
        (
            datetime(2026, 11, 18, 18, 30, tzinfo=IST),
            datetime(2026, 11, 18, 18, 40, tzinfo=IST),
            datetime(2026, 11, 18, 19, 50, tzinfo=IST),
            datetime(2026, 11, 18, 19, 51, tzinfo=IST),
        ),
    )
    jobs = read_jobs_day(conn, date(2026, 11, 18))
    assert jobs.ok == 1 and jobs.failed == ("eod_pipeline",)
    assert read_open_alerts(conn) == []
    conn.execute(
        "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
        "journal_digest, recorded_at) VALUES (%s, %s, 'SKIPPED_DATA_RED', 'status not green', "
        "false, %s, %s)",
        (D13, date(2026, 11, 18), "0" * 64, NOW),
    )
    ((activity, recorded_at),) = read_paper_activity(conn, NoPrices(), [D13])
    assert activity.outcome == "SKIPPED_DATA_RED" and activity.was_in_cash
    assert recorded_at == NOW
