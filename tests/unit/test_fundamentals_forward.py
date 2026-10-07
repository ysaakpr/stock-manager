"""M14.3: the fundamentals forward job — watermark, bounded window, and a lease conflict that shows.

Offline: no database and no socket. The planner and the watermark are pure; the job is driven with
its row reader and plan runner injected, except in the lease tests, which use the real plan runner
against a scratch lake, a transport that refuses every request and a database URL nothing listens
on — so a regression that stopped taking the lease fails loudly instead of reaching NSE or the
production Postgres.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.fetcher import FetchResponse
from dataplatform.ingest.fundamentals_backfill import FundamentalsBackfillReport, ParkReason
from dataplatform.ingest.fundamentals_forward import (
    MAX_NEW_DAYS,
    NIGHT_DEADLINE,
    REREAD_DAYS,
    ForwardPlan,
    FundamentalsForwardError,
    IndexPageRow,
    NoWatermarkError,
    Window,
    forward_deadline,
    integrated_watermark,
    plan_forward,
    run_forward_plan,
    run_fundamentals_forward,
)
from dataplatform.ingest.lease import HostBusyError, host_lease, read_lease
from dataplatform.scheduler import Job, JobContext, JobRegistry, JobState, SchedulerRunner
from dataplatform.scheduler.registry import FUNDAMENTALS_FORWARD, default_registry, lag_budgets
from dataplatform.status.sync_state import SyncState
from tests.conftest import SettingsLoader

#: Thursday 2026-10-08, 02:00 IST — the job's first fire after merge; yesterday is the 7th.
FIRE: Final = datetime(2026, 10, 8, 2, 0, tzinfo=IST)
PAGES: Final = 6


def _window(
    start: date,
    end: date,
    *,
    fetched_on: date | None = None,
    failed: Sequence[int] = (),
    missing: Sequence[int] = (),
) -> list[IndexPageRow]:
    """One planned window's page rows, `p01..p06`, fetched (by default) the day after it ends."""
    on = end + timedelta(days=1) if fetched_on is None else fetched_on
    return [
        IndexPageRow(
            from_date=start,
            to_date=end,
            page=page,
            state=SyncState.FAILED if page in failed else SyncState.PUBLISHED,
            fetched_on=on,
        )
        for page in range(1, PAGES + 1)
        if page not in missing
    ]


def _history() -> list[IndexPageRow]:
    """The lake's real shape on 2026-10-07: monthly windows, a re-run of September, then Oct 1-5."""
    rows: list[IndexPageRow] = []
    month = date(2025, 3, 1)
    while month < date(2026, 10, 1):
        following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        rows += _window(month, following - timedelta(days=1))
        month = following
    rows += _window(date(2026, 9, 1), date(2026, 9, 6))
    rows += _window(date(2026, 10, 1), date(2026, 10, 5))
    return rows


# ── rows and the watermark ──────────────────────────────────────────────────────────────────


def test_a_page_row_parses_the_unit_and_dates_the_fetch_in_ist() -> None:
    """20:00 UTC on the 6th is 01:30 IST on the 7th: the fetch date is the exchange's, not UTC."""
    updated = datetime.fromisoformat("2026-10-06T20:00:00+00:00")
    row = IndexPageRow.of(date(2026, 10, 1), "2026-10-05/p03", "PUBLISHED", updated, IST)
    assert row == IndexPageRow(
        date(2026, 10, 1), date(2026, 10, 5), 3, SyncState.PUBLISHED, date(2026, 10, 7)
    )


@pytest.mark.parametrize("unit", ["", "2026-10-05", "2026-10-05/03", "2026-10-05/pX", "p01"])
def test_a_unit_the_feed_never_writes_is_refused_not_guessed(unit: str) -> None:
    with pytest.raises(ValueError):
        IndexPageRow.of(date(2026, 10, 1), unit, "PUBLISHED", FIRE, IST)


def test_the_watermark_is_the_end_of_the_contiguous_done_run() -> None:
    assert integrated_watermark(_history()) == date(2026, 10, 5)


