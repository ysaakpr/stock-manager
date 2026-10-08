"""M14.3: the nightly fundamentals forward job — the integrated feed, watermark to yesterday.

`fundamentals_backfill` is a campaign: an operator picks `--from`/`--to`, a human gives the go (B1),
and the PIT store is exactly as current as the last time somebody ran it. On 2026-10-07 that was
2026-10-05, a week before the September-quarter results season opens. This module is the same
runner pointed at windows the lake itself names, on a schedule, so the store keeps up on its own.

What one run does, and why each step is shaped the way it is:

* **New days start at the watermark, read from `sync_state`.** The integrated feed checkpoints
  each page as `nse_integrated_filing_index/<window end>/p<NN>` on the window's start date. A window
  is done when every one of its `DEFAULT_PAGES_PER_MONTH` pages is `PUBLISHED`, and it is done only
  *through the day before its pages were fetched*: the feed filters on the dissemination date, so a
  page fetched at 15:00 on D says nothing about what D publishes at 22:00. The watermark is the end
  of the contiguous run of done windows from the feed's first month. A window with a failed or
  missing page, a hole, or a same-day fetch therefore holds it, and the next run re-plans those
  days under a key no existing window has — so the live index is fetched, never a frozen page
  re-read (runbook: "Picking up new filings").
* **They end yesterday, not today**, for the same reason: yesterday is a complete day at 02:00.
* **At most `MAX_NEW_DAYS` new days a run** (peak season measured at 840 filings a day, ~2.5 s
  each), so a long outage is caught up over a few nights rather than in one run past its slot.
* **Recent done windows are re-read, not re-fetched.** Every window that ends in the last
  `REREAD_DAYS` and is fully published is planned again under its *own* key. The runner re-parses
  each page from L0 (no request, no new index row) and drives every entry not yet `PUBLISHED` — a
  retryable failure, or a filing a deadline stop never reached — reusing the document from L0 when
  it is there. Kept apart from the new-days window on purpose: index rows are dated by their
  window's start, and `/status/sources` measures lag from the newest one, so folding a retry into
  the new window would date tonight's row a week back and show the source overdue for a week.
* **The universe is the last quarter's traded names**, not the window's. A one-day window on a
  Sunday has no `prices_raw` partition at all, and filtering against it would skip every filing as
  out of universe — silently, which is the one thing this job must not do.
* **A deadline bounds the run** (`forward_deadline`: 04:45 IST for a night start), checked by the
  runner between units, so the leases are released before the morning jobs need the hosts.

Order of work: a read-only `sync_state` query first — so a run with nothing owed takes no lease and
makes no request — then both NSE host leases, and only then the runner's database connection. A
lease is refused, never queued: another driver on `www.nseindia.com` or `nsearchives.nseindia.com`
raises `HostBusyError`, the scheduler records the run FAILED with the holder named, `/status/jobs`
shows it, `failure_alerts` pages it, and the watermark has not moved, so the next night owes the
same days. A run whose new window did not complete — an index page failed, a 403 spike parked it,
or the deadline stopped it — raises for the same reason.

Offline by construction except `run_forward_plan`, which builds the real wiring; the planner and the
watermark are pure, and the job takes its row reader and plan runner by injection so a test needs
neither a database nor a socket. Time is the job's injected clock (B10). No money is handled here;
the facts it lands are `Decimal` in the XBRL models.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.identity.master import IdentityStore
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.daily_snapshot import LakeRootMismatchError
from dataplatform.ingest.fetcher import Transport, leased_fetcher
from dataplatform.ingest.fundamentals_backfill import (
    LEASED_HOSTS,
    FundamentalsBackfillReport,
    FundamentalsBackfillRunner,
    IndexUnit,
    build_integrated_units,
    isins_in_price_window,
    resolve_universe,
    symbols_accepted_on,
    try_resolve_unambiguous,
)
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.xbrl import integrated
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
    "NIGHT_DEADLINE",
    "REREAD_DAYS",
    "RUN_LIMIT",
    "UNIVERSE_LOOKBACK_DAYS",
    "ForwardPlan",
    "FundamentalsForwardError",
    "IndexPageRow",
    "NoWatermarkError",
    "Window",
    "forward_deadline",
    "integrated_watermark",
    "plan_forward",
    "run_forward_plan",
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
#: run limit. A normal night owes one day.
MAX_NEW_DAYS: Final = 3

#: How far back a done window is re-read from L0 for entries not yet published. A week: long enough
#: to ride out a transient archive failure or two deadline stops, short enough that a permanent
#: parse refusal stops being retried after seven nights and is left on `/status/sources`.
REREAD_DAYS: Final = 7

#: The price window the ingest universe is drawn from, ending yesterday. A quarter, because a
#: company files once a quarter and one that has not traded in a whole quarter is suspended; the
#: campaign's own universe is its window's traded names, which a one-day window cannot supply.
UNIVERSE_LOOKBACK_DAYS: Final = 92

#: A night run stops starting units at 04:45 IST, so it has released both NSE leases before the
#: 06:00 first-Sunday `bse_ca_sweep` (and anything an operator schedules for 05:00) needs them.
NIGHT_DEADLINE: Final = time(4, 45)

#: How long any run may keep starting units, whatever hour it began — a manual `run-once` at 15:00
#: still stops by 17:45, clear of the 18:00 evening window. Also the job's reported budget.
RUN_LIMIT: Final = timedelta(hours=2, minutes=45)


class FundamentalsForwardError(RuntimeError):
    """The new window did not complete; the watermark has not moved and the next run owes it."""


class NoWatermarkError(FundamentalsForwardError):
    """No integrated-feed window is done at all — the campaign, not this job, starts the store."""


@dataclass(frozen=True, slots=True, order=True)
class Window:
    """One discovery window `[from_date, to_date]` — with the page number, a `sync_state` key."""

    from_date: date
    to_date: date


@dataclass(frozen=True, slots=True)
class IndexPageRow:
    """One `sync_state` row of the integrated index: `unit` is `<window end>/p<NN>`.

    `fetched_on` is the exchange-timezone date the row last moved (`updated_at`), which for a
    `PUBLISHED` page is the day it was fetched — the bound on what that page can know.
    """

    from_date: date
    to_date: date
    page: int
    state: SyncState
    fetched_on: date

    @property
    def window(self) -> Window:
        return Window(self.from_date, self.to_date)

    @classmethod
    def of(
        cls, logical_date: date, unit: str, state: str, updated_at: datetime, tz: ZoneInfo
    ) -> IndexPageRow:
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
            fetched_on=updated_at.astimezone(tz).date(),
        )


@dataclass(frozen=True, slots=True)
class ForwardPlan:
    """One run's work: the new-days window (if any) and the done windows to re-read from L0."""

    watermark: date
    through: date
    new: Window | None
    reread: tuple[Window, ...] = ()

    @property
    def new_days(self) -> int:
        """Days past the watermark the new window takes on (0 when there is none)."""
        return 0 if self.new is None else max((self.new.to_date - self.watermark).days, 0)

    @property
    def windows(self) -> tuple[Window, ...]:
        """Every window in run order — the new days first, so fresh filings land before retries."""
        return ((self.new,) if self.new is not None else ()) + self.reread


