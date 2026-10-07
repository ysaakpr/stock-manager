"""M14.3: the nightly fundamentals forward job — the integrated feed, watermark to yesterday.

`fundamentals_backfill` is a campaign: an operator picks `--from`/`--to`, a human gives the go (B1),
and the PIT store is exactly as current as the last time somebody ran it. On 2026-10-07 that was
2026-10-05, a week before the September-quarter results season opens. This module is the same
runner pointed at a window the lake itself names, on a schedule, so the store keeps up on its own.

What one run does, and why each step is shaped the way it is:

* **The window starts at the watermark, read from `sync_state`.** The integrated feed checkpoints
  each page as `nse_integrated_filing_index/<window end>/p<NN>` on the window's start date. A window
  counts as *done* only when every one of its `DEFAULT_PAGES_PER_MONTH` pages is `PUBLISHED`; the
  watermark is the end of the contiguous run of done windows from the feed's first month. So a
  window with one failed page does not move the watermark, and the next night re-plans it — under a
  new key, because its end has moved, which is what makes it re-fetch the live index instead of
  re-reading a frozen page (runbook: "Picking up new filings").
* **It ends yesterday, not today.** The feed's date filter is the dissemination date, and results
  are disseminated into the night. A window ending today, run at 02:00, would be marked done with
  the day barely begun and the next run would start tomorrow — every filing broadcast after the
  fetch would be skipped for good. Yesterday is a complete day at 02:00.
* **It is bounded.** At most `MAX_NEW_DAYS` days past the watermark per run (peak season measured at
  840 filings a day, ~2.5 s each), so a week-long outage is caught up over a few nights inside the
  job's budget rather than in one run that overlaps the morning jobs.
* **A filing that failed recently is retried.** The runner skips only `PUBLISHED`, so re-planning a
  window that covers a retryable `FAILED` filing's date retries it — from L0 when the document was
  already fetched, so a parse refusal costs an index page, not a document. Only the last
  `RETRY_LOOKBACK_DAYS` are pulled back in, so a permanent refusal costs a week of index pages and
  then stays on `/status/sources` for a human.
* **The universe is the last quarter's traded names**, not the window's. A one-day window on a
  Sunday has no `prices_raw` partition at all, and filtering against it would skip every filing as
  out of universe — silently, which is the one thing this job must not do.

The lease is taken before the database is touched and is refused, never queued: another driver on
`www.nseindia.com` or `nsearchives.nseindia.com` raises `HostBusyError`, the scheduler records the
run FAILED with the holder named, `/status/jobs` shows it, `failure_alerts` pages it, and the
watermark has not moved, so the next night owes the same days. A run whose window did not complete
— an index page failed, or a 403 spike parked it — raises for the same reason.

Offline by construction except `_run_window`, which builds the real wiring; the planner and the
watermark are pure, and the job takes its state reader and window runner by injection so a test
needs neither a database nor a socket. Time is the job's injected clock (B10). No money is handled
here; the facts it lands are `Decimal` in the XBRL models.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.daily_snapshot import LakeRootMismatchError
from dataplatform.ingest.fetcher import leased_fetcher
from dataplatform.ingest.fundamentals_backfill import (
    LEASED_HOSTS,
    FundamentalsBackfillReport,
    FundamentalsBackfillRunner,
    build_integrated_units,
    isins_in_price_window,
    resolve_universe,
    symbols_accepted_on,
)
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.xbrl import integrated, parser
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

if TYPE_CHECKING:  # a runtime import would cycle through the registry
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "COMMAND",
    "FEED_START",
    "MAX_NEW_DAYS",
    "RETRY_LOOKBACK_DAYS",
    "UNIVERSE_LOOKBACK_DAYS",
    "ForwardState",
    "ForwardWindow",
    "FundamentalsForwardError",
    "IndexPageRow",
    "NoWatermarkError",
    "integrated_watermark",
    "plan_forward_window",
    "run_fundamentals_forward",
    "run_fundamentals_forward_job",
]

_LOG = get_logger(__name__)

#: The lease holder's `command`, so a refused campaign names this job as the one holding the host.
COMMAND: Final = "fundamentals_forward"

#: The first month the integrated-feed campaign planned (runbook: `--from 2025-03-01`). The
#: watermark is the end of the contiguous run of done windows from here, so a hole anywhere in the
#: feed's history holds the watermark at the hole rather than being jumped over.
FEED_START: Final = date(2025, 3, 1)

#: New days past the watermark one run may take on. Peak season measured 840 filings on one day
#: (2026-05-29); three such days at the fetcher's ~2.5 s spacing is under two hours, inside the
#: job's budget. A normal night owes one day.
MAX_NEW_DAYS: Final = 3

#: How far back a retryable `FAILED` filing pulls the window's start. A week: long enough to ride
#: out a transient archive failure, short enough that a permanent parse refusal stops costing index
#: requests after seven nights and is left on `/status/sources` for a human.
RETRY_LOOKBACK_DAYS: Final = 7

#: The price window the ingest universe is drawn from, ending yesterday. A quarter, because a
#: company files once a quarter and one that has not traded in a whole quarter is suspended; the
#: campaign's own universe is its window's traded names, which a one-day window cannot supply.
UNIVERSE_LOOKBACK_DAYS: Final = 92


class FundamentalsForwardError(RuntimeError):
    """The forward window did not complete; the watermark has not moved and the next run owes it."""


class NoWatermarkError(FundamentalsForwardError):
    """No integrated-feed window is done at all — the campaign, not this job, starts the store."""


@dataclass(frozen=True, slots=True)
class IndexPageRow:
    """One `sync_state` row of the integrated index: `unit` is `<window end>/p<NN>`."""

    from_date: date
    to_date: date
    page: int
    state: SyncState

    @classmethod
    def of(cls, logical_date: date, unit: str, state: str) -> IndexPageRow:
        """Parse one row. Raises `ValueError` naming the unit for a key this feed never writes."""
        end, sep, page = unit.partition("/")
        if not sep or not page.startswith("p") or not page[1:].isdigit():
            raise ValueError(
                f"{integrated.SOURCE_ID} unit {unit!r} is not '<window end>/p<NN>'; the watermark "
                "cannot be read past a row it does not understand"
            )
        return cls(
            from_date=logical_date,
            to_date=date.fromisoformat(end),
            page=int(page[1:]),
            state=SyncState(state),
        )


@dataclass(frozen=True, slots=True)
class ForwardState:
    """What the planner needs from `sync_state`: the watermark and the oldest retry still owed."""

    watermark: date | None
    retry_from: date | None = None


@dataclass(frozen=True, slots=True)
class ForwardWindow:
    """One run's discovery window over the integrated feed, and why it starts where it does."""

    from_date: date
    to_date: date
    watermark: date
    retry_from: date | None

    @property
    def new_days(self) -> int:
        """Days past the watermark this window takes on (0 for a retry-only window)."""
        return max((self.to_date - self.watermark).days, 0)


