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
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence

from dataplatform.logging import configure_logging, get_logger
from dataplatform.scheduler.registry import JobNotRegisteredError
from dataplatform.scheduler.runner import JobState, SchedulerRunner, build_scheduler

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
