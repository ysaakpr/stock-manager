"""M13.2: the four alert triggers fire on onset, stay silent on repeat, and when healthy.

The evaluators are pure and proved here directly; `reconcile` — the onset/repeat/resolve diff — is
proved against an in-memory ledger, so the "one alert per onset" property does not depend on a
database. `tests/integration/test_failure_alerts.py` proves the same against real tables.

Every alerter here is built with a zero dedup window: silence on a repeat must come from the
condition ledger, not from the alerter's in-process window hiding a second send.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import pytest

from dataplatform.alert_triggers import (
    CALENDAR_KEY,
    AlertCondition,
    ConditionLedger,
    FinishedAttempt,
    OpenQualityCheck,
    TickReport,
    Trigger,
    calendar_expiry_conditions,
    failed_job_conditions,
    failed_streak_conditions,
    quality_red_conditions,
    reconcile,
    redact,
    tick,
)
from dataplatform.alerts import AlertOutcome, BaseAlerter, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.calendar import CalendarDataError, trading_calendar
from dataplatform.status import SourceStatus, SyncState

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=IST)


class RecordingAlerter(BaseAlerter):
    """A real `BaseAlerter` (dedup and all) whose wire is a list."""

    channel = "test"

    def __init__(
        self, clock: FrozenClock, *, fail: bool = False, window: timedelta = timedelta(0)
    ) -> None:
        super().__init__(clock=clock, dedup_window=window)
        self.sent: list[tuple[Severity, str, str, str]] = []
        self.fail = fail

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        if self.fail:
            raise ConnectionError("relay down")
        self.sent.append((severity, title, body, dedup_key))


class MemoryLedger(ConditionLedger):
    """`ConditionLedger`'s contract over a dict: key -> (trigger, title, resolved)."""

    def __init__(self, *, broken_writes: bool = False) -> None:
        self.rows: dict[str, tuple[Trigger, str, bool]] = {}
        self.broken_writes = broken_writes

    @contextmanager
    def transaction(self) -> Iterator[object]:
        """Postgres semantics: a block that raises leaves the rows as they were."""
        snapshot = dict(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = snapshot
            raise

    def open_keys(self, trigger: Trigger) -> dict[str, str]:
        return {
            key: title
            for key, (row_trigger, title, resolved) in self.rows.items()
            if row_trigger is trigger and not resolved
        }

    def open(self, condition: AlertCondition, at: datetime) -> None:
        if self.broken_writes:
            raise OSError("ledger write failed")
        self.rows[condition.dedup_key] = (condition.trigger, condition.title, False)

    def touch(self, keys: Sequence[str], at: datetime) -> None:
        pass

    def resolve(self, key: str, at: datetime) -> None:
        trigger, title, _ = self.rows[key]
        self.rows[key] = (trigger, title, True)


def _status(source: str, *, streak: int, error: str | None = "HTTP 503") -> SourceStatus:
    return SourceStatus(
        source=source,
        last_success_date=date(2026, 10, 1),
        last_success_at=None,
        latest_date=date(2026, 10, 6),
        lag_days=5,
        lag_sessions=3,
        failure_streak=streak,
        last_failure_date=date(2026, 10, 6) if streak else None,
        last_error=error if streak else None,
        last_failure_retryable=True if streak else None,
        counts={SyncState.FAILED: streak, SyncState.PUBLISHED: 10},
    )


# ── (a) ingestion FAILED streak ──────────────────────────────────────────────────────────────


def test_a_streak_at_the_threshold_fires_and_one_below_does_not() -> None:
    conditions = failed_streak_conditions(
        [_status("nse_bhavcopy", streak=3), _status("nse_delivery", streak=2)], threshold=3
    )
    assert [c.dedup_key for c in conditions] == ["ingest:nse_bhavcopy:failed_streak"]
    assert conditions[0].severity is Severity.CRITICAL
    assert "3 consecutive" in conditions[0].title


def test_healthy_sources_raise_no_streak_condition() -> None:
    """Fails if the comparison is inverted: a healthy source must never page."""
    healthy = [_status("nse_bhavcopy", streak=0), _status("bse_bhavcopy", streak=0)]
    assert failed_streak_conditions(healthy, threshold=1) == []
    assert failed_streak_conditions(healthy, threshold=3) == []


def test_the_streak_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        failed_streak_conditions([], threshold=0)


def test_a_longer_streak_is_the_same_condition() -> None:
    """The key carries no count, so day 4 of a streak is not a new onset."""
    three = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)
    four = failed_streak_conditions([_status("nse_bhavcopy", streak=4)], threshold=3)
    assert three[0].dedup_key == four[0].dedup_key


