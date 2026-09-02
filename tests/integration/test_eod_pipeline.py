"""M1.10 acceptance: the daily EOD pipeline job, end to end and offline.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the network
is a `RecordedTransport` scripted with the real checked-in bhavcopy fixtures, and `sync_state`, the
L1 lake and the archive bundle all live in a scratch Postgres and a `tmp_path` lake created for the
test and dropped afterwards. No socket is opened to any exchange.

  1. one invocation takes a session from PENDING to PUBLISHED for all daily NSE sources
     (`test_one_invocation_publishes_the_session`).
  2. an induced failure (simulated 500) is retried on the next run and self-heals to PUBLISHED
     (`test_induced_500_is_retried_and_self_heals`, and the cross-day form in
     `test_self_heals_a_prior_days_failure`).
  3. a second run for the same session is a no-op
     (`test_second_run_for_the_same_session_is_a_no_op`).

The two fixture sessions used straddle the 2024-07-08 UDiFF cutover — 2024-07-05 is legacy and
2024-07-08 is UDiFF — so the runs exercise both bhavcopy parsers exactly as the daily job would.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.backfill import NSE_BHAVCOPY, SOURCE_SETS
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.eod import DAILY_NSE_SOURCES, EodPipeline
from dataplatform.ingest.fetcher import Fetcher, RecordedResponse, RecordedTransport
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.migrate import migrate
from dataplatform.store.paths import l1_partition_path

pytestmark = pytest.mark.integration

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "nse_bhavcopy"

#: A legacy session (Fri) and the UDiFF cutover session (Mon), each with its real fixture bytes.
LEGACY_SESSION: Final = date(2024, 7, 5)
UDIFF_SESSION: Final = date(2024, 7, 8)
FIXTURE_FILES: Final[dict[date, Path]] = {
    LEGACY_SESSION: FIXTURES / "legacy" / "cm05JUL2024bhav.csv.zip",
    UDIFF_SESSION: FIXTURES / "udiff" / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip",
}

SCRATCH_DB: Final = f"trading_m1_10_eod_{os.getpid()}"


# ── an alerter that records rather than logs, so alerts are asserted, not observed ─────────────


class RecordingAlerter:
    """An `Alerter` that keeps every send in memory. Deduplication is not exercised here."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, dedup_key))
        return AlertOutcome.SENT


# ── database + lake fixtures (mirror the M1.9 backfill suite) ─────────────────────────────────


def _settings_for(dbname: str, data_root: Path) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname), data_root=data_root)


@pytest.fixture(scope="session")
def scratch_db() -> Iterator[str]:
    """A scratch database created for the session and dropped afterwards (never the dev DB)."""
    admin = Settings(database_url=with_dbname(Settings().database_url, "postgres"))
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    migrate(_settings_for(SCRATCH_DB, Path("data")), clock=FrozenClock(UDIFF_SESSION))
    yield SCRATCH_DB

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def settings(scratch_db: str, tmp_path: Path) -> Settings:
    """Scratch DB plus a per-test lake root under tmp_path, keeping L0/L1/archives off the repo."""
    return _settings_for(scratch_db, tmp_path)


@pytest.fixture
def conn(settings: Settings) -> Iterator[Connection]:
    """A committed connection to the scratch DB, with the tables this job writes cleared.

    The pipeline commits after every session and after the bundle row, so tests cannot lean on
    rollback for isolation — they truncate the tables up front and let each run's commits stand.
    """
    with connection(settings) as live:
        live.execute("TRUNCATE sync_state")
        live.execute("TRUNCATE archive_bundle")
        live.commit()
        yield live


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _url_for(day: date, register: SourceRegister) -> str:
    """The exact URL the runner would fetch for one NSE cash session."""
    return SOURCE_SETS[NSE_BHAVCOPY].build_request(day, register).url