def _windows(rows: Iterable[IndexPageRow], pages: int) -> tuple[set[Window], dict[Window, date]]:
    """Every window any row names, and the done ones with the earliest day a page was fetched."""
    by_window: dict[Window, list[IndexPageRow]] = {}
    for row in rows:
        by_window.setdefault(row.window, []).append(row)
    wanted = set(range(1, pages + 1))
    done: dict[Window, date] = {}
    for window, page_rows in by_window.items():
        states = {row.page: row.state for row in page_rows}
        if wanted <= set(states) and all(states[p] is SyncState.PUBLISHED for p in wanted):
            done[window] = min(row.fetched_on for row in page_rows if row.page in wanted)
    return set(by_window), done


def integrated_watermark(
    rows: Iterable[IndexPageRow],
    *,
    anchor: date = FEED_START,
    pages: int = integrated.DEFAULT_PAGES_PER_MONTH,
) -> date | None:
    """The last day the integrated index is known complete through, or `None` if none is.

    What it does: a window is done when pages `1..pages` are all present and all `PUBLISHED`, and
    it is complete through `min(to_date, fetched_on - 1)` — a page fetched on D cannot know what D
    disseminates after the fetch. The watermark is the end of the contiguous union of those spans
    starting at `anchor`.
    What it assumes: every window was planned at `pages` pages per month, which is the only plan the
    runner makes (`build_integrated_units` takes the default; the CLI has no flag for it).
    What it never does: count a window with a failed, unfinished or missing page as done, count the
    day a page was fetched as complete, or jump a hole — a span that starts after a gap does not
    move the watermark past the gap.
    """
    _, done = _windows(rows, pages)
    spans = sorted(
        (window.from_date, min(window.to_date, fetched - timedelta(days=1)))
        for window, fetched in done.items()
    )
    cursor = anchor - timedelta(days=1)
    for start, end in spans:
        if start <= cursor + timedelta(days=1):
            cursor = max(cursor, end)
    return None if cursor < anchor else cursor


