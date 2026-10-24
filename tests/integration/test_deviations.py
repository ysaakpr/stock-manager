"""Accepted deviations against a real migrated database: the reads, the surfaces, the alerter.

The unit suites prove the matchers over fakes; this proves the SQL they stood in for — the
failure streak's rows (the same predicate as `/status/sources`' streak), the rows a job run wrote
inside its own window, the alert ledger's re-grade — and that the BSE block reaches the JSON, the
console and `alert_condition` as a warning while every other failure stays an error.

Scratch database per module, dropped afterwards; never the developer's `trading` database. Skips
loudly when the docker Postgres is unreachable.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest
from fastapi.testclient import TestClient

from dataplatform.alert_triggers import run_failure_alerts
from dataplatform.alerts import BaseAlerter, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler.registry import default_registry
from dataplatform.status.api import app, clock_source, db_connection, settings_source
from dataplatform.status.deviations import DeviationJudge, default_deviations
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB: Final = f"trading_deviations_{os.getpid()}"
NOW: Final = datetime(2026, 10, 23, 12, 0, tzinfo=IST)
CA_RUN: Final = datetime(2026, 10, 17, 10, 0, tzinfo=IST)
ANN_RUN: Final = datetime(2026, 10, 23, 0, 30, tzinfo=IST)

FORBIDDEN = (
    "ForbiddenError: {source}: https://api.bseindia.com/BseIndiaAPI/api/X/w?a=1 returned 403 "
    "(1 of 3 inside 0:30:00); not retried — a refusal is answered by waiting or stopping"
)
SPIKE = (
    "ForbiddenSpikeError: ingestion from api.bseindia.com is hard-stopped after a 403 spike; "
    "this process will not request it again."
)
SOFT_BLOCK = "bse_scrip_master: expected a non-empty JSON array, got a list — broken response"


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


def _sync(
    conn: Connection,
    source: str,
    day: date,
    state: str,
    error: str | None = None,
    *,
    unit: str = "",
    at: datetime = NOW - timedelta(days=1),
) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, unit, logical_date, state, attempts, retryable, "
        "last_error, updated_at) VALUES (%s, %s, %s, %s, 1, true, %s, %s)",
        (source, unit, day, state, error, at),
    )


def _run(conn: Connection, job: str, state: str, started: datetime, error: str | None) -> None:
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, state, instance, started_at, finished_at, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), job, state, "box:7", started, started + timedelta(minutes=30), error),
    )


def _seed(conn: Connection) -> None:
    """The live shape of the BSE block, plus failures nobody accepted."""
    for source in ("bse_announcements", "bse_scrip_master", "nse_announcements"):
        _sync(conn, source, date(2026, 9, 1), "PUBLISHED")
    for day in (date(2026, 10, 21), date(2026, 10, 22)):
        _sync(conn, "bse_scrip_master", day, "FAILED", FORBIDDEN.format(source="bse_scrip_master"))
        _sync(conn, "nse_announcements", day, "PUBLISHED", at=ANN_RUN + timedelta(minutes=2))
    _sync(conn, "bse_scrip_master", date(2026, 9, 15), "FAILED", SOFT_BLOCK)  # the soft block
    _sync(
        conn,
        "bse_announcements",
        date(2026, 10, 21),
        "FAILED",
        SPIKE,
        at=ANN_RUN + timedelta(minutes=3),
    )
    _sync(
        conn,
        "bse_announcements",
        date(2026, 10, 22),
        "FAILED",
        FORBIDDEN.format(source="bse_announcements"),
        at=ANN_RUN + timedelta(minutes=4),
    )
    # ca_refresh's run of 2026-10-17: NSE landed, two BSE scrips refused.
    _sync(
        conn,
        "nse_corp_actions",
        CA_RUN.date(),
        "PUBLISHED",
        unit="refresh",
        at=CA_RUN + timedelta(minutes=1),
    )
    for scrip in ("500083", "500117"):
        _sync(
            conn,
            "bse_corp_actions",
            CA_RUN.date(),
            "FAILED",
            FORBIDDEN.format(source="bse_corp_actions"),
            unit=scrip,
            at=CA_RUN + timedelta(minutes=5),
        )
    _sync(conn, "bse_corp_actions", date(2016, 9, 1), "PUBLISHED", unit="500325")
    # Failures nobody accepted.
    _sync(conn, "nse_xbrl_filing", date(2026, 10, 20), "FAILED", "FetchHTTPError: x returned 404")
    _run(
        conn,
        "ca_refresh",
        "FAILED",
        CA_RUN,
        "CaRefreshError: CA refresh 2026-09-12..2026-10-17: 2 FAILED: BSE scrip 500083: ...",
    )
    _run(
        conn,
        "announcements_capture",
        "FAILED",
        ANN_RUN,
        "DailyCaptureError: announcements_capture: "
        "nse_announcements@2026-10-21=ALREADY_PUBLISHED, "
        "bse_announcements@2026-10-21=FAILED, nse_announcements@2026-10-22=CAPTURED, "
        "bse_announcements@2026-10-22=FAILED; 6 request(s) — 2 owed date(s) did not land",
    )
    _run(
        conn,
        "identity_refresh",
        "FAILED",
        NOW - timedelta(days=6),
        "RuntimeError: identity refresh 2026-10-17: 2605 securities changed, 10 conflict(s)",
    )


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    """Rolled back at the end of the test, so each test sees only its own rows."""
    with connection(scratch_settings) as live:
        try:
            _seed(live)
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


def test_the_streak_rows_are_the_streak_status_sources_counts(conn: Connection) -> None:
    store = SyncStateStore(conn, clock=FrozenClock(NOW))
    for status in store.source_statuses():
        rows = store.failure_streak_rows(status.source)
        assert len(rows) == status.failure_streak, status.source


def test_status_sources_marks_the_bse_block_accepted_and_nothing_else(client: TestClient) -> None:
    body = client.get("/status/sources").json()
    lines = {line["source"]: line for line in body["sources"]}
    for source in ("bse_announcements", "bse_scrip_master", "bse_corp_actions"):
        assert lines[source]["healthy"] is False, source
        assert lines[source]["deviation"]["status"] == "ACCEPTED", source
    assert lines["nse_xbrl_filing"]["deviation"] is None
    assert lines["nse_xbrl_filing"]["healthy"] is False
    assert body["accepted"] == 3
    assert body["errors"] == sum(1 for line in lines.values() if not line["healthy"]) - 3


def test_status_jobs_accepts_the_bse_only_runs_and_keeps_healthy_strict(
    client: TestClient,
) -> None:
    body = client.get("/status/jobs").json()
    jobs = {job["name"]: job for job in body["jobs"]}
    assert jobs["ca_refresh"]["deviation"]["id"] == "bse-403-job-ca-refresh"
    assert jobs["announcements_capture"]["deviation"]["status"] == "ACCEPTED"
    assert jobs["identity_refresh"]["deviation"] is None
    assert jobs["ca_refresh"]["state"] == "FAILING" and jobs["ca_refresh"]["healthy"] is False
    assert body["healthy"] is False
    assert body["accepted"] == 2


def test_an_nse_leg_failing_in_the_same_run_turns_the_job_red(
    conn: Connection, client: TestClient
) -> None:
    _sync(
        conn,
        "nse_announcements",
        date(2026, 10, 20),
        "FAILED",
        "TransportError: www.nseindia.com timed out",
        at=ANN_RUN + timedelta(minutes=6),
    )
    jobs = {job["name"]: job for job in client.get("/status/jobs").json()["jobs"]}
    assert jobs["announcements_capture"]["deviation"] is None


def test_the_console_shows_the_bse_block_amber_and_the_rest_red(client: TestClient) -> None:
    ops = client.get("/console/ops").text
    for job in ("ca_refresh", "announcements_capture"):
        row = re.search(rf'<tr data-job="{job}"[^>]*>', ops)
        assert row is not None and 'data-deviation="ACCEPTED"' in row.group(0), job
    identity = re.search(r'<tr data-job="identity_refresh"[^>]*>', ops)
    assert identity is not None and "data-deviation" not in identity.group(0)
    data = client.get("/console/data").text
    assert re.search(r'<tr data-source="bse_scrip_master" data-health="ACCEPTED"', data)
    assert re.search(r'<tr data-source="nse_xbrl_filing" data-health="FAILING"', data)


class _Recorder(BaseAlerter):
    channel = "test"

    def __init__(self) -> None:
        super().__init__(clock=FrozenClock(NOW), dedup_window=timedelta(0))
        self.sent: list[tuple[Severity, str]] = []

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        self.sent.append((severity, dedup_key))


def _ledger(conn: Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT dedup_key, severity FROM alert_condition WHERE resolved_at IS NULL"
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def test_failure_alerts_opens_the_bse_block_at_warning_and_the_rest_critical(
    conn: Connection, scratch_settings: Settings
) -> None:
    alerter = _Recorder()
    settings = Settings(database_url=scratch_settings.database_url)
    report = run_failure_alerts(
        conn,
        settings=settings,
        clock=FrozenClock(NOW),
        alerter=alerter,
        job_names=default_registry().names(),
    )
    assert report.errors == []
    ledger = _ledger(conn)
    for key in (
        "ingest:bse_scrip_master:failed_streak",
        "job:ca_refresh:failed",
        "job:announcements_capture:failed",
    ):
        assert ledger[key] == "warning", key
    assert ledger["job:identity_refresh:failed"] == "critical"
    critical = {key for severity, key in alerter.sent if severity is Severity.CRITICAL}
    assert critical and not any("bse" in key or "ca_refresh" in key for key in critical)
    assert "job:announcements_capture:failed" not in critical


def test_an_open_critical_bse_alert_is_lowered_and_a_new_failure_raises_it(
    conn: Connection, scratch_settings: Settings
) -> None:
    conn.execute(
        "INSERT INTO alert_condition (dedup_key, trigger, severity, title, opened_at, "
        "last_seen_at) VALUES ('job:ca_refresh:failed', 'job_failed', 'critical', "
        "'scheduled job ca_refresh raised', %s, %s)",
        (NOW - timedelta(days=13), NOW - timedelta(days=1)),
    )
    settings = Settings(database_url=scratch_settings.database_url)

    def tick(alerter: _Recorder) -> None:
        run_failure_alerts(
            conn,
            settings=settings,
            clock=FrozenClock(NOW),
            alerter=alerter,
            job_names=default_registry().names(),
        )

    quiet = _Recorder()
    tick(quiet)
    assert _ledger(conn)["job:ca_refresh:failed"] == "warning"
    assert "job:ca_refresh:failed" not in {key for _, key in quiet.sent}, "lowering never pages"

    # A non-BSE leg of the same run fails: the known cause no longer covers the job.
    _sync(
        conn,
        "nse_corp_actions",
        CA_RUN.date() - timedelta(days=1),
        "FAILED",
        "TransportError: timed out",
        unit="refresh",
        at=CA_RUN + timedelta(minutes=2),
    )
    loud = _Recorder()
    tick(loud)
    assert _ledger(conn)["job:ca_refresh:failed"] == "critical"
    assert (Severity.CRITICAL, "job:ca_refresh:failed") in loud.sent


def test_the_judge_reads_nothing_for_an_item_no_entry_targets(conn: Connection) -> None:
    judge = DeviationJudge(default_deviations(), conn, SyncStateStore(conn), NOW.date())
    assert judge.job("identity_refresh") is None
    assert judge.alert("job:identity_refresh:failed") is None


# ── D27 / D28 (owner, 2026-10-24) ────────────────────────────────────────────────────────────

D27_KNOWN: Final = "fa424e5f2c8c4c801aa4d309c8137b12"


def _ca_flag(conn: Connection, fingerprint: str, raised: datetime) -> None:
    conn.execute(
        "INSERT INTO quality_flag (logical_date, check_name, severity, isin, source, detail, "
        "raised_at) VALUES (%s, 'ca_reconciliation', 'ERROR', 'INE109C01017', "
        "'bse_corp_actions', %s::jsonb, %s)",
        (
            date(2016, 9, 12),
            f'{{"reason": "RATIO_MISMATCH", "fingerprint": "{fingerprint}"}}',
            raised,
        ),
    )


def test_d27_marks_the_known_flag_and_keeps_a_new_one_red(
    client: TestClient, conn: Connection
) -> None:
    _ca_flag(conn, D27_KNOWN, datetime(2026, 9, 7, 14, 24, tzinfo=IST))
    _ca_flag(conn, "0123456789abcdef0123456789abcdef", datetime(2026, 10, 20, 9, 0, tzinfo=IST))
    body = client.get("/status/quality").json()
    (check,) = [c for c in body["by_check"] if c["check_name"] == "ca_reconciliation"]
    assert check["count"] == 2 and check["accepted"] == 1 and check["deviation"] is None
    verdicts = {
        flag["detail"]["fingerprint"]: flag["deviation"]
        for flag in body["flags"]
        if flag["check_name"] == "ca_reconciliation"
    }
    assert verdicts[D27_KNOWN]["id"] == "d27-ca-reconciliation-backlog"
    assert verdicts["0123456789abcdef0123456789abcdef"] is None


def test_d28_marks_a_recent_retrying_xbrl_404_on_the_gaps_page(
    client: TestClient, conn: Connection
) -> None:
    error = (
        "FetchHTTPError: nse_xbrl_filing: https://nsearchives.nseindia.com/corporate/xbrl/"
        "INTEGRATED_FILING_INDAS_1_2_WEB.xml returned 404, which no retry would change"
    )
    for unit, first in (("IF1", NOW - timedelta(days=2)), ("IF2", NOW - timedelta(days=9))):
        conn.execute(
            "INSERT INTO sync_state (source, unit, logical_date, state, attempts, retryable, "
            "last_error, first_attempt_at, updated_at) "
            "VALUES ('nse_xbrl_filing', %s, %s, 'FAILED', 3, true, %s, %s, %s)",
            (unit, date(2026, 10, 21), error, first, NOW - timedelta(hours=1)),
        )
    body = client.get(
        "/status/gaps",
        params={"from": "2026-10-21", "to": "2026-10-21", "source": "nse_xbrl_filing"},
    ).json()
    by_error_age = [e["deviation"] for e in body["unexplained"]]
    assert len(by_error_age) == 2
    assert sum(1 for d in by_error_age if d is not None and d["status"] == "ACCEPTED") == 1
    assert body["accepted_total"] == 1 and body["fully_explained"] is False