def test_a_window_with_a_failed_page_does_not_move_the_watermark() -> None:
    """Inverted, a failed index page would be skipped for good: the next window starts after it."""
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 7), failed=[2])
    assert integrated_watermark(rows) == date(2026, 10, 5)
    # The same window with every page published does move it — so the check above is not vacuous.
    complete = _history() + _window(date(2026, 10, 6), date(2026, 10, 7))
    assert integrated_watermark(complete) == date(2026, 10, 7)


def test_a_window_with_a_page_never_attempted_is_not_done() -> None:
    """A run stopped mid-window leaves later pages without rows; absent is not published."""
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 7), missing=[5, 6])
    assert integrated_watermark(rows) == date(2026, 10, 5)


def test_a_hole_holds_the_watermark_and_a_later_window_does_not_jump_it() -> None:
    """April 2025 never ran: everything after it is done, and the watermark stays at March."""
    rows = [row for row in _history() if row.from_date != date(2025, 4, 1)]
    assert integrated_watermark(rows) == date(2025, 3, 31)


def test_a_later_failure_replanned_under_a_new_key_is_superseded() -> None:
    """The failed Oct 6-7 window lingers in sync_state; the Oct 6-8 re-plan covers it."""
    rows = (
        _history()
        + _window(date(2026, 10, 6), date(2026, 10, 7), failed=[1])
        + _window(date(2026, 10, 6), date(2026, 10, 8))
    )
    assert integrated_watermark(rows) == date(2026, 10, 8)


def test_no_done_window_is_no_watermark() -> None:
    assert integrated_watermark([]) is None
    assert integrated_watermark(_window(date(2025, 3, 1), date(2025, 3, 31), failed=[1])) is None


def test_a_window_fetched_on_its_own_last_day_is_done_only_through_the_day_before() -> None:
    """B1: a 15:00 manual run `--to D` must not mark D done — D publishes into the night.

    Inverted (counting the fetch day), the 02:00 run on D+1 would plan nothing and every filing
    disseminated after 15:00 on D would be lost for good.
    """
    oct6, oct7, oct8 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)
    same_day = _history() + _window(oct6, oct7, fetched_on=oct7)
    assert integrated_watermark(same_day) == oct6
    next_day = _history() + _window(oct6, oct7, fetched_on=oct8)
    assert integrated_watermark(next_day) == oct7
    # Fetched on its first day: nothing in it is complete, so the watermark does not move at all.
    first_day = _history() + _window(oct6, oct7, fetched_on=oct6)
    assert integrated_watermark(first_day) == date(2026, 10, 5)


# ── planning ────────────────────────────────────────────────────────────────────────────────


def test_a_normal_night_owes_one_day_and_rereads_the_recent_windows() -> None:
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 6))
    plan = plan_forward(rows, through=date(2026, 10, 7))
    assert plan is not None
    assert plan.watermark == date(2026, 10, 6)
    assert plan.new == Window(date(2026, 10, 7), date(2026, 10, 7))
    assert plan.new_days == 1
    assert plan.reread == (
        Window(date(2026, 10, 1), date(2026, 10, 5)),
        Window(date(2026, 10, 6), date(2026, 10, 6)),
    )


def test_nothing_is_owed_once_the_watermark_reaches_yesterday() -> None:
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 7))
    assert integrated_watermark(rows) == date(2026, 10, 7)
    assert plan_forward(rows, through=date(2026, 10, 7)) is None


def test_a_same_day_window_is_replanned_live_the_next_night() -> None:
    """B1 end to end: a manual run fetched Oct 7..Oct 7 on the 7th; the 02:00 run on the 8th owes
    the 7th, and plans it under a key that window does not have — a page fetched live, never the
    frozen 15:00 payload re-parsed."""
    same_day = _window(date(2026, 10, 7), date(2026, 10, 7), fetched_on=date(2026, 10, 7))
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 6)) + same_day
    plan = plan_forward(rows, through=date(2026, 10, 7))
    assert plan is not None
    assert plan.watermark == date(2026, 10, 6)
    assert plan.new is not None
    assert plan.new.from_date <= date(2026, 10, 7) <= plan.new.to_date
    assert plan.new not in {row.window for row in rows}
    assert plan.new == Window(date(2026, 10, 6), date(2026, 10, 7))