def integrated_watermark(
    rows: Iterable[IndexPageRow],
    *,
    anchor: date = FEED_START,
    pages: int = integrated.DEFAULT_PAGES_PER_MONTH,
) -> date | None:
    """The last day the integrated index is known complete through, or `None` if none is.

    What it does: groups the rows into windows by `(from, to)`; a window is done when pages
    `1..pages` are all present and all `PUBLISHED`. The watermark is the end of the contiguous
    union of done windows starting at `anchor`.
    What it assumes: every window was planned at `pages` pages per month, which is the only plan the
    runner makes (`build_integrated_units` takes the default; the CLI has no flag for it).
    What it never does: count a window with a failed, unfinished or missing page as done, or jump a
    hole — a done window that starts after a gap does not move the watermark past the gap.
    """
    by_window: dict[tuple[date, date], dict[int, SyncState]] = {}
    for row in rows:
        by_window.setdefault((row.from_date, row.to_date), {})[row.page] = row.state
    wanted = set(range(1, pages + 1))
    done = sorted(
        window
        for window, states in by_window.items()
        if wanted <= set(states) and all(states[p] is SyncState.PUBLISHED for p in wanted)
    )
    cursor = anchor - timedelta(days=1)
    for start, end in done:
        if start <= cursor + timedelta(days=1):
            cursor = max(cursor, end)
    return None if cursor < anchor else cursor


