"""`python -m dataplatform.scheduler` — the scheduler process, and the manual trigger.

Three subcommands, all of which go through the same `SchedulerRunner`, so a job an operator or an
agent fires by hand is locked, recorded and heartbeated exactly like one the cron fired:

    uv run python -m dataplatform.scheduler list
    uv run python -m dataplatform.scheduler run-once eod_pipeline
    uv run python -m dataplatform.scheduler run

Exit codes for `run-once` are the point of it being a command rather than a function: 0 the job
succeeded, 1 it failed or overran its budget, 2 the name is not registered, 3 another process was
already running it. A caller — a systemd unit, a retry wrapper, a future agent tool — can tell
"the job broke" from "the job was already running" without parsing the log.

`run` and `run-once` both refuse to start — exit 5, before any job is built — while a migration in
`dataplatform/store/migrations` is not applied, or an applied one was edited (M15.4). Every job is
written against the schema in this checkout; a scheduler that started on an older database would
fail one job at a time, at each job's own hour, with an `UndefinedColumn` nobody connects to a
skipped `make migrate`. A database *ahead* of the checkout (a newer checkout migrated it) is not a
refusal: migrations are additive, so this code's tables are all there — it starts, and pages a
warning to deploy the newer checkout.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import psycopg

from dataplatform.alerts import Severity, build_alerter
from dataplatform.config import Settings
from dataplatform.logging import configure_logging, get_logger
from dataplatform.scheduler.registry import JobNotRegisteredError
from dataplatform.scheduler.runner import JobState, SchedulerRunner, build_scheduler
from dataplatform.store.migrate import MigrationDriftError, SchemaStatus, schema_status

#: `run-once` exit codes, by outcome. SKIPPED_LOCKED is deliberately not a failure: the job is
#: running, which is what the caller wanted, just not in this process.
_EXIT_CODES = {
    JobState.SUCCEEDED: 0,
    JobState.FAILED: 1,
    JobState.TIMED_OUT: 1,
    JobState.SKIPPED_LOCKED: 3,
    JobState.RUNNING: 1,
}

_UNKNOWN_JOB = 2

#: The schema is not what this checkout's jobs were written against. Distinct from every job
#: outcome, and named in `ops/systemd/scheduler.service`'s `RestartPreventExitStatus` so systemd
#: stops rather than restarting into the same refusal every 30 seconds.
EXIT_MIGRATIONS_PENDING = 5

#: The database could not be reached for the schema check even after `_CONNECT_BACKOFF`. Exit 1,
#: which systemd restarts: an outage, unlike an unmigrated schema, can end by itself.
EXIT_DATABASE_UNREACHABLE = 1

#: Waits between attempts at the startup schema check — 4.6 minutes in all. Long enough to ride out
#: Postgres restarting under the scheduler (a compose restart, a host reboot racing the unit);
#: short enough that a real outage reaches systemd's restart loop and the stale heartbeat soon.
_CONNECT_BACKOFF: tuple[float, ...] = (5, 10, 20, 40, 80, 120)


@dataclass(frozen=True, slots=True)
class SchemaVerdict:
    """What the startup check concluded. `refuse` stops the process; otherwise it may warn."""

    refuse: bool
    severity: Severity
    title: str
    reason: str
    dedup_key: str


log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Kept in a function so the tests can read the commands off it."""
    parser = argparse.ArgumentParser(
        prog="python -m dataplatform.scheduler",
        description="The EOD platform's in-process scheduler (EXECUTION_PLAN.md §8.1).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print the registered jobs and their cron schedules")
    once = commands.add_parser("run-once", help="run one registered job now, then exit")
    once.add_argument("job", help="the registered job name, e.g. eod_pipeline")
    commands.add_parser("run", help="start the scheduler and stay in the foreground")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint. Configures logging first so that even a startup failure is structured."""
    args = build_parser().parse_args(argv)
    configure_logging()
    runner = SchedulerRunner()

    if args.command == "list":
        for job in runner.registry:
            print(f"{job.name}\t{job.cron}\t{job.description}")
        return 0

    try:
        verdict = check_schema(runner.settings)
    except psycopg.OperationalError as error:
        log.error("scheduler.database_unreachable", error=str(error).strip())
        print(f"scheduler: database unreachable for the schema check: {error}", file=sys.stderr)
        return EXIT_DATABASE_UNREACHABLE
    if verdict is not None:
        _page(runner.settings, verdict)
        if verdict.refuse:
            log.error("scheduler.refused", title=verdict.title, reason=verdict.reason)
            print(f"scheduler: refusing to start: {verdict.reason}", file=sys.stderr)
            return EXIT_MIGRATIONS_PENDING
        log.warning("scheduler.schema_warning", title=verdict.title, reason=verdict.reason)

    if args.command == "run-once":
        try:
            run = runner.run_once(args.job)
        except JobNotRegisteredError as error:
            log.error("scheduler.unknown_job", job=args.job, error=str(error))
            print(error, file=sys.stderr)
            return _UNKNOWN_JOB
        print(f"{run.job_name} {run.state.value} run_id={run.run_id}")
        if run.error:
            print(run.error, file=sys.stderr)
        return _EXIT_CODES[run.state]

    return _run_forever(runner)


def check_schema(
    settings: Settings,
    *,
    status: Callable[[Settings], SchemaStatus] = schema_status,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Sequence[float] = _CONNECT_BACKOFF,
) -> SchemaVerdict | None:
    """Whether the scheduler may start against this database; `None` when the schema is current.

    What it does: asks `schema_status` (read-only), retrying a connection failure on `backoff`
    before letting `psycopg.OperationalError` out. Then: an edited applied migration or a pending
    file refuses, each with its own remedy; a database ahead of the checkout warns and starts.
    What it never does: migrate. Applying DDL is an operator's `make migrate`, run deliberately
    after a merge — never a side effect of a restart.
    """
    for attempt, wait in enumerate((*backoff, None)):
        try:
            current = status(settings)
            break
        except MigrationDriftError as error:
            return SchemaVerdict(
                refuse=True,
                severity=Severity.CRITICAL,
                title="scheduler refused to start: an applied migration was edited",
                reason=(
                    f"{error} Restore the file to the content that was applied (git log -p on "
                    "it), put the change in a new numbered migration, then restart the scheduler. "
                    "`make migrate` will refuse until then."
                ),
                dedup_key="scheduler.refused_migration_drift",
            )
        except psycopg.OperationalError as error:
            if wait is None:
                raise
            log.warning(
                "scheduler.schema_check_retry", attempt=attempt + 1, wait_s=wait, error=str(error)
            )
            sleep(wait)

    pending = ", ".join(migration.path.name for migration in current.pending)
    ahead = ", ".join(current.ahead)
    if current.pending and current.ahead:
        return SchemaVerdict(
            refuse=True,
            severity=Severity.CRITICAL,
            title="scheduler refused to start: database and checkout have diverged",
            reason=(
                f"this checkout has unapplied migration(s) {pending}, and the database has "
                f"applied {ahead}, which this checkout lacks. Deploy the checkout that applied "
                f"{ahead} (normally main), then `make migrate` and restart the scheduler."
            ),
            dedup_key="scheduler.refused_schema_diverged",
        )
    if current.pending:
        return SchemaVerdict(
            refuse=True,
            severity=Severity.CRITICAL,
            title="scheduler refused to start: migrations not applied",
            reason=(
                f"{len(current.pending)} migration(s) not applied: {pending}. "
                "Run `make migrate`, then restart the scheduler."
            ),
            dedup_key="scheduler.refused_pending_migrations",
        )
    if current.ahead:
        return SchemaVerdict(
            refuse=False,
            severity=Severity.WARNING,
            title="scheduler started on a database ahead of its checkout",
            reason=(
                f"the database has migration(s) {ahead} that this checkout lacks; migrations are "
                "additive, so the scheduler started. Deploy the newer checkout (git pull on main) "
                "and restart the scheduler so its jobs match the schema."
            ),
            dedup_key="scheduler.schema_ahead",
        )
    return None


def _page(settings: Settings, verdict: SchemaVerdict) -> None:
    """Page the verdict: a stopped scheduler is also the one that would run `failure_alerts`."""
    try:
        build_alerter(settings).send(
            verdict.severity, verdict.title, verdict.reason, dedup_key=verdict.dedup_key
        )
    except Exception as error:  # the verdict itself must still reach stderr and the exit code
        log.error("scheduler.schema_alert_failed", error=str(error))


def _run_forever(runner: SchedulerRunner) -> int:
    """Start the scheduler and block until interrupted.

    Beats once before starting, so `/health` is fresh the moment the process is up rather than one
    tick later — a container that restarts every 30 seconds would otherwise always look healthy
    for the wrong reason.
    """
    scheduler = build_scheduler(runner)
    runner.beat()
    scheduler.start()
    log.info("scheduler.running", jobs=list(runner.registry.names()))
    try:
        threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.stopping")
    finally:
        scheduler.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