# ── (b) quality red ──────────────────────────────────────────────────────────────────────────


def test_a_check_with_open_errors_is_red() -> None:
    checks = [
        OpenQualityCheck("price_spike", 4, date(2026, 10, 5), date(2026, 10, 6)),
        OpenQualityCheck("volume_outlier", 0, date(2026, 10, 5), date(2026, 10, 5)),
    ]
    conditions = quality_red_conditions(checks)
    assert [c.dedup_key for c in conditions] == ["quality:price_spike:red"]
    assert "4 unresolved ERROR" in conditions[0].body


def test_no_open_errors_means_no_quality_condition() -> None:
    assert quality_red_conditions([]) == []


# ── (c) holiday-calendar expiry, against a fixed clock ───────────────────────────────────────


@pytest.mark.parametrize(
    ("today", "fires"),
    [
        (date(2026, 10, 6), False),  # 86 days out — the day this task was written
        (date(2026, 10, 31), False),  # 61 days out — one day before the window
        (date(2026, 11, 1), True),  # exactly 60 days out — the first day inside it
        (date(2026, 12, 31), True),  # the last covered day
        (date(2027, 1, 4), True),  # past the end: the condition stays open
    ],
)
def test_calendar_expiry_fires_inside_the_lead_window_only(today: date, fires: bool) -> None:
    clock = FrozenClock(today)
    coverage_end = trading_calendar().coverage_end
    assert coverage_end == date(2026, 12, 31), "the checked-in calendar moved; update this test"
    conditions = calendar_expiry_conditions(coverage_end, today=clock.today(), lead_days=60)
    assert bool(conditions) is fires
    if fires:
        assert conditions[0].dedup_key == CALENDAR_KEY
        assert conditions[0].severity is Severity.WARNING


def test_calendar_expiry_names_how_long_ago_it_ended() -> None:
    (condition,) = calendar_expiry_conditions(
        date(2026, 12, 31), today=date(2027, 1, 4), lead_days=60
    )
    assert "ended 4 day(s) ago" in condition.title


# ── (d) scheduled job raised ─────────────────────────────────────────────────────────────────


def test_a_failed_job_is_one_condition_per_job() -> None:
    attempts = [
        FinishedAttempt(
            "eod_pipeline", "FAILED", NOW, "EodPipelineError: nse_bhavcopy not published"
        )
    ]
    (condition,) = failed_job_conditions(attempts)
    assert condition.dedup_key == "job:eod_pipeline:failed"
    assert "EodPipelineError" in condition.body


def test_no_failed_jobs_means_no_job_condition() -> None:
    assert failed_job_conditions([]) == []


@pytest.mark.parametrize("state", ["SUCCEEDED", "TIMED_OUT"])
def test_a_job_whose_newest_outcome_did_not_raise_is_not_a_condition(state: str) -> None:
    """Fails if the state filter is inverted. TIMED_OUT clears too: the job returned, it did not
    raise — the overrun is `/status/jobs`' FAILING, not this trigger's page."""
    attempts = [FinishedAttempt("eod_pipeline", state, NOW, None)]
    assert failed_job_conditions(attempts) == []


def test_only_the_jobs_that_raised_are_conditions() -> None:
    attempts = [
        FinishedAttempt("eod_pipeline", "SUCCEEDED", NOW, None),
        FinishedAttempt("daily_snapshot", "FAILED", NOW, "boom"),
        FinishedAttempt("tri_refresh", "TIMED_OUT", NOW, "ran for 999s"),
    ]
    assert [c.dedup_key for c in failed_job_conditions(attempts)] == ["job:daily_snapshot:failed"]