def plan_forward(
    rows: Sequence[IndexPageRow],
    *,
    through: date,
    max_new_days: int = MAX_NEW_DAYS,
    reread_days: int = REREAD_DAYS,
    pages: int = integrated.DEFAULT_PAGES_PER_MONTH,
) -> ForwardPlan | None:
    """What one run does, or `None` when no new day is owed.

    What it does: plans the new days `[watermark + 1, min(through, watermark + max_new_days)]`, and
    — only alongside them — re-reads every done window that ends within `reread_days` of `through`
    under its own key. If an existing window already has the new window's key (a same-day fetch that
    the watermark discounted), the start moves back a day at a time until the key is new, so the
    page is fetched live instead of re-parsed from a frozen payload.
    What it assumes: `through` is the last complete dissemination day (yesterday, at the job's
    hour).
    What it never does: plan from nowhere — with no watermark it raises `NoWatermarkError`, because
    an empty store is the B1 campaign's to fill — or plan anything past `through`. A night with no
    new day plans nothing at all, so it takes no lease; the re-reads ride with the next new day.
    """
    if max_new_days < 1:
        raise ValueError(f"max_new_days must be >= 1, got {max_new_days}")
    watermark = integrated_watermark(rows, pages=pages)
    if watermark is None:
        raise NoWatermarkError(
            f"no {integrated.SOURCE_ID} window is complete in sync_state; the forward job extends "
            "the store and does not start it — run the integrated campaign first "
            "(ops/runbooks/fundamentals_backfill.md)"
        )
    if watermark >= through:
        return None
    existing, done = _windows(rows, pages)
    to_date = min(through, watermark + timedelta(days=max_new_days))
    new = Window(watermark + timedelta(days=1), to_date)
    while new in existing:
        new = Window(new.from_date - timedelta(days=1), to_date)
    horizon = through - timedelta(days=reread_days - 1)
    reread = tuple(
        sorted(w for w in done if w != new and w.to_date >= horizon and w.from_date <= through)
    )
    return ForwardPlan(watermark=watermark, through=through, new=new, reread=reread)


def forward_deadline(start: datetime) -> datetime:
    """When a run that began at `start` stops starting units.

    `start + RUN_LIMIT`, and no later than `NIGHT_DEADLINE` on the same day for a run that began
    before it — so the 02:00 fire (and a late fire inside its one-hour misfire grace) is done with
    both NSE hosts by 04:45, whatever the window holds.
    """
    deadline = start + RUN_LIMIT
    if start.time() < NIGHT_DEADLINE:
        deadline = min(deadline, datetime.combine(start.date(), NIGHT_DEADLINE, start.tzinfo))
    return deadline


# ── reading the state ────────────────────────────────────────────────────────────────────────

_INDEX_ROWS_SQL: Final = (
    "SELECT logical_date, unit, state, updated_at FROM sync_state WHERE source = %s"
)


def _read_rows(settings: Settings) -> list[IndexPageRow]:
    """Every integrated-index page row, read-only. No lease is held while this runs."""
    with connection(settings) as conn:
        rows = conn.execute(_INDEX_ROWS_SQL, (integrated.SOURCE_ID,)).fetchall()
    return [IndexPageRow.of(r[0], str(r[1]), str(r[2]), r[3], settings.tzinfo) for r in rows]


# ── running one plan ─────────────────────────────────────────────────────────────────────────


def _units(plan: ForwardPlan) -> list[IndexUnit]:
    """The runner's units for every window, keyed exactly as the windows' stored pages are."""
    register = load_register()
    return [
        unit
        for window in plan.windows
        for unit in build_integrated_units(window.from_date, window.to_date, register=register)
    ]


def run_forward_plan(
    settings: Settings,
    clock: Clock,
    plan: ForwardPlan,
    *,
    should_stop: Callable[[], bool],
    transport: Transport | None = None,
) -> FundamentalsBackfillReport:
    """Drive `plan` through the campaign's runner with the real wiring, under both NSE leases.

    What it does: takes the leases, then opens the runner's connection, resolves the universe and
    runs every window's pages — the new days fetched, the re-read windows parsed from L0.
    What it assumes: `should_stop` is the job's deadline; the runner checks it between units.
    What it never does: open its connection before the leases are held, so a host another driver
    holds refuses the run with nothing written. `transport` is a test seam: `None` is the network.
    Per-filing commits, exactly as a fetching campaign run.
    """
    calendar = trading_calendar()
    units = _units(plan)
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
            register=load_register(),
            l0=l0,
            transport=transport,
        ) as fetcher,
        connection(settings) as conn,
    ):
        master = IdentityStore(conn, clock=clock).load_master()
        universe = resolve_universe(
            master,
            isins_in_price_window(
                calendar,
                plan.through - timedelta(days=UNIVERSE_LOOKBACK_DAYS),
                plan.through,
                data_root=settings.data_root,
            ),
        )
        runner = FundamentalsBackfillRunner(
            fetcher=fetcher,
            l0=l0,
            sync=SyncStateStore(conn, clock=clock, calendar=calendar),
            universe=universe,
            commit=conn.commit,
            should_stop=should_stop,
            data_root=settings.data_root,
            symbol_history=lambda isin, on: symbols_accepted_on(
                master.windows_for(isin),
                on,
                isin=isin,
                owner_on=lambda symbol: master.try_resolve(symbol, on),
            ),
            resolve_isin=lambda symbol, on: try_resolve_unambiguous(master, symbol, on),
        )
        _LOG.info(
            "fundamentals_forward.plan_start",
            source=integrated.SOURCE_ID,
            new=None if plan.new is None else f"{plan.new.from_date}..{plan.new.to_date}",
            reread=[f"{w.from_date}..{w.to_date}" for w in plan.reread],
            pages=len(units),
            universe=len(universe),
            state="RUNNING",
        )
        return runner.run(units)


