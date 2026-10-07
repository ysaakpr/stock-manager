"""M14.3: the fundamentals forward job — watermark, bounded window, and a lease conflict that shows.

Offline: no database and no socket. The planner and the watermark are pure; the job is driven with
its state reader and window runner injected, except in the lease tests, which use the real window
runner against a scratch lake and stop it at the lease — before it would open a connection.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from dataplatform.clock import IST, Clock, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.fundamentals_backfill import FundamentalsBackfillReport, ParkReason
from dataplatform.ingest.fundamentals_forward import (
    MAX_NEW_DAYS,
    ForwardState,
    ForwardWindow,
    FundamentalsForwardError,
    IndexPageRow,
    NoWatermarkError,
    integrated_watermark,
    plan_forward_window,
    run_fundamentals_forward,
)
from dataplatform.ingest.lease import HostBusyError, host_lease, read_lease
from dataplatform.scheduler import Job, JobContext, JobRegistry, JobState, SchedulerRunner
from dataplatform.scheduler.registry import FUNDAMENTALS_FORWARD
from dataplatform.status.sync_state import SyncState
from tests.conftest import SettingsLoader

#: Thursday 2026-10-08, 02:00 IST — the job's first fire after merge; yesterday is the 7th.
FIRE: Final = datetime(2026, 10, 8, 2, 0, tzinfo=IST)
PAGES: Final = 6


def _window(
    start: date, end: date, *, failed: Iterable[int] = (), missing: Iterable[int] = ()
) -> list[IndexPageRow]:
    """One planned window's page rows, `p01..p06`, as the runner writes them."""
    skip, bad = set(missing), set(failed)
    return [
        IndexPageRow(
            from_date=start,
            to_date=end,
            page=page,
            state=SyncState.FAILED if page in bad else SyncState.PUBLISHED,
        )
        for page in range(1, PAGES + 1)
        if page not in skip
    ]


def _history() -> list[IndexPageRow]:
    """The lake's real shape on 2026-10-07: monthly windows, a re-run of September, then Oct 1-5."""
    rows: list[IndexPageRow] = []
    rows += _window(date(2025, 3, 1), date(2025, 3, 31))
    rows += _window(date(2025, 4, 1), date(2025, 4, 30))
    rows += _window(date(2026, 9, 1), date(2026, 9, 6))
    rows += _window(date(2026, 9, 1), date(2026, 9, 30))
    rows += _window(date(2026, 10, 1), date(2026, 10, 5))
    return rows


def _contiguous_history() -> list[IndexPageRow]:
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


def test_a_page_row_parses_the_unit_the_runner_writes() -> None:
    row = IndexPageRow.of(date(2026, 10, 1), "2026-10-05/p03", "PUBLISHED")
    assert row == IndexPageRow(date(2026, 10, 1), date(2026, 10, 5), 3, SyncState.PUBLISHED)


@pytest.mark.parametrize("unit", ["", "2026-10-05", "2026-10-05/03", "2026-10-05/pX", "p01"])
def test_a_unit_the_feed_never_writes_is_refused_not_guessed(unit: str) -> None:
    with pytest.raises(ValueError):
        IndexPageRow.of(date(2026, 10, 1), unit, "PUBLISHED")


def test_the_watermark_is_the_end_of_the_contiguous_done_run() -> None:
    assert integrated_watermark(_contiguous_history()) == date(2026, 10, 5)


def test_a_window_with_a_failed_page_does_not_move_the_watermark() -> None:
    """Inverted, a failed index page would be skipped for good: the next window starts after it."""
    rows = _contiguous_history() + _window(date(2026, 10, 6), date(2026, 10, 7), failed=[2])
    assert integrated_watermark(rows) == date(2026, 10, 5)
    # The same window with every page published does move it — so the check above is not vacuous.
    rows = _contiguous_history() + _window(date(2026, 10, 6), date(2026, 10, 7))
    assert integrated_watermark(rows) == date(2026, 10, 7)


def test_a_window_with_a_page_never_attempted_is_not_done() -> None:
    """A run stopped mid-window leaves later pages without rows; absent is not published."""
    rows = _contiguous_history() + _window(date(2026, 10, 6), date(2026, 10, 7), missing=[5, 6])
    assert integrated_watermark(rows) == date(2026, 10, 5)