def _transport(
    register: SourceRegister,
    *,
    ok: tuple[date, ...] = (),
    fail_500: tuple[date, ...] = (),
) -> RecordedTransport:
    """Serve each `ok` date's real fixture as a 200 zip and each `fail_500` date as a 500."""
    script: dict[str, RecordedResponse] = {}
    for day in ok:
        script[_url_for(day, register)] = RecordedResponse(
            status_code=200,
            body=FIXTURE_FILES[day].read_bytes(),
            headers={"content-type": "application/zip"},
        )
    for day in fail_500:
        script[_url_for(day, register)] = RecordedResponse(status_code=500, body=b"upstream error")
    return RecordedTransport(script)


def _fetcher(
    transport: RecordedTransport, settings: Settings, clock: FrozenClock, register: SourceRegister
) -> Fetcher:
    """A fetcher wired to a recorded transport with a no-op sleep — offline and instant."""
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=RecordingAlerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _pipeline(
    transport: RecordedTransport,
    *,
    settings: Settings,
    conn: Connection,
    clock: FrozenClock,
    register: SourceRegister,
    alerter: RecordingAlerter,
    lookback: timedelta,
) -> EodPipeline:
    return EodPipeline(
        conn=conn,
        fetcher=_fetcher(transport, settings, clock, register),
        l0=L0Store(clock=clock, data_root=settings.data_root),
        alerter=alerter,
        clock=clock,
        calendar=trading_calendar(),
        register=register,
        archive_root=settings.data_root,
        data_root=settings.data_root,
        lookback=lookback,
    )


def _store(conn: Connection, clock: FrozenClock) -> SyncStateStore:
    return SyncStateStore(conn, clock=clock, calendar=trading_calendar())


def _urls_fetched(transport: RecordedTransport) -> list[str]:
    return [r.url for r in transport.requests]


# ── acceptance 1: one invocation, PENDING → PUBLISHED for every daily NSE source ──────────────