# ── no secret in an alert body ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("connection to postgresql://trader:hunter2@db:5432/trading failed", "hunter2"),
        ("POST https://api.telegram.org/bot123456:AAH-secret_tok/sendMessage 403", "AAH-secret"),
        ("GET https://example.org/data?apikey=abc123XYZ&d=1 -> 401", "abc123XYZ"),
        ("login failed: password=s3cr3t for user ops", "s3cr3t"),
        ("Authorization: Bearer-xyz-token rejected", "Bearer-xyz-token"),
    ],
)
def test_redact_strips_credentials(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_a_streak_alert_body_never_carries_the_dsn_password() -> None:
    status = _status(
        "nse_bhavcopy", streak=5, error="OperationalError: postgresql://u:pa55word@h/db refused"
    )
    (condition,) = failed_streak_conditions([status], threshold=3)
    assert "pa55word" not in condition.body and "pa55word" not in condition.title


def test_redact_truncates_long_errors() -> None:
    assert len(redact("x" * 5000)) == 400


# ── onset, repeat, resolution ────────────────────────────────────────────────────────────────


def _tick(
    conditions: list[AlertCondition], ledger: MemoryLedger, alerter: RecordingAlerter
) -> TickReport:
    report = TickReport()
    reconcile(
        Trigger.INGEST_FAILED_STREAK,
        conditions,
        ledger=ledger,
        alerter=alerter,
        at=NOW,
        report=report,
    )
    return report


def test_onset_pages_once_and_repeats_stay_silent() -> None:
    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    streak = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)

    first = _tick(streak, ledger, alerter)
    assert first.opened == ["ingest:nse_bhavcopy:failed_streak"]
    assert len(alerter.sent) == 1

    for _ in range(5):
        repeat = _tick(streak, ledger, alerter)
        assert repeat.opened == [] and repeat.still_open == ["ingest:nse_bhavcopy:failed_streak"]
    assert len(alerter.sent) == 1, "a repeat of an open condition must not page again"


def test_a_cleared_condition_sends_one_resolution_and_can_reopen() -> None:
    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    streak = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)
    _tick(streak, ledger, alerter)

    cleared = _tick([], ledger, alerter)
    assert cleared.resolved == ["ingest:nse_bhavcopy:failed_streak"]
    severity, title, _, key = alerter.sent[-1]
    assert severity is Severity.INFO and title.startswith("resolved: ")
    assert key == "ingest:nse_bhavcopy:failed_streak:resolved"

    assert _tick([], ledger, alerter).resolved == [], "a resolution is sent once"
    assert _tick(streak, ledger, alerter).opened == ["ingest:nse_bhavcopy:failed_streak"]
    assert len(alerter.sent) == 3  # onset, resolution, re-onset


def test_healthy_ticks_send_nothing() -> None:
    """The inverted-comparison guard end to end: healthy inputs through the whole diff."""
    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    healthy = failed_streak_conditions([_status("nse_bhavcopy", streak=0)], threshold=3)
    for _ in range(3):
        report = _tick(healthy, ledger, alerter)
        assert report.opened == report.resolved == report.still_open == []
    assert alerter.sent == []


def test_an_undelivered_onset_is_not_recorded_and_is_retried() -> None:
    clock = FrozenClock(NOW)
    ledger = MemoryLedger()
    streak = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)

    failed = _tick(streak, ledger, RecordingAlerter(clock, fail=True))
    assert failed.opened == [] and failed.errors and not failed.ok
    assert ledger.rows == {}, "an onset whose send raised must not be marked as told"

    working = RecordingAlerter(clock)
    assert _tick(streak, ledger, working).opened == ["ingest:nse_bhavcopy:failed_streak"]
    assert len(working.sent) == 1


def test_resolution_only_touches_its_own_trigger() -> None:
    """A tick of one trigger must not resolve another trigger's open conditions."""
    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    (calendar,) = calendar_expiry_conditions(date(2026, 12, 31), today=NOW.date(), lead_days=60)
    report = TickReport()
    reconcile(
        Trigger.CALENDAR_EXPIRY, [calendar], ledger=ledger, alerter=alerter, at=NOW, report=report
    )
    assert _tick([], ledger, alerter).resolved == []
    assert ledger.open_keys(Trigger.CALENDAR_EXPIRY) == {CALENDAR_KEY: calendar.title}