# ── the job ──────────────────────────────────────────────────────────────────────────────────

RunPlan = Callable[..., FundamentalsBackfillReport]


def run_fundamentals_forward(
    context: JobContext,
    *,
    read_rows: Callable[[Settings], Sequence[IndexPageRow]] = _read_rows,
    run_plan: RunPlan = run_forward_plan,
) -> FundamentalsBackfillReport | None:
    """One forward run: plan from the watermark to yesterday, run it, fail loud if it fell short.

    What it does: reads the index rows (read-only, no lease), plans the bounded new window plus
    the recent re-reads, runs them under both NSE leases with a deadline, and returns the runner's
    report — or `None` when no new day is owed, having taken no lease.
    What it assumes: the injected clock and settings are the run's (B10), the integrated campaign
    has run at least once, and the database is migrated.
    What it never does: catch `HostBusyError` (a refused lease is a FAILED run naming the holder,
    never a quiet skip), report a window that did not complete as a success, or fetch past
    yesterday.
    Raises `FundamentalsForwardError` when an index page failed, a 403 spike parked the run, or the
    deadline stopped it before the plan was through.
    """
    settings, clock = context.settings, context.clock
    started = clock.now()
    through = clock.today() - timedelta(days=1)
    plan = plan_forward(read_rows(settings), through=through)
    if plan is None:
        _LOG.info(
            "fundamentals_forward.nothing_owed",
            source=integrated.SOURCE_ID,
            through=through.isoformat(),
            state="PUBLISHED",
        )
        return None

    deadline = forward_deadline(started)
    stopped: list[datetime] = []

    def should_stop() -> bool:
        now = clock.now()
        if now < deadline:
            return False
        if not stopped:
            stopped.append(now)
            _LOG.warning(
                "fundamentals_forward.deadline",
                source=integrated.SOURCE_ID,
                deadline=deadline.isoformat(),
                state="STOPPING",
            )
        return True

    _LOG.info(
        "fundamentals_forward.planned",
        source=integrated.SOURCE_ID,
        watermark=plan.watermark.isoformat(),
        through=through.isoformat(),
        new=None if plan.new is None else f"{plan.new.from_date}..{plan.new.to_date}",
        new_days=plan.new_days,
        reread=len(plan.reread),
        behind_days=(through - plan.watermark).days,
        deadline=deadline.isoformat(),
    )
    report = run_plan(settings, clock, plan, should_stop=should_stop)
    if report.parked:
        state = "PARKED"
    elif report.index_failed or stopped:
        state = "DEGRADED"
    else:
        state = "PUBLISHED"
    _LOG.info(
        "fundamentals_forward.done",
        source=integrated.SOURCE_ID,
        index_published=report.index_published,
        index_reread=report.index_skipped_published,
        index_failed=report.index_failed,
        filings_discovered=report.filings_discovered,
        filings_published=report.filings_published,
        filings_skipped_published=report.filings_skipped_published,
        filings_failed=report.filings_failed,
        skipped_out_of_universe=report.skipped_out_of_universe,
        unresolved_symbols=sorted(report.unresolved_symbols),
        facts_written=report.facts_written,
        stopped_at_deadline=bool(stopped),
        state=state,
    )
    span = f"{plan.windows[0].from_date}..{plan.through}"
    if report.parked:
        raise FundamentalsForwardError(f"{span}: parked — {report.park_detail}")
    if report.index_failed:
        failed = "; ".join(f"{label}: {message}" for label, message in report.failures[:5])
        raise FundamentalsForwardError(
            f"{span}: {report.index_failed} index page(s) failed, so the window is not done and "
            f"the next run re-plans it — {failed}"
        )
    if stopped:
        raise FundamentalsForwardError(
            f"{span}: stopped at the {deadline:%H:%M} deadline before the plan was through; an "
            "unfinished new window holds the watermark and a finished one is re-read next run"
        )
    return report


def run_fundamentals_forward_job(context: JobContext) -> None:
    """Scheduler body for `fundamentals_forward`."""
    run_fundamentals_forward(context)