def test_a_long_gap_is_caught_up_a_bounded_window_at_a_time() -> None:
    through = date(2026, 10, 20)
    plan = plan_forward(_history(), through=through)
    assert plan is not None and plan.new is not None
    assert plan.new == Window(date(2026, 10, 6), date(2026, 10, 5) + timedelta(days=MAX_NEW_DAYS))
    assert plan.new_days == MAX_NEW_DAYS
    assert plan.new.to_date < through


def test_no_window_reaches_past_through() -> None:
    for days_behind in range(1, 10):
        through = date(2026, 10, 5) + timedelta(days=days_behind)
        plan = plan_forward(_history(), through=through)
        assert plan is not None
        assert all(window.to_date <= through for window in plan.windows)


def test_a_retry_rereads_existing_keys_and_tonights_row_still_dates_tonight() -> None:
    """B2: a retry pending in last week's windows must not date tonight's index row a week back.

    The new window starts at watermark + 1 — the newest row `/status/sources` measures lag from —
    and every retry rides on a window that already exists, re-read from L0 with no new index row.
    Inverted (retry folded into the new window), the new window would start at the failure's date
    and the source would show overdue for the whole lookback.
    """
    rows = _history()
    for day in range(6, 13):
        rows += _window(date(2026, 10, day), date(2026, 10, day))
    through = date(2026, 10, 13)
    plan = plan_forward(rows, through=through)
    assert plan is not None and plan.new is not None
    assert plan.new.from_date == plan.watermark + timedelta(days=1) == date(2026, 10, 13)
    done = {row.window for row in rows}
    assert plan.reread and set(plan.reread) <= done
    assert len(plan.reread) == REREAD_DAYS - 1  # the 7th..12th; the 13th is tonight's new window
    assert Window(date(2026, 10, 1), date(2026, 10, 5)) not in plan.reread  # outside the week

    # The lag the status API will compute once tonight's window publishes, against its budget.
    as_of = through + timedelta(days=1)
    sessions = trading_calendar().expected_data_dates(plan.new.from_date + timedelta(days=1), as_of)
    assert len(sessions) <= lag_budgets(default_registry())["nse_integrated_filing_index"]


def test_no_watermark_refuses_to_start_the_store() -> None:
    with pytest.raises(NoWatermarkError, match="run the integrated campaign first"):
        plan_forward([], through=date(2026, 10, 7))


# ── the deadline ─────────────────────────────────────────────────────────────────────────────


def test_a_night_run_stops_starting_units_by_four_forty_five_whenever_it_started() -> None:
    """The 02:00 fire, and a late fire anywhere inside its one-hour misfire grace."""
    for minutes in range(0, 61, 5):
        start = FIRE + timedelta(minutes=minutes)
        deadline = forward_deadline(start)
        assert deadline.date() == start.date()
        assert deadline.time() == NIGHT_DEADLINE
    assert FIRE.replace(hour=5).time() > NIGHT_DEADLINE


def test_a_daytime_manual_run_stops_before_the_evening_window() -> None:
    deadline = forward_deadline(datetime(2026, 10, 8, 15, 0, tzinfo=IST))
    assert deadline == datetime(2026, 10, 8, 17, 45, tzinfo=IST)


# ── the job, with its rows and plan runner injected ──────────────────────────────────────────


def _context(settings: Settings, clock: FrozenClock | None = None) -> JobContext:
    return JobContext(
        job_name="fundamentals_forward",
        run_id=uuid4(),
        clock=FrozenClock(FIRE) if clock is None else clock,
        settings=settings,
    )