def test_a_suppressed_onset_is_not_recorded_as_told() -> None:
    """Only a SENT outcome opens the row: a send the alerter's own window swallowed did not page."""
    clock = FrozenClock(NOW)
    alerter = RecordingAlerter(clock, window=timedelta(hours=6))
    streak = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)
    key = streak[0].dedup_key
    assert alerter.send(Severity.INFO, "earlier", "earlier", key) is AlertOutcome.SENT

    report = _tick(streak, MemoryLedger(), alerter)
    assert report.opened == [] and not report.ok
    assert "suppressed" in report.errors[0]


def test_a_ledger_write_failure_sends_nothing() -> None:
    """Write before send: a broken ledger costs a page, never a page repeated every tick."""
    alerter = RecordingAlerter(FrozenClock(NOW))
    streak = failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)
    for _ in range(3):
        report = _tick(streak, MemoryLedger(broken_writes=True), alerter)
        assert report.opened == [] and not report.ok
    assert alerter.sent == []


# ── one trigger's failure never silences the others ──────────────────────────────────────────


def _conditions_for(trigger: Trigger) -> list[AlertCondition]:
    if trigger is Trigger.INGEST_FAILED_STREAK:
        return failed_streak_conditions([_status("nse_bhavcopy", streak=3)], threshold=3)
    if trigger is Trigger.QUALITY_RED:
        return quality_red_conditions(
            [OpenQualityCheck("price_spike", 1, date(2026, 11, 1), date(2026, 11, 1))]
        )
    return failed_job_conditions([FinishedAttempt("eod_pipeline", "FAILED", NOW, "boom")])


def test_a_malformed_holiday_file_fails_only_the_calendar_trigger() -> None:
    """The review's blocking case: a bad YAML edit must not stop (a), (b) and (d) from paging."""

    def broken_calendar() -> list[AlertCondition]:
        raise CalendarDataError("nse_holidays.yaml does not match the holiday schema")

    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    report = tick(
        [
            (Trigger.INGEST_FAILED_STREAK, lambda: _conditions_for(Trigger.INGEST_FAILED_STREAK)),
            (Trigger.CALENDAR_EXPIRY, broken_calendar),
            (Trigger.QUALITY_RED, lambda: _conditions_for(Trigger.QUALITY_RED)),
            (Trigger.JOB_FAILED, lambda: _conditions_for(Trigger.JOB_FAILED)),
        ],
        ledger=ledger,
        alerter=alerter,
        at=NOW,
    )
    assert sorted(report.opened) == sorted(
        ["ingest:nse_bhavcopy:failed_streak", "quality:price_spike:red", "job:eod_pipeline:failed"]
    )
    assert len(alerter.sent) == 3
    assert report.errors == ["calendar_expiry: not evaluated: CalendarDataError"]


def test_an_unevaluated_trigger_resolves_nothing() -> None:
    """Absence of evidence is not a cleared alarm: a broken read keeps its open rows open."""
    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), MemoryLedger()
    (calendar,) = calendar_expiry_conditions(date(2026, 12, 31), today=NOW.date(), lead_days=60)
    tick([(Trigger.CALENDAR_EXPIRY, lambda: [calendar])], ledger=ledger, alerter=alerter, at=NOW)

    def broken() -> list[AlertCondition]:
        raise CalendarDataError("bad yaml")

    report = tick([(Trigger.CALENDAR_EXPIRY, broken)], ledger=ledger, alerter=alerter, at=NOW)
    assert report.resolved == []
    assert ledger.open_keys(Trigger.CALENDAR_EXPIRY) == {CALENDAR_KEY: calendar.title}


def test_a_ledger_read_failure_in_one_trigger_does_not_abort_the_rest() -> None:
    class FlakyLedger(MemoryLedger):
        def open_keys(self, trigger: Trigger) -> dict[str, str]:
            if trigger is Trigger.QUALITY_RED:
                raise OSError("connection reset")
            return super().open_keys(trigger)

    alerter, ledger = RecordingAlerter(FrozenClock(NOW)), FlakyLedger()
    report = tick(
        [
            (Trigger.QUALITY_RED, lambda: _conditions_for(Trigger.QUALITY_RED)),
            (Trigger.JOB_FAILED, lambda: _conditions_for(Trigger.JOB_FAILED)),
        ],
        ledger=ledger,
        alerter=alerter,
        at=NOW,
    )
    assert report.opened == ["job:eod_pipeline:failed"]
    assert report.errors == ["quality_red: not reconciled: OSError"]