def test_one_invocation_publishes_the_session(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """One run lands the latest session in L1, PUBLISHED, with a clean gap report and archive."""
    clock = FrozenClock(UDIFF_SESSION)
    alerter = RecordingAlerter()
    pipeline = _pipeline(
        _transport(register, ok=(UDIFF_SESSION,)),
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter,
        lookback=timedelta(days=2),  # window = Sat, Sun, the target Monday — no un-run sessions
    )

    report = pipeline.run()

    assert report.logical_date == UDIFF_SESSION
    assert report.session_published
    assert report.sources == DAILY_NSE_SOURCES

    # Every daily NSE source is PUBLISHED for the session, and the data really landed in L1.
    store = _store(conn, clock)
    for source in DAILY_NSE_SOURCES:
        record = store.get(source, UDIFF_SESSION)
        assert record is not None and record.state is SyncState.PUBLISHED
    assert l1_partition_path("prices_raw", UDIFF_SESSION, data_root=settings.data_root).exists()
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0

    # The gap check ran and found nothing to explain over the clean window.
    assert report.gap_report is not None and report.gap_report.fully_explained
    assert alerter.sent == []  # nothing failed, so nothing was alerted

    # The archive bundle was published for the session and is on disk with its manifest.
    assert report.archive is not None
    assert report.archive.logical_date == UDIFF_SESSION
    assert (report.archive.bundle_dir / "manifest.json").exists()
    row = conn.execute(
        "SELECT count(*) FROM archive_bundle WHERE logical_date = %s", (UDIFF_SESSION,)
    ).fetchone()
    assert row is not None and row[0] == 1


# ── acceptance 2: an induced 500 is retried on the next run and self-heals to PUBLISHED ────────


def test_induced_500_is_retried_and_self_heals(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """First run 500s the session (FAILED, retryable, alerted); the next run heals it."""
    clock = FrozenClock(UDIFF_SESSION)

    # Run 1: the source 500s. The session is left FAILED(retryable) and an alert is emitted.
    failing = _transport(register, fail_500=(UDIFF_SESSION,))
    alerter_1 = RecordingAlerter()
    first = _pipeline(
        failing,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter_1,
        lookback=timedelta(days=2),
    ).run()

    assert not first.session_published
    assert first.archive is None  # nothing to archive for a session that never published
    store = _store(conn, clock)
    failed = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert failed is not None and failed.state is SyncState.FAILED and failed.retryable
    assert any(
        key == f"eod:{NSE_BHAVCOPY}:{UDIFF_SESSION.isoformat()}:FAILED"
        for _sev, _title, key in alerter_1.sent
    ), "a FAILED source must be alerted"

    # Run 2: the source is healthy again. The pipeline self-heals the retryable date to PUBLISHED.
    healthy = _transport(register, ok=(UDIFF_SESSION,))
    alerter_2 = RecordingAlerter()
    second = _pipeline(
        healthy,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=alerter_2,
        lookback=timedelta(days=2),
    ).run()

    assert second.session_published
    healed = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert healed is not None and healed.state is SyncState.PUBLISHED
    assert healed.attempts >= 2, "the retry is a second attempt on the same row, not a fresh row"
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0
    assert second.archive is not None  # the now-complete session is archived
    assert alerter_2.sent == []  # healed: nothing left to alert


def test_self_heals_a_prior_days_failure(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """A session that failed on an earlier day is picked up by a later run's self-heal step.

    This is the case the same-session retry cannot prove: Friday's fetch failed, and *Monday's* run
    — for a different target — re-attempts Friday within the lookback window before doing its own
    work, so both land PUBLISHED without a human.
    """
    # Friday's run: 2024-07-05 (legacy era) 500s and is left FAILED(retryable).
    friday_clock = FrozenClock(LEGACY_SESSION)
    first = _pipeline(
        _transport(register, fail_500=(LEGACY_SESSION,)),
        settings=settings,
        conn=conn,
        clock=friday_clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=3),
    ).run()
    assert first.logical_date == LEGACY_SESSION and not first.session_published

    # Monday's run: target is 2024-07-08 (UDiFF era); the lookback reaches back to Friday.
    monday_clock = FrozenClock(UDIFF_SESSION)
    second = _pipeline(
        _transport(register, ok=(LEGACY_SESSION, UDIFF_SESSION)),
        settings=settings,
        conn=conn,
        clock=monday_clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=3),
    ).run()

    assert second.logical_date == UDIFF_SESSION
    assert second.session_published
    assert LEGACY_SESSION in second.healed, "the prior day's failure was re-attempted this run"

    store = _store(conn, monday_clock)
    for day in (LEGACY_SESSION, UDIFF_SESSION):
        record = store.get(NSE_BHAVCOPY, day)
        assert record is not None and record.state is SyncState.PUBLISHED
    # Both eras really landed in L1, and the window now has nothing to explain.
    assert len(read_prices_raw(LEGACY_SESSION, data_root=settings.data_root)) > 0
    assert len(read_prices_raw(UDIFF_SESSION, data_root=settings.data_root)) > 0
    assert second.gap_report is not None and second.gap_report.fully_explained


# ── acceptance 3: a second run for the same session is a no-op ─────────────────────────────────


def test_second_run_for_the_same_session_is_a_no_op(
    settings: Settings, conn: Connection, register: SourceRegister
) -> None:
    """Re-running a published session fetches nothing, changes no state, and re-writes no bundle."""
    clock = FrozenClock(UDIFF_SESSION)

    first = _pipeline(
        _transport(register, ok=(UDIFF_SESSION,)),
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=2),
    ).run()
    assert first.session_published and first.archive is not None

    store = _store(conn, clock)
    before = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert before is not None

    # Second run, same session, a fresh transport that must be asked for nothing.
    second_transport = _transport(register, ok=(UDIFF_SESSION,))
    second = _pipeline(
        second_transport,
        settings=settings,
        conn=conn,
        clock=clock,
        register=register,
        alerter=RecordingAlerter(),
        lookback=timedelta(days=2),
    ).run()

    assert second.session_published
    assert _urls_fetched(second_transport) == [], "a no-op run opens no socket"
    assert second.archive is None, "the bundle already exists, so it is not re-written"
    for outcome in second.outcomes.values():
        assert outcome.report.published == 0
        assert outcome.report.skipped_published == 1

    # The sync_state row is byte-for-byte what the first run left — no new attempt, no re-publish.
    after = store.get(NSE_BHAVCOPY, UDIFF_SESSION)
    assert after is not None
    assert (after.state, after.attempts, after.updated_at) == (
        before.state,
        before.attempts,
        before.updated_at,
    )