def test_a_hole_holds_the_watermark_and_a_later_window_does_not_jump_it() -> None:
    """April 2025 never ran: everything after it is done, and the watermark stays at March."""
    rows = [row for row in _history() if row.from_date != date(2025, 4, 1)]
    assert integrated_watermark(rows) == date(2025, 3, 31)


def test_a_later_failure_replanned_under_a_new_key_is_superseded() -> None:
    """The failed Oct 6-7 window lingers in sync_state; the Oct 6-8 re-plan covers it."""
    rows = (
        _contiguous_history()
        + _window(date(2026, 10, 6), date(2026, 10, 7), failed=[1])
        + _window(date(2026, 10, 6), date(2026, 10, 8))
    )
    assert integrated_watermark(rows) == date(2026, 10, 8)


def test_no_done_window_is_no_watermark() -> None:
    assert integrated_watermark([]) is None
    assert integrated_watermark(_window(date(2025, 3, 1), date(2025, 3, 31), failed=[1])) is None


# ── planning ────────────────────────────────────────────────────────────────────────────────


def test_a_normal_night_owes_one_day() -> None:
    window = plan_forward_window(ForwardState(date(2026, 10, 6)), through=date(2026, 10, 7))
    assert window == ForwardWindow(date(2026, 10, 7), date(2026, 10, 7), date(2026, 10, 6), None)
    assert window.new_days == 1


def test_nothing_is_owed_once_the_watermark_reaches_yesterday() -> None:
    assert plan_forward_window(ForwardState(date(2026, 10, 7)), through=date(2026, 10, 7)) is None
    # A watermark past `through` (a manual campaign run to today) is not a reason to fetch.
    assert plan_forward_window(ForwardState(date(2026, 10, 8)), through=date(2026, 10, 7)) is None


def test_a_long_gap_is_caught_up_a_bounded_window_at_a_time() -> None:
    through = date(2026, 10, 20)
    window = plan_forward_window(ForwardState(date(2026, 10, 5)), through=through)
    assert window is not None
    assert window.from_date == date(2026, 10, 6)
    assert window.to_date == date(2026, 10, 5) + timedelta(days=MAX_NEW_DAYS)
    assert window.new_days == MAX_NEW_DAYS
    assert window.to_date < through


def test_the_window_never_reaches_past_through() -> None:
    for days_behind in range(1, 10):
        through = date(2026, 10, 5) + timedelta(days=days_behind)
        window = plan_forward_window(ForwardState(date(2026, 10, 5)), through=through)
        assert window is not None and window.to_date <= through


def test_a_recent_retryable_failure_pulls_the_window_back_over_it() -> None:
    state = ForwardState(watermark=date(2026, 10, 6), retry_from=date(2026, 10, 3))
    window = plan_forward_window(state, through=date(2026, 10, 7))
    assert window is not None
    assert (window.from_date, window.to_date) == (date(2026, 10, 3), date(2026, 10, 7))


def test_a_retry_alone_still_plans_a_window_when_no_new_day_is_owed() -> None:
    state = ForwardState(watermark=date(2026, 10, 7), retry_from=date(2026, 10, 4))
    window = plan_forward_window(state, through=date(2026, 10, 7))
    assert window is not None
    assert (window.from_date, window.to_date) == (date(2026, 10, 4), date(2026, 10, 7))
    assert window.new_days == 0


def test_no_watermark_refuses_to_start_the_store() -> None:
    with pytest.raises(NoWatermarkError, match="run the integrated campaign first"):
        plan_forward_window(ForwardState(None), through=date(2026, 10, 7))


# ── the job, with its state and window runner injected ───────────────────────────────────────