def plan_forward_window(
    state: ForwardState,
    *,
    through: date,
    max_new_days: int = MAX_NEW_DAYS,
) -> ForwardWindow | None:
    """The window one run fetches, or `None` when nothing is owed.

    What it does: starts the day after the watermark — or earlier, at `state.retry_from`, when a
    recent filing is owed a retry — and ends at `through` or `max_new_days` past the watermark,
    whichever is sooner.
    What it assumes: `through` is the last complete dissemination day (yesterday, at the job's
    hour), and `retry_from` was already limited to the retry lookback by the caller.
    What it never does: plan from nowhere. With no watermark it raises `NoWatermarkError`: an empty
    store is the B1 campaign's to fill, not a nightly job's.
    """
    if max_new_days < 1:
        raise ValueError(f"max_new_days must be >= 1, got {max_new_days}")
    if state.watermark is None:
        raise NoWatermarkError(
            f"no {integrated.SOURCE_ID} window is complete in sync_state; the forward job extends "
            "the store and does not start it — run the integrated campaign first "
            "(ops/runbooks/fundamentals_backfill.md)"
        )
    new_from = state.watermark + timedelta(days=1)
    to_date = min(through, state.watermark + timedelta(days=max_new_days))
    from_date = new_from if state.retry_from is None else min(new_from, state.retry_from)
    if to_date < new_from:
        # No new day is owed; a retry still re-covers up to the watermark (or `through`).
        to_date = min(through, state.watermark)
    if from_date > to_date:
        return None
    return ForwardWindow(
        from_date=from_date,
        to_date=to_date,
        watermark=state.watermark,
        retry_from=state.retry_from,
    )


# ── reading the state ────────────────────────────────────────────────────────────────────────

_INDEX_ROWS_SQL: Final = "SELECT logical_date, unit, state FROM sync_state WHERE source = %s"

_RETRY_FROM_SQL: Final = (
    "SELECT min(logical_date) FROM sync_state "
    "WHERE source = %s AND state = %s AND retryable AND logical_date BETWEEN %s AND %s"
)


def _read_state(settings: Settings, through: date) -> ForwardState:
    """The watermark and the oldest retryable filing failure in the lookback, from Postgres."""
    with connection(settings) as conn:
        rows = conn.execute(_INDEX_ROWS_SQL, (integrated.SOURCE_ID,)).fetchall()
        retry = conn.execute(
            _RETRY_FROM_SQL,
            (
                parser.SOURCE_ID,
                SyncState.FAILED.value,
                through - timedelta(days=RETRY_LOOKBACK_DAYS - 1),
                through,
            ),
        ).fetchone()
    watermark = integrated_watermark(IndexPageRow.of(r[0], str(r[1]), str(r[2])) for r in rows)
    return ForwardState(watermark=watermark, retry_from=None if retry is None else retry[0])


# ── running one window ───────────────────────────────────────────────────────────────────────


def _run_window(
    settings: Settings, clock: Clock, window: ForwardWindow
) -> FundamentalsBackfillReport:
    """Drive `window` through the campaign's runner with the real, leased wiring.

    The lease comes first, before any connection is opened, so a host another driver holds refuses
    the run with nothing written. Per-filing commits, exactly as a fetching campaign run: this is
    the daily-forward path the runner keeps its one-filing checkpoint for.
    """
    register = load_register()
    calendar = trading_calendar()
    plan = build_integrated_units(window.from_date, window.to_date, register=register)
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    expected = settings.snapshot_expect_lake_root
    if expected is not None and l0.root.resolve() != expected.resolve():
        raise LakeRootMismatchError(
            f"L0 resolved to {l0.root.resolve()} but the operator declared {expected.resolve()}; "
            "nothing was fetched. Fix the invocation (DATA_ROOT), not this assertion."
        )
    with (
        leased_fetcher(
            LEASED_HOSTS,
            clock=clock,
            command=COMMAND,
            settings=settings,
            register=register,
            l0=l0,
        ) as fetcher,
        connection(settings) as conn,
    ):
        master = IdentityStore(conn, clock=clock).load_master()
        universe = resolve_universe(
            master,
            isins_in_price_window(
                calendar,
                window.to_date - timedelta(days=UNIVERSE_LOOKBACK_DAYS),
                window.to_date,
                data_root=settings.data_root,
            ),
        )

        def resolve_isin(symbol: str, on: date) -> str | None:
            # An ambiguous symbol is unresolved, not an identity (invariant #2); the runner counts
            # and names it on the report.
            try:
                return master.try_resolve(symbol, on)
            except Exception:
                return None

        runner = FundamentalsBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            universe=universe,
            commit=conn.commit,
            data_root=settings.data_root,
            symbol_history=lambda isin, on: symbols_accepted_on(
                master.windows_for(isin),
                on,
                isin=isin,
                owner_on=lambda symbol: master.try_resolve(symbol, on),
            ),
            resolve_isin=resolve_isin,
        )
        _LOG.info(
            "fundamentals_forward.window_start",
            source=integrated.SOURCE_ID,
            from_date=window.from_date.isoformat(),
            to_date=window.to_date.isoformat(),
            pages=len(plan),
            universe=len(universe),
            state="RUNNING",
        )
        return runner.run(plan)


