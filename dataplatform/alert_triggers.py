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

from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Final

from dataplatform.alerts import Alerter, AlertOutcome, Severity, build_alerter
from dataplatform.clock import IST, Clock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.calendar import load as load_calendar
from dataplatform.logging import get_logger
from dataplatform.redaction import mask_secrets
from dataplatform.status import SourceStatus, SyncStateStore
from dataplatform.store.db import Connection, connection

if TYPE_CHECKING:  # the scheduler imports this module lazily; keep the edge one-way at runtime
    from dataplatform.scheduler.registry import JobContext, JobRegistry

__all__ = [
    "CALENDAR_KEY",
    "MAX_DETAIL_CHARS",
    "AlertCondition",
    "ConditionLedger",
    "FinishedAttempt",
    "OpenQualityCheck",
    "RetryDeadline",
    "TickReport",
    "Trigger",
    "TriggerEvaluationError",
    "TriggerEvaluator",
    "calendar_expiry_conditions",
    "failed_job_conditions",
    "failed_streak_conditions",
    "quality_red_conditions",
    "read_last_attempts",
    "read_red_quality_checks",
    "reconcile",
    "redact",
    "registry_retry_deadline",
    "run_failure_alerts",
    "run_failure_alerts_job",
    "tick",
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


def redact(text: str, *, limit: int = MAX_DETAIL_CHARS) -> str:
    """`text` with anything credential-shaped removed, then truncated to `limit` characters.

    What it does: `dataplatform.redaction.mask_secrets` (URL userinfo, URL query strings, bot and
    prefixed API tokens, `Bearer` credentials, `password=`/`token:`-style pairs), then collapses
    whitespace.
    What it assumes: the upstream error strings are diagnostic text, not structured data — an
    over-eager mask costs a little detail, an under-eager one publishes a credential.
    What it never does: pass a secret through on the grounds that it was already in a log line.
    An alert leaves the box; the log does not.
    """
    text = mask_secrets(text)
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
class FinishedAttempt:
    """A job's newest attempt that reached an outcome — the shape `read_last_attempts` returns.

    `state` is `job_run.state`: FAILED, SUCCEEDED or TIMED_OUT. RUNNING and SKIPPED_LOCKED are
    excluded at the read, because neither is an outcome. `retry_pending` is `job_run.retry_pending`
    (0014): the run raised a `RetryPendingError`, decided by the runner from the exception's type.
    """

    job_name: str
    state: str
    started_at: datetime
    error: str | None
    retry_pending: bool = False


#: When a retry-pending failure stops being "a later fire will retry" and becomes a page: given
#: the job and the failed run's start, the instant by which the job's next fire *the same day*
#: should have finished, or None when no later fire is due that day (the failed run was the last).
RetryDeadline = Callable[[str, datetime], datetime | None]


def registry_retry_deadline(registry: JobRegistry) -> RetryDeadline:
    """The production `RetryDeadline`: each job's own cron, read in exchange time.

    What it does: finds the job's first fire strictly after the failed run started; if that fire
    falls on the same IST date, the deadline is that fire plus the job's timeout (the budget the
    retry has to finish in — while it is RUNNING it is not a finished attempt, so the failed run is
    still the newest). Otherwise None.
    What it assumes: the run's `started_at` is its fire time give or take seconds, so "strictly
    after" is one second past it (cron has one-minute resolution).
    What it never does: look at the error text, or treat a job missing from `registry` as pending.
    """

    def deadline(job_name: str, started_at: datetime) -> datetime | None:
        if job_name not in registry.names():
            return None
        job = registry.get(job_name)
        after = started_at.astimezone(IST) + timedelta(seconds=1)
        next_fire: datetime | None = job.trigger(IST).get_next_fire_time(None, after)
        if next_fire is None or next_fire.astimezone(IST).date() != after.date():
            return None
        return next_fire + job.timeout

    return deadline


#: The one `job_run.state` that means "the job raised" (`runner._execute`).
_RAISED: Final = "FAILED"


def failed_job_conditions(
    attempts: Iterable[FinishedAttempt],
    *,
    now: datetime | None = None,
    retry_deadline: RetryDeadline | None = None,
    already_open: Collection[str] = (),
) -> list[AlertCondition]:
    """One CRITICAL condition per job whose newest finished attempt was FAILED.

    A job that fails three nights running without a success between is one condition: the key is
    the job, not the run, so the onset pages and the repeats do not.

    The one exception is a retry-pending failure (`FinishedAttempt.retry_pending`) while a later
    fire of the same job is still due today — `now` before `retry_deadline`'s answer. That is not
    an onset: `tri_evening`'s 19:50 fire fails most evenings because the TRI publishes near 20:47,
    and paging it would page every weekday. It does not *resolve* anything either: if the key is
    `already_open` (an earlier, real failure paged), the condition is kept so the open alert stays
    open. A retry-pending failure of the day's last fire, one whose retry did not finish by the
    deadline, or any failure when `retry_deadline` or `now` is not given, pages as before.

    What clears it: a newer SUCCEEDED attempt — or a newer TIMED_OUT one. TIMED_OUT is the runner's
    "returned normally but overran its budget": the job did *not* raise, so the condition this
    trigger owns ("a scheduled job that raised") is over. The overrun itself is `/status/jobs`'
    FAILING state and is not paged here; paging it would turn every slow EOD night into a page.
    """
    conditions = []
    for attempt in attempts:
        if attempt.state != _RAISED:
            continue
        key = f"job:{attempt.job_name}:failed"
        if key not in already_open and _retry_still_due(attempt, now, retry_deadline):
            log.info(
                "alert_trigger.retry_pending",
                job=attempt.job_name,
                started_at=attempt.started_at.isoformat(),
            )
            continue
        error = "none recorded" if attempt.error is None else redact(attempt.error)
        conditions.append(
            AlertCondition(
                trigger=Trigger.JOB_FAILED,
                dedup_key=key,
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


def _retry_still_due(
    attempt: FinishedAttempt, now: datetime | None, retry_deadline: RetryDeadline | None
) -> bool:
    """True only for a retry-pending failure whose same-day retry can still land it."""
    if not attempt.retry_pending or now is None or retry_deadline is None:
        return False
    deadline = retry_deadline(attempt.job_name, attempt.started_at)
    return deadline is not None and now < deadline


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
#: Which outcomes are failures is decided in Python (`failed_job_conditions`), where a unit test
#: can see it.
_LAST_ATTEMPTS_SQL: Final = """
    SELECT DISTINCT ON (job_name) job_name, state, started_at, error, retry_pending
    FROM job_run
    WHERE state NOT IN ('RUNNING', 'SKIPPED_LOCKED') AND job_name = ANY(%s)
    ORDER BY job_name, started_at DESC
"""


def read_red_quality_checks(conn: Connection) -> list[OpenQualityCheck]:
    """Every quality check with open ERROR flags, by name."""
    return [
        OpenQualityCheck(
            check_name=str(row[0]), open_errors=int(row[1]), first_date=row[2], last_date=row[3]
        )
        for row in conn.execute(_RED_QUALITY_SQL).fetchall()
    ]


def read_last_attempts(conn: Connection, job_names: Sequence[str]) -> list[FinishedAttempt]:
    """Each registered job's newest finished attempt.

    Only `job_names` — the registry — is asked about: a job removed from the registry whose last
    run failed would otherwise hold an alert open forever with nothing able to clear it.
    """
    return [
        FinishedAttempt(
            job_name=str(row[0]),
            state=str(row[1]),
            started_at=row[2],
            error=None if row[3] is None else str(row[3]),
            retry_pending=bool(row[4]),
        )
        for row in conn.execute(_LAST_ATTEMPTS_SQL, (list(job_names),)).fetchall()
    ]


# ── the ledger and the diff ──────────────────────────────────────────────────────────────────


class ConditionLedger:
    """`alert_condition` (0013): which conditions are open, i.e. have already paged.

    What it does: reads the open keys of a trigger, opens/reopens a key at onset, stamps
    `last_seen_at` on a repeat, and closes a key on resolution.
    What it assumes: an autocommit connection. Each onset or resolution runs inside its own
    `transaction()`, so the ledger write and the send stand or fall together.
    What it never does: delete. A resolved row stays as the record of its last episode.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def transaction(self) -> AbstractContextManager[object]:
        """A transaction that rolls the ledger write back if the block raises."""
        return self._conn.transaction()

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


class _NotSentError(Exception):
    """Raised inside a ledger transaction to roll back a write whose alert did not go out."""

    def __init__(self, outcome: AlertOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


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

    What it does: for every condition with no open row, writes the open row and sends the onset
    alert in one transaction; touches the ones already open; for every open row no longer present,
    closes it and sends one INFO resolution, again in one transaction.
    What it assumes: `conditions` is the *complete* current set for `trigger` — a partial set would
    resolve the missing ones. A trigger that could not be evaluated must not reach here.
    What it never does: record an onset or a resolution that did not go out. Write first, send
    second, inside one transaction: a send that raises — or returns anything but SENT, e.g. the
    alerter's own window suppressed it — rolls the write back, so the next tick tries again; a
    ledger write that raises happens before the send, so a broken ledger costs a missing page (and
    a FAILED run, which `/status/jobs` shows), never a page repeated every tick.
    Ledger *reads* that raise propagate; `tick` isolates them per trigger.
    """
    open_rows = ledger.open_keys(trigger)
    current = {condition.dedup_key: condition for condition in conditions}

    repeats = [key for key in current if key in open_rows]
    ledger.touch(repeats, at)
    report.still_open.extend(repeats)

    for key, condition in current.items():
        if key in open_rows:
            continue
        if _write_then_send(
            partial(ledger.open, condition, at),
            partial(alerter.send, condition.severity, condition.title, condition.body, key),
            ledger=ledger,
            key=key,
            what="onset alert",
            report=report,
        ):
            report.opened.append(key)
            log.warning("alert_trigger.onset", trigger=trigger.value, dedup_key=key)

    for key, title in open_rows.items():
        if key in current:
            continue
        if _write_then_send(
            partial(ledger.resolve, key, at),
            partial(
                alerter.send,
                Severity.INFO,
                f"resolved: {title}",
                f"Cleared at {at.isoformat()}: {title}",
                f"{key}:resolved",
            ),
            ledger=ledger,
            key=key,
            what="resolution",
            report=report,
        ):
            report.resolved.append(key)
            log.info("alert_trigger.resolved", trigger=trigger.value, dedup_key=key)


def _write_then_send(
    write: Callable[[], None],
    send: Callable[[], AlertOutcome],
    *,
    ledger: ConditionLedger,
    key: str,
    what: str,
    report: TickReport,
) -> bool:
    """Run `write` then `send` in one ledger transaction; True only if both happened and SENT."""
    try:
        with ledger.transaction():
            write()
            outcome = send()
            if outcome is not AlertOutcome.SENT:
                raise _NotSentError(outcome)
    except _NotSentError as not_sent:
        report.errors.append(f"{key}: {what} {not_sent.outcome.value}; will retry next tick")
        log.warning("alert_trigger.not_sent", dedup_key=key, outcome=not_sent.outcome.value)
        return False
    except Exception as error:
        report.errors.append(f"{key}: {what} not delivered: {type(error).__name__}")
        log.error("alert_trigger.send_failed", dedup_key=key, error_type=type(error).__name__)
        return False
    return True


# ── the tick ─────────────────────────────────────────────────────────────────────────────────

#: One trigger and the thunk that reads its current conditions. A thunk, so that everything the
#: read needs — the holiday file included — is loaded inside the trigger's own isolation.
TriggerEvaluator = tuple[Trigger, Callable[[], list[AlertCondition]]]


def tick(
    evaluators: Sequence[TriggerEvaluator],
    *,
    ledger: ConditionLedger,
    alerter: Alerter,
    at: datetime,
) -> TickReport:
    """Evaluate and reconcile each trigger in turn, each one isolated from the others' failures.

    What it does: for each trigger, reads the current conditions and `reconcile`s them. Anything
    that raises — the read, a ledger read, a malformed holiday file — is collected into
    `report.errors` against that trigger, and the next trigger still runs.
    What it never does: resolve the conditions of a trigger it could not evaluate. Absence of
    evidence is not a cleared alarm.
    """
    report = TickReport()
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
        try:
            reconcile(trigger, conditions, ledger=ledger, alerter=alerter, at=at, report=report)
        except Exception as error:
            report.errors.append(f"{trigger.value}: not reconciled: {type(error).__name__}")
            log.error(
                "alert_trigger.reconcile_failed",
                trigger=trigger.value,
                error_type=type(error).__name__,
                error=redact(str(error)),
            )
    log.info(
        "alert_trigger.tick",
        opened=report.opened,
        still_open=len(report.still_open),
        resolved=report.resolved,
        errors=len(report.errors),
    )
    return report


def run_failure_alerts(
    conn: Connection,
    *,
    settings: Settings,
    clock: Clock,
    alerter: Alerter,
    job_names: Sequence[str],
    calendar_loader: Callable[[], TradingCalendar] | None = None,
    retry_deadline: RetryDeadline | None = None,
) -> TickReport:
    """Evaluate all four triggers once against the database and page what changed.

    What it does: builds the four evaluators and hands them to `tick`.
    - The expiry trigger calls `calendar_loader` (default `calendar.load`: a fresh read of the
      YAML, not the per-process cache, so an appended year is seen without a restart), and
      calls it *inside* its own evaluator: a malformed holiday file fails that one trigger, not
      the other three.
    - The streak trigger builds its `SyncStateStore` on the cached `trading_calendar()` inside its
      evaluator, for the same reason.
    - The failed-job trigger holds a retry-pending failure while `retry_deadline` says a later fire
      the same day is still due (None: every failure pages; production passes the registry's).
    What it assumes: `conn` is autocommit and migrated through 0013; `clock` is the run's (B10).
    What it never does: raise for a failed trigger (`run_failure_alerts_job` turns errors into a
    FAILED run), or read the wall clock.
    """
    today = clock.today()
    threshold = settings.alert_failure_streak_threshold
    lead_days = settings.alert_calendar_lead_days

    def streaks() -> list[AlertCondition]:
        store = SyncStateStore(conn, clock=clock, calendar=trading_calendar())
        return failed_streak_conditions(store.source_statuses(), threshold=threshold)

    def failed_jobs() -> list[AlertCondition]:
        return failed_job_conditions(
            read_last_attempts(conn, job_names),
            now=clock.now(),
            retry_deadline=retry_deadline,
            already_open=ConditionLedger(conn).open_keys(Trigger.JOB_FAILED),
        )

    def expiry() -> list[AlertCondition]:
        coverage_end = (
            load_calendar if calendar_loader is None else calendar_loader
        )().coverage_end
        return calendar_expiry_conditions(coverage_end, today=today, lead_days=lead_days)

    evaluators: list[TriggerEvaluator] = [
        (Trigger.INGEST_FAILED_STREAK, streaks),
        (Trigger.QUALITY_RED, lambda: quality_red_conditions(read_red_quality_checks(conn))),
        (Trigger.CALENDAR_EXPIRY, expiry),
        (Trigger.JOB_FAILED, failed_jobs),
    ]
    return tick(evaluators, ledger=ConditionLedger(conn), alerter=alerter, at=clock.now())


def run_failure_alerts_job(context: JobContext) -> None:
    """The `failure_alerts` scheduler job body: one tick against the production wiring.

    What it does: builds the configured alerter and runs `run_failure_alerts` over the production
    registry's job names.
    What it assumes: a fresh alerter per tick, so the alerter's in-process dedup window never
    suppresses anything here — dedup is the `alert_condition` ledger's job. Were it ever to
    suppress, `reconcile` rolls the onset back rather than recording a page that was not sent.
    What it never does: report a green run when a trigger could not be evaluated or an alert could
    not be delivered; it raises `TriggerEvaluationError`, which the runner records FAILED.
    """
    from dataplatform.scheduler.registry import default_registry

    alerter = build_alerter(context.settings, clock=context.clock)
    registry = default_registry()
    with connection(context.settings, autocommit=True) as conn:
        report = run_failure_alerts(
            conn,
            settings=context.settings,
            clock=context.clock,
            alerter=alerter,
            job_names=registry.names(),
            retry_deadline=registry_retry_deadline(registry),
        )
    if not report.ok:
        raise TriggerEvaluationError("; ".join(report.errors))