def _context(settings: Settings, clock: Clock | None = None) -> JobContext:
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
    """Environment-only settings on a scratch lake; the repo `.env` is never read."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return load_settings(None)


class _Recorder:
    def __init__(self, report: FundamentalsBackfillReport) -> None:
        self.report = report
        self.windows: list[ForwardWindow] = []

    def __call__(
        self, settings: Settings, clock: Clock, window: ForwardWindow
    ) -> FundamentalsBackfillReport:
        self.windows.append(window)
        return self.report


class _State:
    """A `read_state` stand-in that records the `through` it was asked for."""

    def __init__(self, watermark: date | None, retry_from: date | None = None) -> None:
        self.state = ForwardState(watermark=watermark, retry_from=retry_from)
        self.seen: list[date] = []

    def __call__(self, settings: Settings, through: date) -> ForwardState:
        self.seen.append(through)
        return self.state


def test_the_job_plans_from_the_watermark_to_yesterday_by_the_injected_clock(
    settings: Settings,
) -> None:
    read = _State(date(2026, 10, 5))
    runner = _Recorder(FundamentalsBackfillReport(index_requested=6, index_published=6))
    report = run_fundamentals_forward(_context(settings), read_state=read, run_window=runner)
    assert report is runner.report
    assert read.seen == [date(2026, 10, 7)]
    assert [(w.from_date, w.to_date) for w in runner.windows] == [
        (date(2026, 10, 6), date(2026, 10, 7))
    ]


def test_the_job_is_idempotent_when_nothing_is_owed(settings: Settings) -> None:
    runner = _Recorder(FundamentalsBackfillReport(index_requested=0))
    result = run_fundamentals_forward(
        _context(settings), read_state=_State(date(2026, 10, 7)), run_window=runner
    )
    assert result is None
    assert runner.windows == []


def test_a_failed_index_page_fails_the_run(settings: Settings) -> None:
    report = FundamentalsBackfillReport(index_requested=6, index_published=5, index_failed=1)
    report.failures.append(
        ("integrated index 2026-10-06..2026-10-07 page 2", "FetchHTTPError: 503")
    )
    with pytest.raises(FundamentalsForwardError, match="1 index page"):
        run_fundamentals_forward(
            _context(settings), read_state=_State(date(2026, 10, 5)), run_window=_Recorder(report)
        )


def test_a_parked_run_fails_the_run(settings: Settings) -> None:
    report = FundamentalsBackfillReport(
        index_requested=6,
        park_reason=ParkReason.FORBIDDEN_SPIKE,
        park_detail="FORBIDDEN_SPIKE: a 403 spike hard-stopped the fetch",
    )
    with pytest.raises(FundamentalsForwardError, match="parked"):
        run_fundamentals_forward(
            _context(settings), read_state=_State(date(2026, 10, 5)), run_window=_Recorder(report)
        )


def test_a_filing_failure_alone_does_not_fail_the_run(settings: Settings) -> None:
    """It is FAILED on its own sync_state row (status API) and the retry lookback re-plans it."""
    report = FundamentalsBackfillReport(
        index_requested=6, index_published=6, filings_published=40, filings_failed=1
    )
    result = run_fundamentals_forward(
        _context(settings), read_state=_State(date(2026, 10, 5)), run_window=_Recorder(report)
    )
    assert result is report


# ── a lease conflict surfaces ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["www.nseindia.com", "nsearchives.nseindia.com"])
def test_a_held_nse_lease_raises_and_is_not_swallowed(
    settings: Settings, tmp_path: Path, host: str
) -> None:
    """The real window runner, stopped at the lease: it raises naming the holder, before any DB."""
    clock = FrozenClock(FIRE)
    with host_lease(host, clock=clock, command="integrated campaign", data_root=tmp_path) as held:
        with pytest.raises(HostBusyError, match="integrated campaign") as raised:
            run_fundamentals_forward(
                _context(settings, clock), read_state=_State(date(2026, 10, 5))
            )
        assert raised.value.holder.pid == held.pid
        # The campaign's lease is untouched: refused, not broken and not queued behind.
        assert read_lease(host, data_root=tmp_path) == held


def test_nothing_owed_takes_no_lease_and_so_cannot_conflict(
    settings: Settings, tmp_path: Path
) -> None:
    clock = FrozenClock(FIRE)
    with host_lease("www.nseindia.com", clock=clock, command="campaign", data_root=tmp_path):
        assert (
            run_fundamentals_forward(
                _context(settings, clock), read_state=_State(date(2026, 10, 7))
            )
            is None
        )


def _forward(context: JobContext, read: _State) -> None:
    run_fundamentals_forward(context, read_state=read)


def test_the_scheduler_records_a_lease_conflict_as_failed_naming_the_holder(
    settings: Settings, tmp_path: Path
) -> None:
    """`job_run` gets FAILED with the holder in `error` — what `/status/jobs` and the alert read.

    Not SUCCEEDED (a swallowed conflict) and not SKIPPED_LOCKED (which `assess` judges by the
    success behind it). `_execute` is the runner's whole outcome mapping; it opens no connection.
    """
    clock = FrozenClock(FIRE)
    read = _State(date(2026, 10, 5))
    job = Job(
        name=FUNDAMENTALS_FORWARD.name,
        cron=FUNDAMENTALS_FORWARD.cron,
        fn=lambda ctx: _forward(ctx, read),
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
