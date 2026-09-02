"""D1: the daily EOD pipeline job (M1.10) — one trading session, fetched to PUBLISHED, archived.

M0.6 registered `eod_pipeline` as a no-op placeholder; this module is the body it now runs. Once a
day, after the NSE close, the scheduler fires `run_eod_pipeline`, which for the latest trading
session drives every daily NSE source down the pipeline the M1 tasks built —
`fetch → L0 → parse → L1 → sync_state` (M1.9's `BackfillRunner`) — runs the D7 gap check (M1.11),
publishes the day's archive bundle (M1.12), and alerts on any source left FAILED.

Three properties make it an operable daily job rather than a one-shot script, each mapped to an
acceptance criterion:

* **One invocation takes a session from PENDING to PUBLISHED.** The pipeline reuses the exact
  `BackfillRunner` the backfill uses (invariant: paper and real share one code path — here, backfill
  and daily share one ingest path), so a single run of the latest session lands its L1 partition and
  moves its `sync_state` row to PUBLISHED for every daily NSE source.

* **Self-heal.** Before today's work, the runner re-attempts every date left FAILED(retryable)
  inside a lookback window. A Friday session that failed to fetch is not lost until someone notices;
  Monday's run picks it up and finishes it. This is what makes the M1 gate's "self-heal on one
  induced failure" true — a 500 on one run becomes a PUBLISHED partition on the next, with no human
  in the loop. The mechanism is `sync_state` itself: a retryable FAILED row transitions back to
  PENDING and is re-driven, a PUBLISHED row is skipped.

* **Idempotent.** Running twice for the same session changes nothing. The runner never re-fetches a
  PUBLISHED date (M1.9), and the archive is published only when the target has no bundle yet, so a
  second run opens no socket, writes no L1, and re-writes no bundle — it is a true no-op.

Offline by construction (B8): the pipeline takes its `Fetcher`, `L0Store`, `SyncStateStore` (via the
connection), alerter and clock by injection, so a test wires a `RecordedTransport` and a scratch
database and never opens a socket. `run_eod_pipeline` is the only place that builds the real,
networked wiring; it is what the scheduler's `eod_pipeline` job calls.

The operator runbook is `ops/runbooks/daily-eod.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dataplatform.alerts import Alerter, Severity, build_alerter
from dataplatform.archives.publisher import PublishReport, publish_bundle
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.ingest.backfill import (
    NSE_BHAVCOPY,
    SOURCE_SETS,
    BackfillReport,
    BackfillRunner,
    SourceSet,
)
from dataplatform.ingest.calendar import (
    CalendarCoverageError,
    TradingCalendar,
    trading_calendar,
)
from dataplatform.ingest.fetcher import Fetcher, build_fetcher
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger, log_context
from dataplatform.quality.gaps import GapReport, GapScanner, LakeL1Presence
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import Connection, connection
from dataplatform.store.l0 import L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "DAILY_NSE_SOURCES",
    "DEFAULT_LOOKBACK",
    "EodPipeline",
    "EodPipelineError",
    "EodReport",
    "SourceOutcome",
    "latest_data_date",
    "run_eod_pipeline",
]

_LOG = get_logger(__name__)

#: The daily NSE source sets this job drives, by `BackfillRunner` source-set name (which is also the
#: `sync_state` source name — see `backfill.FetchRequest.state_source`). Only the cash bhavcopy is
#: wired end-to-end into a source set today (M1.9); delivery, corp-actions and the other daily NSE
#: rows join here as their own source sets land in later milestones. Keeping the list explicit means
#: "all daily NSE sources" is one auditable tuple, not a register scan that would sweep in a source
#: whose parser does not exist yet.
DAILY_NSE_SOURCES: Final[tuple[str, ...]] = (NSE_BHAVCOPY,)

#: How far back the self-heal step looks for a FAILED(retryable) date to re-attempt. A week covers a
#: long weekend plus a couple of missed runs without re-scanning the whole decade every night — the
#: backfill runner (M1.9), not the daily job, is what fills a genuine multi-year hole.
DEFAULT_LOOKBACK: Final = timedelta(days=7)


class EodPipelineError(RuntimeError):
    """The daily run finished with the target session not PUBLISHED for every daily NSE source.

    Raised by `run_eod_pipeline` so the scheduler records the job FAILED (and `run-once` exits
    non-zero) when a source could not be landed — the failure is already alerted and already sits in
    `sync_state` as a retryable FAILED row the next run will self-heal, but the job itself must not
    report success on a day its own session never published.
    """


def latest_data_date(calendar: TradingCalendar, today: date) -> date:
    """The most recent date on or before `today` the exchange should have published a bhavcopy for.

    What it does: walks back from `today` to the nearest session or Muhurat date. On a trading day
    the job runs after the close, so that is `today`; on a Monday it is the previous Friday, and a
    holiday is stepped over the same way.
    What it assumes: the calendar covers the walk — `today` and the session it lands on are inside
    the holiday file's span (C.2). It never guesses past coverage: an uncovered `today` raises
    `CalendarCoverageError` rather than inventing a session, because a fabricated trading date is
    exactly the phantom the calendar exists to prevent.
    """
    if today > calendar.coverage_end:
        raise CalendarCoverageError(
            f"{today.isoformat()} is past the calendar's coverage "
            f"({calendar.coverage_start}..{calendar.coverage_end}); extend the holiday file before "
            "running the daily job for a date it makes no claim about"
        )
    day = today
    while day >= calendar.coverage_start:
        if calendar.classify(day).expects_data:
            return day
        day -= timedelta(days=1)
    raise CalendarCoverageError(
        f"no expected-data date on or before {today.isoformat()} within the calendar's coverage "
        f"({calendar.coverage_start}..{calendar.coverage_end})"
    )


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """What the daily run did for one source: the runner's report, and whether the target landed."""

    source: str
    target_published: bool
    healed: tuple[date, ...]
    report: BackfillReport


