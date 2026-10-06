"""Alert triggers (M13.2) — the failures nobody was seeing, turned into `Alerter.send` calls.

C.3 built the channel and left the triggers to their owners (BACKLOG C.3); four conditions had no
owner and so paged nobody:

* an ingestion source **FAILED for N consecutive sessions** (`sync_state`'s failure streak, the
  same number `/status/sources` serves);
* a **quality check gone red** — any open ERROR `quality_flag`, invariant #10's "not green";
* the **holiday calendar running out** — `nse_holidays.yaml` coverage ending within the lead window
  of the injected clock's today, after which `expected_sessions` refuses every date and the daily
  pipeline hard-fails (BACKLOG C.2);
* a **scheduled job that raised** — the newest finished attempt recorded FAILED in `job_run`.

One scheduler job, `failure_alerts`, evaluates all four on a short cron rather than hooking each
transition, because the conditions are *states*, not events: a streak is three rows, red is a
count, the calendar expires with nobody touching it. Each tick asks "what is true now" and diffs
it against `alert_condition` (0013):

* a condition seen with no open row is an **onset** — the only tick that pages;
* a condition seen with an open row is the same news — recorded as seen, never re-sent;
* an open row no longer seen has **cleared** — one INFO resolution, and the row is closed.

The ledger lives in Postgres rather than in the alerter's in-process window, so a scheduler
restart is not a reason to page about a five-day-old streak again.

A trigger whose evaluation fails resolves nothing (absence of evidence is not a cleared alarm) and
the job raises after the others have run — which makes `failure_alerts` itself a FAILED job, and
the next tick pages about that.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dataplatform.alerts import Alerter, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import TradingCalendar
from dataplatform.ingest.calendar import load as load_calendar
from dataplatform.logging import get_logger
from dataplatform.status import SourceStatus, SyncStateStore
from dataplatform.store.db import Connection, connection

if TYPE_CHECKING:  # the scheduler imports this module lazily; keep the edge one-way at runtime
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "CALENDAR_KEY",
    "MAX_DETAIL_CHARS",
    "AlertCondition",
    "ConditionLedger",
    "FailedAttempt",
    "OpenQualityCheck",
    "TickReport",
    "Trigger",
    "TriggerEvaluationError",
    "calendar_expiry_conditions",
    "failed_job_conditions",
    "failed_streak_conditions",
    "quality_red_conditions",
    "read_failed_jobs",
    "read_red_quality_checks",
    "reconcile",
    "redact",
    "run_failure_alerts",
    "run_failure_alerts_job",
]

#: The calendar condition's key. There is one holiday file, so there is one key.
CALENDAR_KEY: Final = "calendar:nse_holidays:coverage_expiry"

#: How much of an upstream error string an alert body carries. Enough to say what broke; the full
#: text is in `sync_state.last_error` / `job_run.error`, which is where someone goes next anyway.
MAX_DETAIL_CHARS: Final = 400

log = get_logger(__name__)


class Trigger(StrEnum):
    """Which watcher raised a condition — `alert_condition.trigger` verbatim."""

    INGEST_FAILED_STREAK = "ingest_failed_streak"
    QUALITY_RED = "quality_red"
    CALENDAR_EXPIRY = "calendar_expiry"
    JOB_FAILED = "job_failed"


@dataclass(frozen=True, slots=True)
class AlertCondition:
    """One thing that is wrong right now, already phrased as the alert its onset sends.

    `dedup_key` is the identity: two ticks that produce the same key are the same news. It never
    carries a date or a count, so a streak growing from 3 to 4 is not a new condition.
    """

    trigger: Trigger
    dedup_key: str
    severity: Severity
    title: str
    body: str


class TriggerEvaluationError(RuntimeError):
    """One or more triggers could not be evaluated, or an alert could not be delivered.

    Raised at the end of a tick, after every other trigger has had its turn, so one broken query
    cannot silence the other three.
    """


# ── redaction ────────────────────────────────────────────────────────────────────────────────

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_URL_QUERY = re.compile(r"(?P<path>://[^\s?#]+)\?[^\s#]*")
_TELEGRAM_TOKEN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_CREDENTIAL_PAIR = re.compile(
    r"(?P<name>password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization)"
    r"(?P<sep>\s*[=:]\s*)(?P<value>\S+)",
    re.IGNORECASE,
)


def redact(text: str, *, limit: int = MAX_DETAIL_CHARS) -> str:
    """`text` with anything credential-shaped removed, then truncated to `limit` characters.

    What it does: masks URL userinfo (a DSN's `user:password@`), drops URL query strings (where a
    token travels when it travels in a URL), masks Telegram bot tokens, and masks the value of any
    `password=`/`token:`-style pair.
    What it assumes: the upstream error strings are diagnostic text, not structured data — an
    over-eager mask costs a little detail, an under-eager one publishes a credential.
    What it never does: pass a secret through on the grounds that it was already in a log line.
    An alert leaves the box; the log does not.
    """
    text = _URL_USERINFO.sub(r"\g<scheme>***@", text)
    text = _URL_QUERY.sub(r"\g<path>?***", text)
    text = _TELEGRAM_TOKEN.sub("bot***", text)
    text = _CREDENTIAL_PAIR.sub(r"\g<name>\g<sep>***", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


# ── the four evaluators — pure, so each is proved without a database ────────────────────────


def failed_streak_conditions(
    statuses: Iterable[SourceStatus], *, threshold: int
) -> list[AlertCondition]:
    """One CRITICAL condition per source whose failure streak has reached `threshold`.

    What it does: reads `SourceStatus.failure_streak` — FAILED dates since the newest date that is
    neither FAILED nor GAP, the `/status/sources` definition — and compares it with `>=`.
    What it assumes: `threshold >= 1`; a threshold of 0 would page about every healthy source.
    What it never does: page below the threshold. A single FAILED session self-heals on the next
    EOD run often enough that paging on it would teach the owner to ignore the channel.
    """
    if threshold < 1:
        raise ValueError(f"failure-streak threshold must be at least 1, got {threshold}")
    conditions = []
    for status in statuses:
        if status.failure_streak < threshold:
            continue
        error = "none recorded" if status.last_error is None else redact(status.last_error)
        retryable = {True: "yes", False: "no", None: "unknown"}[status.last_failure_retryable]
        last_success = (
            "never" if status.last_success_date is None else status.last_success_date.isoformat()
        )
        conditions.append(
            AlertCondition(
                trigger=Trigger.INGEST_FAILED_STREAK,
                dedup_key=f"ingest:{status.source}:failed_streak",
                severity=Severity.CRITICAL,
                title=f"{status.source}: {status.failure_streak} consecutive sessions FAILED",
                body=(
                    f"Source {status.source} has failed {status.failure_streak} consecutive "
                    f"sessions (alert threshold {threshold}).\n"
                    f"Latest failed session: {status.last_failure_date}\n"
                    f"Last success: {last_success}\n"
                    f"Retryable: {retryable}\n"
                    f"Last error: {error}\n"
                    "See GET /status/sources and /status/sync."
                ),
            )
        )
    return conditions


@dataclass(frozen=True, slots=True)
class OpenQualityCheck:
    """One quality check with open ERROR flags — the shape `read_red_quality_checks` returns."""

    check_name: str
    open_errors: int
    first_date: date
    last_date: date


def quality_red_conditions(checks: Iterable[OpenQualityCheck]) -> list[AlertCondition]:
    """One CRITICAL condition per check that has at least one open ERROR flag.

    Keyed by check, not by flag: a sentinel that raises forty flags on one bad day is one piece of
    news, and the flag count in the body says how big it is. WARN and INFO flags never page — they
    are not what invariant #10 means by red.
    """
    return [
        AlertCondition(
            trigger=Trigger.QUALITY_RED,
            dedup_key=f"quality:{check.check_name}:red",
            severity=Severity.CRITICAL,
            title=f"quality check {check.check_name} is red ({check.open_errors} open ERROR)",
            body=(
                f"Quality check {check.check_name} has {check.open_errors} unresolved ERROR "
                f"flag(s), for logical dates {check.first_date} .. {check.last_date}.\n"
                "Red data blocks trading on those dates (invariant #10). "
                "See GET /status/quality."
            ),
        )
        for check in checks
        if check.open_errors > 0
    ]


def calendar_expiry_conditions(
    coverage_end: date, *, today: date, lead_days: int
) -> list[AlertCondition]:
    """A WARNING condition when the holiday calendar ends `lead_days` or fewer days after `today`.

    What it does: one comparison, `coverage_end - today <= lead_days`, so the alert fires on the
    first day inside the window and keeps the condition open through and past the end date.
    What it assumes: `today` is the injected clock's (B10).
    What it never does: extend the calendar. Appending next year's holidays from the NSE circular
    is a human edit to `dataplatform/ingest/data/nse_holidays.yaml`; this only says it is due.
    """
    if lead_days < 0:
        raise ValueError(f"calendar lead window must not be negative, got {lead_days}")
    remaining = (coverage_end - today).days
    if remaining > lead_days:
        return []
    when = f"ends in {remaining} day(s)" if remaining >= 0 else f"ended {-remaining} day(s) ago"
    return [
        AlertCondition(
            trigger=Trigger.CALENDAR_EXPIRY,
            dedup_key=CALENDAR_KEY,
            severity=Severity.WARNING,
            title=f"NSE holiday calendar coverage {when} ({coverage_end})",
            body=(
                f"dataplatform/ingest/data/nse_holidays.yaml covers trading dates up to "
                f"{coverage_end}; today is {today}, so coverage {when}.\n"
                "Past that date expected_sessions refuses every date and the daily EOD pipeline "
                "hard-fails. Append the next year's holidays from the NSE holiday circular and "
                "move coverage.end (BACKLOG C.2)."
            ),
        )
    ]


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    """A job whose newest finished attempt raised — the shape `read_failed_jobs` returns."""

    job_name: str
    started_at: datetime
    error: str | None


def failed_job_conditions(attempts: Iterable[FailedAttempt]) -> list[AlertCondition]:
    """One CRITICAL condition per job whose newest finished attempt was FAILED.

    A job that fails three nights running without a success between is one condition: the key is
    the job, not the run, so the onset pages and the repeats do not. A success clears it.
    """
    conditions = []
    for attempt in attempts:
        error = "none recorded" if attempt.error is None else redact(attempt.error)
        conditions.append(
            AlertCondition(
                trigger=Trigger.JOB_FAILED,
                dedup_key=f"job:{attempt.job_name}:failed",
                severity=Severity.CRITICAL,
                title=f"scheduled job {attempt.job_name} raised",
                body=(
                    f"The newest run of {attempt.job_name}, started "
                    f"{attempt.started_at.isoformat()}, raised and was recorded FAILED.\n"
                    f"Error: {error}\n"
                    "See GET /status/jobs; re-run with "
                    f"`python -m dataplatform.scheduler run-once {attempt.job_name}`."
                ),
            )
        )
    return conditions


# ── the reads ────────────────────────────────────────────────────────────────────────────────

_RED_QUALITY_SQL: Final = """
    SELECT check_name, count(*), min(logical_date), max(logical_date)
    FROM quality_flag
    WHERE NOT resolved AND severity = 'ERROR'
    GROUP BY check_name
    ORDER BY check_name