# ── the job ──────────────────────────────────────────────────────────────────────────────────


def run_fundamentals_forward(
    context: JobContext,
    *,
    read_state: Callable[[Settings, date], ForwardState] = _read_state,
    run_window: Callable[
        [Settings, Clock, ForwardWindow], FundamentalsBackfillReport
    ] = _run_window,
) -> FundamentalsBackfillReport | None:
    """One forward run: plan from the watermark to yesterday, run it, fail loud if it fell short.

    What it does: reads the watermark, plans the bounded window, runs it under both NSE leases, and
    returns the runner's report — or `None` when nothing is owed, having taken no lease.
    What it assumes: the injected clock and settings are the run's (B10), the integrated campaign
    has run at least once, and the database is migrated.
    What it never does: catch `HostBusyError` (a refused lease is a FAILED run naming the holder,
    never a quiet skip), mark a short window done, or fetch beyond yesterday.
    Raises `FundamentalsForwardError` when an index page failed or a 403 spike parked the run.
    """
    settings, clock = context.settings, context.clock
    through = clock.today() - timedelta(days=1)
    state = read_state(settings, through)
    window = plan_forward_window(state, through=through)
    if window is None:
        _LOG.info(
            "fundamentals_forward.nothing_owed",
            source=integrated.SOURCE_ID,
            watermark=None if state.watermark is None else state.watermark.isoformat(),
            through=through.isoformat(),
            state="PUBLISHED",
        )
        return None

    _LOG.info(
        "fundamentals_forward.planned",
        source=integrated.SOURCE_ID,
        watermark=window.watermark.isoformat(),
        retry_from=None if window.retry_from is None else window.retry_from.isoformat(),
        from_date=window.from_date.isoformat(),
        to_date=window.to_date.isoformat(),
        through=through.isoformat(),
        new_days=window.new_days,
        behind_days=(through - window.watermark).days,
    )
    report = run_window(settings, clock, window)
    _LOG.info(
        "fundamentals_forward.done",
        source=integrated.SOURCE_ID,
        from_date=window.from_date.isoformat(),
        to_date=window.to_date.isoformat(),
        index_published=report.index_published,
        index_failed=report.index_failed,
        filings_discovered=report.filings_discovered,
        filings_published=report.filings_published,
        filings_skipped_published=report.filings_skipped_published,
        filings_failed=report.filings_failed,
        skipped_out_of_universe=report.skipped_out_of_universe,
        unresolved_symbols=sorted(report.unresolved_symbols),
        facts_written=report.facts_written,
        state="PARKED" if report.parked else ("DEGRADED" if report.index_failed else "PUBLISHED"),
    )
    if report.parked:
        raise FundamentalsForwardError(
            f"{window.from_date}..{window.to_date}: parked — {report.park_detail}"
        )
    if report.index_failed:
        failed = "; ".join(f"{label}: {message}" for label, message in report.failures[:5])
        raise FundamentalsForwardError(
            f"{window.from_date}..{window.to_date}: {report.index_failed} index page(s) failed, so "
            f"the window is not done and the next run re-plans it — {failed}"
        )
    return report


def run_fundamentals_forward_job(context: JobContext) -> None:
    """Scheduler body for `fundamentals_forward`."""
    run_fundamentals_forward(context)