@dataclass(slots=True)
class EodReport:
    """Everything one daily run did — the record an operator and a test both read.

    `session_published` is the job's pass/fail: true only when the target session is PUBLISHED for
    every daily NSE source. `healed` lists the older FAILED(retryable) dates this run re-attempted,
    which is the visible evidence the self-heal step ran.
    """

    logical_date: date
    lookback_from: date
    sources: tuple[str, ...]
    outcomes: dict[str, SourceOutcome] = field(default_factory=dict)
    gap_report: GapReport | None = None
    archive: PublishReport | None = None
    alerts_sent: int = 0

    @property
    def session_published(self) -> bool:
        """True when every daily NSE source has the target session PUBLISHED."""
        return bool(self.outcomes) and all(o.target_published for o in self.outcomes.values())

    @property
    def healed(self) -> tuple[date, ...]:
        """Every older FAILED(retryable) date re-attempted this run, across sources, sorted once."""
        seen: set[date] = set()
        for outcome in self.outcomes.values():
            seen.update(outcome.healed)
        return tuple(sorted(seen))

    @property
    def failed_sources(self) -> tuple[str, ...]:
        """Sources whose target session is not PUBLISHED after this run."""
        return tuple(name for name, o in self.outcomes.items() if not o.target_published)


class EodPipeline:
    """Drives the latest trading session (and any retryable stragglers) to PUBLISHED, then archives.

    What it does: for each daily NSE source, self-heals FAILED(retryable) dates in the lookback
    window and drives the target session through `BackfillRunner`; then runs the gap check, alerts
    on any source left FAILED, and publishes the target's archive bundle when it is newly complete.
    What it assumes: its `Fetcher`, `L0Store` and connection share one injected clock (B10), and the
    connection is *not* autocommit — the runner commits after each session, which is the checkpoint,
    and this class commits once more after the bundle row.
    What it never does: re-fetch a PUBLISHED date, publish a bundle twice, decide the date itself
    (the calendar and the injected clock do), or weaken the 403 hard stop the runner enforces.
    """

    def __init__(
        self,
        *,
        conn: Connection,
        fetcher: Fetcher,
        l0: L0Store,
        alerter: Alerter,
        clock: Clock,
        calendar: TradingCalendar,
        register: SourceRegister,
        archive_root: Path,
        data_root: Path | None = None,
        sources: tuple[str, ...] = DAILY_NSE_SOURCES,
        lookback: timedelta = DEFAULT_LOOKBACK,
    ) -> None:
        self._conn = conn
        self._fetcher = fetcher
        self._l0 = l0
        self._alerter = alerter
        self._clock = clock
        self._calendar = calendar
        self._register = register
        self._archive_root = archive_root
        self._data_root = data_root
        self._sources = sources
        self._lookback = lookback
        self._sync = SyncStateStore(conn, clock=clock, calendar=calendar)

    def run(self) -> EodReport:
        """Run one daily EOD cycle for the latest trading session and return what it did.

        Never raises for a *source* failure — that is recorded FAILED in `sync_state`, alerted, and
        left for the next run to self-heal; the caller (`run_eod_pipeline`) reads
        `EodReport.session_published` and raises `EodPipelineError` so the job is recorded FAILED.
        Does raise if the archive of a genuinely complete session cannot be built (fail loud), and
        for a `today` outside the calendar's coverage.
        """
        today = self._clock.today()
        target = latest_data_date(self._calendar, today)
        lookback_from = target - self._lookback
        report = EodReport(logical_date=target, lookback_from=lookback_from, sources=self._sources)

        with log_context(job="eod_pipeline", trading_date=target.isoformat()):
            _LOG.info(
                "eod.start",
                trading_date=target.isoformat(),
                today=today.isoformat(),
                lookback_from=lookback_from.isoformat(),
                sources=list(self._sources),
            )
            for name in self._sources:
                report.outcomes[name] = self._run_source(
                    SOURCE_SETS[name], target=target, lookback_from=lookback_from
                )

            report.gap_report = self._gap_check(lookback_from, target)
            report.alerts_sent = self._emit_alerts(report)

            if report.session_published:
                report.archive = self._publish_archive(target)

            _LOG.info(
                "eod.done",
                trading_date=target.isoformat(),
                session_published=report.session_published,
                healed=[d.isoformat() for d in report.healed],
                failed_sources=list(report.failed_sources),
                archive_published=report.archive is not None,
                alerts_sent=report.alerts_sent,
            )
            return report

    # ── per source ───────────────────────────────────────────────────────────────────────────

    def _run_source(
        self, source_set: SourceSet, *, target: date, lookback_from: date
    ) -> SourceOutcome:
        """Self-heal the source's retryable stragglers, then drive the target, then report on it."""
        source = source_set.name
        healed = self._failed_retryable(source, lookback_from, target)
        dates = sorted(set(healed) | {target})
        plan = [source_set.build_request(day, self._register) for day in dates]

        runner = BackfillRunner(
            source_set,
            fetcher=self._fetcher,
            l0=self._l0,
            sync=self._sync,
            commit=self._conn.commit,
        )
        backfill_report = runner.run(plan)

        record = self._sync.get(source, target)
        target_published = record is not None and record.state is SyncState.PUBLISHED
        _LOG.info(
            "eod.source_done",
            source=source,
            target=target.isoformat(),
            target_published=target_published,
            healed=[d.isoformat() for d in healed],
            published=backfill_report.published,
            skipped_published=backfill_report.skipped_published,
            failed=backfill_report.failed,
        )
        return SourceOutcome(
            source=source,
            target_published=target_published,
            healed=tuple(healed),
            report=backfill_report,
        )

    def _failed_retryable(self, source: str, from_date: date, to_date: date) -> list[date]:
        """Dates for `source` left FAILED(retryable) in the window — the self-heal work list.

        Excludes the target itself: the target is always in the plan, so listing it here would
        double it. A non-retryable FAILED date is deliberately left alone — re-driving a date the
        source will not serve is the hot loop the `retryable` flag exists to prevent (M1.3).
        """
        rows = self._sync.rows_in_range(from_date, to_date, sources=[source])
        return [
            row.logical_date
            for row in rows
            if row.state is SyncState.FAILED and row.retryable and row.logical_date != to_date
        ]

    # ── gap check, alerts, archive ─────────────────────────────────────────────────────────────

    def _gap_check(self, from_date: date, to_date: date) -> GapReport:
        """The D7 gap report over the lookback window (M1.11) — observability, never a job failure.

        Enumerates every unexplained absence in the window so a bring-up backlog (dates never
        backfilled) is visible and alerted, but does not fail the daily run on it: filling a genuine
        multi-year hole is the backfill runner's job, and coupling the nightly job's success to it
        would wedge the platform on the very night it most needs to keep landing today's data.
        """
        scanner = GapScanner(
            self._conn,
            calendar=self._calendar,
            l1_presence=LakeL1Presence(self._data_root),
        )
        report = scanner.report(from_date, to_date, sources=self._sources)
        _LOG.info(
            "eod.gap_check",
            summary=report.summary(),
            fully_explained=report.fully_explained,
            unexplained=len(report.unexplained),
        )
        return report

    def _emit_alerts(self, report: EodReport) -> int:
        """Alert on every source left FAILED and on an unexplained gap; return how many were sent.

        Dedup keys are stable across repeats of the same problem (§8.1 alerting): a source that has
        been failing for days is one piece of news per source per date, not one per run.
        """
        sent = 0
        for name in report.failed_sources:
            outcome = report.outcomes[name]
            first = outcome.report.failures[0] if outcome.report.failures else None
            detail = (
                f"{first[0].isoformat()}: {first[1]}" if first else "no failure detail recorded"
            )
            result = self._alerter.send(
                Severity.CRITICAL,
                f"EOD pipeline: {name} not PUBLISHED for {report.logical_date.isoformat()}",
                f"The daily EOD run left {name} FAILED for {report.logical_date.isoformat()}. "
                f"It will be re-attempted (self-heal) on the next run. First failure — {detail}.",
                f"eod:{name}:{report.logical_date.isoformat()}:FAILED",
            )
            sent += int(result.value == "sent")

        gaps = report.gap_report
        if gaps is not None and not gaps.fully_explained:
            result = self._alerter.send(
                Severity.WARNING,
                f"EOD pipeline: {len(gaps.unexplained)} unexplained gap(s) "
                f"through {report.logical_date.isoformat()}",
                gaps.summary(),
                f"eod:gaps:{report.logical_date.isoformat()}",
            )
            sent += int(result.value == "sent")
        return sent

    def _publish_archive(self, target: date) -> PublishReport | None:
        """Publish the target's archive bundle (M1.12), unless it already has one.

        Idempotent: L0 is immutable (invariant #1), so a PUBLISHED date's bundle is byte-identical
        however often it is built — the only moving field is `published_at`. So a bundle is built
        exactly once, the first run that completes the session, and a no-op second run skips it,
        which is what makes the whole daily run a true no-op the second time.
        """
        if self._bundle_exists(target):
            _LOG.info(
                "eod.archive_skipped", trading_date=target.isoformat(), reason="already built"
            )
            return None
        report = publish_bundle(
            self._conn,
            self._l0,
            target,
            clock=self._clock,
            archive_root=self._archive_root,
            data_root=self._data_root,
        )
        self._conn.commit()
        _LOG.info(
            "eod.archive_published",
            trading_date=target.isoformat(),
            bundle_path=report.bundle_path,
            manifest_sha256=report.manifest_sha256,
            file_count=report.file_count,
        )
        return report

    def _bundle_exists(self, target: date) -> bool:
        """Whether an `archive_bundle` row already exists for the date."""
        row = self._conn.execute(
            "SELECT 1 FROM archive_bundle WHERE logical_date = %s", (target,)
        ).fetchone()
        return row is not None