"""

#: The newest attempt per job that actually ran to an outcome. RUNNING has no outcome yet and
#: SKIPPED_LOCKED means another process held the lock, so neither may clear — or raise — a failure.
_FAILED_JOBS_SQL: Final = """
    SELECT job_name, started_at, error
    FROM (
        SELECT DISTINCT ON (job_name) job_name, state, started_at, error
        FROM job_run
        WHERE state NOT IN ('RUNNING', 'SKIPPED_LOCKED') AND job_name = ANY(%s)
        ORDER BY job_name, started_at DESC
    ) newest
    WHERE state = 'FAILED'
    ORDER BY job_name
"""


def read_red_quality_checks(conn: Connection) -> list[OpenQualityCheck]:
    """Every quality check with open ERROR flags, by name."""
    return [
        OpenQualityCheck(
            check_name=str(row[0]), open_errors=int(row[1]), first_date=row[2], last_date=row[3]
        )
        for row in conn.execute(_RED_QUALITY_SQL).fetchall()
    ]


def read_failed_jobs(conn: Connection, job_names: Sequence[str]) -> list[FailedAttempt]:
    """Registered jobs whose newest finished attempt raised.

    Only `job_names` — the registry — is asked about: a job removed from the registry whose last
    run failed would otherwise hold an alert open forever with nothing able to clear it.
    """
    return [
        FailedAttempt(
            job_name=str(row[0]), started_at=row[1], error=None if row[2] is None else str(row[2])
        )
        for row in conn.execute(_FAILED_JOBS_SQL, (list(job_names),)).fetchall()
    ]


# ── the ledger and the diff ──────────────────────────────────────────────────────────────────


class ConditionLedger:
    """`alert_condition` (0013): which conditions are open, i.e. have already paged.

    What it does: reads the open keys of a trigger, opens/reopens a key at onset, stamps
    `last_seen_at` on a repeat, and closes a key on resolution.
    What it assumes: an autocommit connection, so a row written after a successful send survives a
    later failure in the same tick — the alert went out, and the ledger must say so.
    What it never does: delete. A resolved row stays as the record of its last episode.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def open_keys(self, trigger: Trigger) -> dict[str, str]:
        """Open condition keys of `trigger`, mapped to the title they paged with."""
        rows = self._conn.execute(
            "SELECT dedup_key, title FROM alert_condition "
            "WHERE trigger = %s AND resolved_at IS NULL",
            (trigger.value,),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def open(self, condition: AlertCondition, at: datetime) -> None:
        """Record an onset: a new open row, or a resolved row reopened as a fresh episode."""
        self._conn.execute(
            "INSERT INTO alert_condition "
            "(dedup_key, trigger, severity, title, opened_at, last_seen_at, resolved_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, NULL) "
            "ON CONFLICT (dedup_key) DO UPDATE SET trigger = EXCLUDED.trigger, "
            "severity = EXCLUDED.severity, title = EXCLUDED.title, "
            "opened_at = EXCLUDED.opened_at, last_seen_at = EXCLUDED.last_seen_at, "
            "resolved_at = NULL",
            (
                condition.dedup_key,
                condition.trigger.value,
                condition.severity.value,
                condition.title,
                at,
                at,
            ),
        )

    def touch(self, keys: Sequence[str], at: datetime) -> None:
        """Record that open conditions are still present at `at`."""
        if keys:
            self._conn.execute(
                "UPDATE alert_condition SET last_seen_at = %s "
                "WHERE dedup_key = ANY(%s) AND resolved_at IS NULL",
                (at, list(keys)),
            )

    def resolve(self, key: str, at: datetime) -> None:
        """Close an open condition."""
        self._conn.execute(
            "UPDATE alert_condition SET resolved_at = %s, last_seen_at = %s "
            "WHERE dedup_key = %s AND resolved_at IS NULL",
            (at, at, key),
        )


@dataclass(slots=True)
class TickReport:
    """What one tick did — logged, and what the tests read."""

    opened: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def reconcile(
    trigger: Trigger,
    conditions: Sequence[AlertCondition],
    *,
    ledger: ConditionLedger,
    alerter: Alerter,
    at: datetime,
    report: TickReport,
) -> None:
    """Diff one trigger's current conditions against its open rows; page onsets and resolutions.

    What it does: sends the onset alert for every condition with no open row and then opens it;
    touches the ones already open; sends one INFO resolution for every open row no longer present
    and then closes it.
    What it assumes: `conditions` is the *complete* current set for `trigger` — a partial set would
    resolve the missing ones. A trigger that could not be evaluated must not reach here.
    What it never does: record an onset whose send raised. The row stays absent, so the next tick
    tries again rather than believing the owner was told. A delivery failure is collected into
    `report.errors` and the remaining conditions still get their turn.
    """
    open_rows = ledger.open_keys(trigger)
    current = {condition.dedup_key: condition for condition in conditions}

    repeats = [key for key in current if key in open_rows]
    ledger.touch(repeats, at)
    report.still_open.extend(repeats)

    for key, condition in current.items():
        if key in open_rows:
            continue
        try:
            outcome = alerter.send(condition.severity, condition.title, condition.body, key)
        except Exception as error:
            report.errors.append(f"{key}: onset alert not delivered: {type(error).__name__}")
            log.error("alert_trigger.send_failed", dedup_key=key, error_type=type(error).__name__)
            continue
        ledger.open(condition, at)
        report.opened.append(key)
        log.warning(
            "alert_trigger.onset",
            trigger=trigger.value,
            dedup_key=key,
            outcome=outcome.value,
        )

    for key, title in open_rows.items():
        if key in current:
            continue
        try:
            outcome = alerter.send(
                Severity.INFO,
                f"resolved: {title}",
                f"Cleared at {at.isoformat()}: {title}",
                f"{key}:resolved",
            )
        except Exception as error:
            report.errors.append(f"{key}: resolution not delivered: {type(error).__name__}")
            log.error("alert_trigger.send_failed", dedup_key=key, error_type=type(error).__name__)
            continue
        ledger.resolve(key, at)
        report.resolved.append(key)
        log.info(
            "alert_trigger.resolved",
            trigger=trigger.value,
            dedup_key=key,
            outcome=outcome.value,
        )


# ── the tick ─────────────────────────────────────────────────────────────────────────────────


def run_failure_alerts(
    conn: Connection,
    *,
    settings: Settings,
    clock: Clock,
    alerter: Alerter,
    calendar: TradingCalendar,
    job_names: Sequence[str],
) -> TickReport:
    """Evaluate all four triggers once and page what changed since the last tick.

    What it does: for each trigger in turn, reads the current conditions, then `reconcile`s them.
    A trigger whose read raises is recorded in `report.errors` and skipped — nothing of it is
    resolved — while the others still run.
    What it assumes: `conn` is autocommit and migrated through 0013; `clock` is the run's (B10).
    What it never does: raise for a failed trigger (`run_failure_alerts_job` turns errors into a
    FAILED run), or read the wall clock.
    """
    now = clock.now()
    today = clock.today()
    report = TickReport()
    ledger = ConditionLedger(conn)
    store = SyncStateStore(conn, clock=clock, calendar=calendar)
    threshold = settings.alert_failure_streak_threshold

    evaluators: tuple[tuple[Trigger, Callable[[], list[AlertCondition]]], ...] = (
        (
            Trigger.INGEST_FAILED_STREAK,
            lambda: failed_streak_conditions(store.source_statuses(), threshold=threshold),
        ),
        (Trigger.QUALITY_RED, lambda: quality_red_conditions(read_red_quality_checks(conn))),
        (
            Trigger.CALENDAR_EXPIRY,
            lambda: calendar_expiry_conditions(
                calendar.coverage_end, today=today, lead_days=settings.alert_calendar_lead_days
            ),
        ),
        (Trigger.JOB_FAILED, lambda: failed_job_conditions(read_failed_jobs(conn, job_names))),
    )
    for trigger, evaluate in evaluators:
        try:
            conditions = evaluate()
        except Exception as error:
            report.errors.append(f"{trigger.value}: not evaluated: {type(error).__name__}")
            log.error(
                "alert_trigger.evaluation_failed",
                trigger=trigger.value,
                error_type=type(error).__name__,
                error=redact(str(error)),
            )
            continue
        reconcile(trigger, conditions, ledger=ledger, alerter=alerter, at=now, report=report)

    log.info(
        "alert_trigger.tick",
        opened=report.opened,
        still_open=len(report.still_open),
        resolved=report.resolved,
        errors=len(report.errors),
    )
    return report


def run_failure_alerts_job(context: JobContext) -> None:
    """The `failure_alerts` scheduler job body: one tick against the production wiring.

    What it does: loads the holiday calendar fresh from disk — not the per-process cached copy, so
    a year appended to the YAML is seen without a scheduler restart — builds the configured
    alerter, and runs `run_failure_alerts` over the production registry's job names.
    What it never does: report a green run when a trigger could not be evaluated or an alert could
    not be delivered; it raises `TriggerEvaluationError`, which the runner records FAILED.
    """
    from dataplatform.scheduler.registry import default_registry

    alerter = build_alerter(context.settings, clock=context.clock)
    with connection(context.settings, autocommit=True) as conn:
        report = run_failure_alerts(
            conn,
            settings=context.settings,
            clock=context.clock,
            alerter=alerter,
            calendar=load_calendar(),
            job_names=default_registry().names(),
        )
    if not report.ok:
        raise TriggerEvaluationError("; ".join(report.errors))