@pytest.fixture
def settings(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Settings:
    """Environment-only settings on a scratch lake; the repo `.env` is never read, and the database
    URL points at a port nothing listens on, so no test here can reach a real Postgres."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nowhere")
    return load_settings(None)


class _Rows:
    """A `read_rows` stand-in."""

    def __init__(self, rows: Sequence[IndexPageRow]) -> None:
        self.rows = list(rows)

    def __call__(self, settings: Settings) -> list[IndexPageRow]:
        return self.rows


class _Runner:
    """A `run_plan` stand-in recording the plan; `during` runs inside it, to move the clock."""

    def __init__(
        self,
        report: FundamentalsBackfillReport,
        during: Callable[[FrozenClock], None] | None = None,
    ) -> None:
        self.report = report
        self.plans: list[ForwardPlan] = []
        self.stops: list[bool] = []
        self.during = during

    def __call__(
        self,
        settings: Settings,
        clock: FrozenClock,
        plan: ForwardPlan,
        *,
        should_stop: Callable[[], bool],
    ) -> FundamentalsBackfillReport:
        self.plans.append(plan)
        self.stops.append(should_stop())
        if self.during is not None:
            self.during(clock)
            self.stops.append(should_stop())
        return self.report


def _clean() -> FundamentalsBackfillReport:
    return FundamentalsBackfillReport(index_requested=12, index_published=6)


def test_the_job_plans_from_the_watermark_to_yesterday_by_the_injected_clock(
    settings: Settings,
) -> None:
    runner = _Runner(_clean())
    report = run_fundamentals_forward(
        _context(settings), read_rows=_Rows(_history()), run_plan=runner
    )
    assert report is runner.report
    (plan,) = runner.plans
    assert plan.through == date(2026, 10, 7)
    assert plan.new == Window(date(2026, 10, 6), date(2026, 10, 7))
    assert runner.stops == [False]


def test_the_job_is_idempotent_when_nothing_is_owed(settings: Settings) -> None:
    runner = _Runner(_clean())
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 7))
    result = run_fundamentals_forward(_context(settings), read_rows=_Rows(rows), run_plan=runner)
    assert result is None
    assert runner.plans == []


def test_a_failed_index_page_fails_the_run(settings: Settings) -> None:
    report = FundamentalsBackfillReport(index_requested=6, index_published=5, index_failed=1)
    report.failures.append(("integrated index 2026-10-06..2026-10-07 page 2", "FetchHTTPError"))
    with pytest.raises(FundamentalsForwardError, match="1 index page"):
        run_fundamentals_forward(
            _context(settings), read_rows=_Rows(_history()), run_plan=_Runner(report)
        )


def test_a_parked_run_fails_the_run(settings: Settings) -> None:
    report = FundamentalsBackfillReport(
        index_requested=6,
        park_reason=ParkReason.FORBIDDEN_SPIKE,
        park_detail="FORBIDDEN_SPIKE: a 403 spike hard-stopped the fetch",
    )
    with pytest.raises(FundamentalsForwardError, match="parked"):
        run_fundamentals_forward(
            _context(settings), read_rows=_Rows(_history()), run_plan=_Runner(report)
        )


def test_a_filing_failure_alone_does_not_fail_the_run(settings: Settings) -> None:
    """It is FAILED on its own sync_state row (status API) and the week's re-reads retry it."""
    report = FundamentalsBackfillReport(
        index_requested=6, index_published=6, filings_published=40, filings_failed=1
    )
    result = run_fundamentals_forward(
        _context(settings), read_rows=_Rows(_history()), run_plan=_Runner(report)
    )
    assert result is report


def test_the_deadline_stops_the_runner_and_fails_the_run(settings: Settings) -> None:
    """N2: the runner's `should_stop` turns at 04:45 on the injected clock, and a stopped run is
    reported, not passed off as a finished one."""
    clock = FrozenClock(FIRE)
    runner = _Runner(_clean(), during=lambda c: c.freeze_at(FIRE.replace(hour=4, minute=45)))
    with pytest.raises(FundamentalsForwardError, match="04:45 deadline"):
        run_fundamentals_forward(
            _context(settings, clock), read_rows=_Rows(_history()), run_plan=runner
        )
    assert runner.stops == [False, True]


def test_a_run_that_finishes_before_the_deadline_is_not_stopped(settings: Settings) -> None:
    clock = FrozenClock(FIRE)
    runner = _Runner(_clean(), during=lambda c: c.freeze_at(FIRE.replace(hour=4, minute=44)))
    result = run_fundamentals_forward(
        _context(settings, clock), read_rows=_Rows(_history()), run_plan=runner
    )
    assert result is runner.report
    assert runner.stops == [False, False]


# ── a lease conflict surfaces ────────────────────────────────────────────────────────────────


class _NoNetwork:
    """A transport that fails the test on any request: a regression must not reach NSE."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        payload: bytes | None = None,
    ) -> FetchResponse:
        pytest.fail(f"the forward job made a request while another driver held the lease: {url}")


_OFFLINE: Final = partial(run_forward_plan, transport=_NoNetwork())


@pytest.mark.parametrize("host", ["www.nseindia.com", "nsearchives.nseindia.com"])
def test_a_held_nse_lease_raises_and_is_not_swallowed(
    settings: Settings, tmp_path: Path, host: str
) -> None:
    """The real plan runner, stopped at the lease: it raises naming the holder, before any DB."""
    clock = FrozenClock(FIRE)
    with host_lease(host, clock=clock, command="integrated campaign", data_root=tmp_path) as held:
        with pytest.raises(HostBusyError, match="integrated campaign") as raised:
            run_fundamentals_forward(
                _context(settings, clock), read_rows=_Rows(_history()), run_plan=_OFFLINE
            )
        assert raised.value.holder.pid == held.pid
        # The campaign's lease is untouched: refused, not broken and not queued behind.
        assert read_lease(host, data_root=tmp_path) == held


def test_nothing_owed_takes_no_lease_and_so_cannot_conflict(
    settings: Settings, tmp_path: Path
) -> None:
    clock = FrozenClock(FIRE)
    rows = _history() + _window(date(2026, 10, 6), date(2026, 10, 7))
    with host_lease("www.nseindia.com", clock=clock, command="campaign", data_root=tmp_path):
        result = run_fundamentals_forward(
            _context(settings, clock), read_rows=_Rows(rows), run_plan=_OFFLINE
        )
    assert result is None


def _forward(context: JobContext, rows: _Rows) -> None:
    run_fundamentals_forward(context, read_rows=rows, run_plan=_OFFLINE)


def test_the_scheduler_records_a_lease_conflict_as_failed_naming_the_holder(
    settings: Settings, tmp_path: Path
) -> None:
    """`job_run` gets FAILED with the holder in `error` — what `/status/jobs` and the alert read.

    Not SUCCEEDED (a swallowed conflict) and not SKIPPED_LOCKED (which `assess` judges by the
    success behind it). `_execute` is the runner's whole outcome mapping; it opens no connection.
    """
    clock = FrozenClock(FIRE)
    rows = _Rows(_history())
    job = Job(
        name=FUNDAMENTALS_FORWARD.name,
        cron=FUNDAMENTALS_FORWARD.cron,
        fn=lambda ctx: _forward(ctx, rows),
        timeout=FUNDAMENTALS_FORWARD.timeout,
        description=FUNDAMENTALS_FORWARD.description,
    )
    runner = SchedulerRunner(JobRegistry([job]), settings=settings, clock=clock, instance="test")
    with host_lease(
        "nsearchives.nseindia.com", clock=clock, command="delivery", data_root=tmp_path
    ):
        state, error, retry_pending = runner._execute(job, uuid4())
    assert state is JobState.FAILED
    assert error is not None and error.startswith("HostBusyError:")
    assert "delivery" in error and "nsearchives.nseindia.com" in error
    assert retry_pending is False