def run_eod_pipeline(context: JobContext) -> None:
    """The scheduler's `eod_pipeline` job body — build the real wiring and run one daily cycle.

    What it does: from the job's injected clock and settings (B10), builds the networked fetcher,
    the lake stores, the configured alert channel and a database connection, runs `EodPipeline`, and
    raises `EodPipelineError` if the target session did not reach PUBLISHED for every daily NSE
    source — so the run is recorded FAILED and `run-once` exits non-zero.
    What it assumes: the database is migrated and reachable, and the archive lake is the same lake
    the status API serves downloads from (`Settings.data_root`), so a published bundle is
    immediately downloadable.
    What it never does: catch a source failure into a green run. The failure is alerted and left as
    a retryable FAILED row for the next run to self-heal, but the job reports the truth.
    """
    settings: Settings = context.settings
    clock = context.clock
    calendar = trading_calendar()
    register = load_register()

    fetcher = build_fetcher(clock=clock, settings=settings, register=register)
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    alerter = build_alerter(settings, clock=clock)

    with connection(settings) as conn:
        pipeline = EodPipeline(
            conn=conn,
            fetcher=fetcher,
            l0=l0,
            alerter=alerter,
            clock=clock,
            calendar=calendar,
            register=register,
            archive_root=settings.data_root,
            data_root=settings.data_root,
        )
        report = pipeline.run()

    if not report.session_published:
        raise EodPipelineError(
            f"EOD run for {report.logical_date.isoformat()} left "
            f"{', '.join(report.failed_sources)} not PUBLISHED; recorded FAILED(retryable) for "
            "the next run to self-heal. See the alert and /status/sources."
        )
