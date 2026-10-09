"""D1 (M17.10): same-evening index levels and India VIX → `macro_series`.

The M17 desk's Commons market sheet, regime read, screens and sector returns all read
`IN.NSE.<INDEX>.{CLOSE,PE,PB,DIV_YIELD}` and `IN.NSE.INDIA_VIX.CLOSE` for the session they decide.
Until this job nothing landed those the same evening: the close-all snapshot was a one-shot M11.2
campaign and India VIX only came with the weekly Sunday capture, so on 2026-10-09 every series
stopped at 2026-10-05 and the M17 job's bounded data wait timed out every night.

Two steps, each under its own host lease, both reusing the existing ingest code rather than a
second parser:

* **close-all** — `ind_close_all_<DDMMYYYY>.csv` from the NSE archive host, driven through the
  M11.2 runner (`backfill.MacroBackfillRunner`) over the trailing `CATCHUP_DAYS` of expected data
  dates. The runner's resume is `sync_state`, so a session already `PUBLISHED` costs nothing and a
  missed evening is picked up by the next run. The archive is one dated file per session — it has
  no ranged form — so a catch-up costs one request per *missed* session and none for the rest, all
  inside one run under one lease (never a loop of single-date invocations). A 404 for the session
  owed tonight is "not yet published", left retryable (`pending_from`), never a closed gap.
* **India VIX** — NSE Indices' historical endpoint (`india_vix.parse_india_vix_history`), one
  ranged POST from the day after the newest session that source has in the store to tonight's
  session. A week of missed evenings is one request, not five. The close also arrives through the
  close-all file's "India VIX" row under the same `series_id`; the two writes have distinct fact
  keys (this source states `period_start`), and the M17 sheet reads one level per session.

Point-in-time: both sources disseminate a session's level at that session's close, so every fact
is `period_end = release_date = the session` — a level for D is knowable from D's evening and from
no earlier date. Neither step ever dates a fact by the fetch, and neither writes a session after
the one the run owes.

A step that cannot land tonight's session raises once every step has run, so the run is FAILED on
`/status/jobs` and the next fire retries; a fire after both landed makes no request.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.indices import tri_request_body
from dataplatform.ingest.macro.backfill import (
    ARCHIVE_EPOCH,
    MacroBackfillRunner,
    SyncStore,
    build_plan,
)
from dataplatform.ingest.macro.backfill import SOURCE_ID as CLOSE_ALL_SOURCE_ID
from dataplatform.ingest.macro.index_valuation import IndexAliasTable
from dataplatform.ingest.macro.india_vix import (
    INDIA_VIX_REQUEST_NAME,
    INDIA_VIX_SOURCE_ID,
    INDIA_VIX_URL,
    parse_india_vix_history,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tri_backfill import latest_session_through
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store
from dataplatform.store.macro_series import read_l1, write_release

if TYPE_CHECKING:
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "CATCHUP_DAYS",
    "CLOSE_ALL_HOST",
    "CLOSE_ALL_SOURCE_ID",
    "EVENING_SOURCES",
    "INDIA_VIX_HOST",
    "IndexEveningError",
    "IndexLevelsNotYetPublishedError",
    "StepOutcome",
    "close_all_evening",
    "india_vix_evening",
    "india_vix_evening_filename",
    "main",
    "run_index_close_evening",
    "run_index_close_evening_job",
]

_LOG = get_logger(__name__)

#: How far back (calendar days) each evening run looks for a session it has not landed. Two weeks
#: covers a missed week plus a holiday cluster; anything older is the M11.2 campaign's to redo.
CATCHUP_DAYS: Final = 14

#: The host each step leases — the register rows' own hosts (`test_scheduler_coverage` derives the
#: overlap check from them).
CLOSE_ALL_HOST: Final = "nsearchives.nseindia.com"
INDIA_VIX_HOST: Final = "niftyindices.com"

#: The register rows this job keeps current, in run order (the job's `covers`).
EVENING_SOURCES: Final[tuple[str, ...]] = (CLOSE_ALL_SOURCE_ID, INDIA_VIX_SOURCE_ID)


class IndexLevelsNotYetPublishedError(RuntimeError):
    """A step ran, but the source does not carry the owed session yet; the next fire retries."""


class IndexEveningError(RuntimeError):
    """One or more evening steps failed; the message names each and why."""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What one step did for the owed session ``through``."""

    source: str
    through: date
    requests: int
    sessions_landed: tuple[date, ...]
    latest: date | None
    note: str = ""

    @property
    def line(self) -> str:
        """One human-readable line for the CLI and the job log."""
        landed = (
            f"{self.sessions_landed[0]}..{self.sessions_landed[-1]}"
            if self.sessions_landed
            else "none"
        )
        extra = f" — {self.note}" if self.note else ""
        return (
            f"{self.source}: owed {self.through}, {self.requests} request(s), landed {landed}, "
            f"latest {self.latest or 'none'}{extra}"
        )


# ── close-all (NSE archive host) ─────────────────────────────────────────────────────────────


def close_all_evening(
    *,
    fetcher: Fetcher | None,
    l0: L0Store,
    sync: SyncStore,
    commit: Callable[[], None],
    rollback: Callable[[], None] = lambda: None,
    through: date,
    calendar: TradingCalendar,
    register: SourceRegister,
    since: date | None = None,
    data_root: Path | None = None,
    table: IndexAliasTable | None = None,
) -> StepOutcome:
    """Land every close-all session in ``[since, through]`` (``since``: ``through - CATCHUP_DAYS``).

    What it does: plans the window's expected data dates and hands them to the M11.2 runner, which
    skips each session `sync_state` already closed, takes a payload already in L0 without a
    request, fetches the rest, and writes each session's facts to its own `release_date`
    partition. A 404 for ``through`` is left retryable (`pending_from`).
    What it assumes: the caller holds `CLOSE_ALL_HOST`'s lease and `commit` commits `sync`.
    What it never does: request a session after ``through``, or report success when ``through`` is
    not `PUBLISHED` — that raises `IndexLevelsNotYetPublishedError` once the window is walked, so
    the missed sessions still land on a night the latest one is late.
    """
    default_start = through - timedelta(days=CATCHUP_DAYS)
    start = max(ARCHIVE_EPOCH, since if since is not None else default_start)
    plan = build_plan(start, through, calendar=calendar, register=register)
    runner = MacroBackfillRunner(
        fetcher=fetcher,
        l0=l0,
        sync=sync,
        commit=commit,
        rollback=rollback,
        data_root=data_root,
        table=table,
        pending_from=through,
    )
    report = runner.run(plan)
    if report.parked:
        raise IndexEveningError(report.park_detail or "close-all fetch parked")
    landed = tuple(
        unit.session
        for unit in plan
        if getattr(sync.get(CLOSE_ALL_SOURCE_ID, unit.session), "state", None)
        is SyncState.PUBLISHED
    )
    outcome = StepOutcome(
        source=CLOSE_ALL_SOURCE_ID,
        through=through,
        requests=report.requests,
        sessions_landed=landed,
        latest=landed[-1] if landed else None,
        note=(
            f"{report.published} published now, {report.resumed} already, "
            f"{report.not_published} not published (404), {report.failed} failed, "
            f"{report.refused} refused"
        ),
    )
    if through not in landed:
        detail = "; ".join(f"{label}: {why}" for label, why in report.failures) or "no row"
        raise IndexLevelsNotYetPublishedError(
            f"{CLOSE_ALL_SOURCE_ID}: the close-all file for {through} is not published yet "
            f"({detail})"
        )
    return outcome


# ── India VIX (niftyindices.com) ─────────────────────────────────────────────────────────────


def india_vix_evening_filename(start: date, end: date, attempt_at: datetime) -> str:
    """L0 filename for one evening window, stamped with the attempt instant (IST).

    The weekly capture's name (`india_vix_filename`) is one key per window; an evening fire that
    finds tonight's session missing must be able to ask again an hour later without colliding with
    the first payload, which stays in L0 as the record of what the endpoint said then.
    """
    stamp = attempt_at.astimezone(IST)
    return f"india_vix_{start:%Y%m%d}_{end:%Y%m%d}_at{stamp:%Y%m%dT%H%M%S}.json"


def _latest_vix_session(
    through: date, *, lookback_days: int, data_root: Path | None
) -> date | None:
    """The newest session ≤ ``through`` this source already wrote, within ``lookback_days``.

    Read per `release_date` partition (one small file each), newest first, and only this source's
    facts: the close-all file's "India VIX" row shares the series id but is not this source's
    OHLC, so it must not make this step skip.
    """
    for offset in range(lookback_days + 1):
        day = through - timedelta(days=offset)
        try:
            facts = read_l1(day, data_root=data_root)
        except FileNotFoundError:
            continue
        if any(f.source == INDIA_VIX_SOURCE_ID and f.period_end <= through for f in facts):
            return day
    return None


def india_vix_evening(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    through: date,
    attempt_at: datetime,
    since: date | None = None,
    data_root: Path | None = None,
    lookback_days: int = CATCHUP_DAYS * 2,
) -> StepOutcome:
    """India VIX spot OHLC from the first session this source lacks through ``through``: one POST.

    What it does: finds the newest session the store holds from this source; when it is already
    ``through`` it returns without a request. Otherwise one ranged POST over
    ``[that session + 1, through]`` (or ``[since, through]``, or the trailing ``lookback_days`` for
    an empty store), filed in L0 under ``through`` with the attempt instant in the name, and one
    `macro_series` release per session in the window.
    What it assumes: the caller holds `INDIA_VIX_HOST`'s lease.
    What it never does: write a row outside the requested window (the endpoint's rows are clipped
    to it), date a level by the fetch, or report success when the payload does not reach
    ``through`` — the sessions it does carry are written, then
    `IndexLevelsNotYetPublishedError` is raised.
    """
    last = _latest_vix_session(through, lookback_days=lookback_days, data_root=data_root)
    if since is None and last is not None and last >= through:
        _LOG.info(
            "index_evening.vix_skipped",
            source=INDIA_VIX_SOURCE_ID,
            session=through.isoformat(),
            reason="the store already holds this session from this source",
            state="PUBLISHED",
        )
        return StepOutcome(INDIA_VIX_SOURCE_ID, through, 0, (), last, note="already landed")
    if since is not None:
        start = since
    elif last is not None:
        start = last + timedelta(days=1)
    else:
        start = through - timedelta(days=lookback_days)
    filename = india_vix_evening_filename(start, through, attempt_at)
    ref = fetcher.fetch(
        INDIA_VIX_SOURCE_ID,
        INDIA_VIX_URL,
        through,
        filename=filename,
        payload=tri_request_body(INDIA_VIX_REQUEST_NAME, start, through),
    )
    releases = tuple(
        release
        for release in parse_india_vix_history(l0.get(ref), filename=filename, l0_key=ref.key)
        if start <= release.release_date <= through
    )
    for release in releases:
        write_release(release, data_root=data_root)
    sessions = tuple(release.release_date for release in releases)
    latest = max((s for s in (*sessions, last) if s is not None), default=None)
    outcome = StepOutcome(
        INDIA_VIX_SOURCE_ID, through, 1, sessions, latest, note=f"window {start}..{through}"
    )
    if latest is None or latest < through:
        raise IndexLevelsNotYetPublishedError(
            f"{INDIA_VIX_SOURCE_ID}: the endpoint does not carry {through} yet (window "
            f"{start}..{through} returned {len(sessions)} session(s); payload kept as {ref.key})"
        )
    return outcome


# ── the job ──────────────────────────────────────────────────────────────────────────────────


def run_index_close_evening(
    *,
    settings: Settings,
    clock: Clock,
    through: date | None = None,
    since: date | None = None,
    command: str = "index_close_evening",
) -> tuple[StepOutcome, ...]:
    """Both steps for the session owed on ``clock.today()`` (or ``through``), each under its lease.

    One step failing is logged and the other still runs; then `IndexEveningError` is raised naming
    every failure, so the run is FAILED on the status surface rather than in a log line. A weekday
    holiday owes the previous session, which the previous evening landed: a no-op.
    """
    calendar = trading_calendar()
    owed = through or latest_session_through(clock.today(), calendar)
    if owed is None:
        raise IndexEveningError(f"no trading session on or before {clock.today()} in coverage")
    register = load_register()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    attempt_at = clock.now()

    def close_all() -> StepOutcome:
        with (
            connection(settings) as conn,
            leased_fetcher(
                [CLOSE_ALL_HOST],
                clock=clock,
                command=command,
                settings=settings,
                register=register,
            ) as fetcher,
        ):
            sync = SyncStateStore(conn, clock=clock, calendar=calendar)
            return close_all_evening(
                fetcher=fetcher,
                l0=l0,
                sync=sync,
                commit=conn.commit,
                rollback=conn.rollback,
                through=owed,
                calendar=calendar,
                register=register,
                since=since,
                data_root=settings.data_root,
            )

    def vix() -> StepOutcome:
        with leased_fetcher(
            [INDIA_VIX_HOST], clock=clock, command=command, settings=settings, register=register
        ) as fetcher:
            return india_vix_evening(
                fetcher=fetcher,
                l0=l0,
                through=owed,
                attempt_at=attempt_at,
                since=since,
                data_root=settings.data_root,
            )

    outcomes: list[StepOutcome] = []
    failures: list[str] = []
    for source, step in ((CLOSE_ALL_SOURCE_ID, close_all), (INDIA_VIX_SOURCE_ID, vix)):
        try:
            outcome = step()
        except Exception as error:  # one source failing must not stop the other; re-raised below
            _LOG.error(
                "index_evening.step_failed",
                source=source,
                date=owed.isoformat(),
                error=f"{type(error).__name__}: {error}",
                state="FAILED",
            )
            failures.append(f"{source}: {type(error).__name__}: {error}")
            continue
        if outcome.requests:
            # The instant a fire first found the session up — read after a few weeks to tune the
            # first fire against the source's real publication time.
            _LOG.info(
                "index_evening.first_landed",
                source=source,
                session=owed.isoformat(),
                landed_at_ist=attempt_at.astimezone(IST).isoformat(),
                requests=outcome.requests,
                state="PUBLISHED",
            )
        _LOG.info("index_evening.step_done", source=source, line=outcome.line, state="PUBLISHED")
        outcomes.append(outcome)
    if failures:
        raise IndexEveningError(
            f"{len(failures)} of 2 index-evening step(s) failed for {owed}: " + "; ".join(failures)
        )
    return tuple(outcomes)


def run_index_close_evening_job(context: JobContext) -> None:
    """The scheduler's `index_close_evening` job body (see the module note)."""
    run_index_close_evening(settings=context.settings, clock=context.clock)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.ingest.macro.index_evening [--since D] [--through D]`.

    The job's own run, by hand: one process, one lease per host, the close-all sessions the store
    lacks in ``[since, through]`` and one VIX POST over the same window. Exit 0 when both landed
    ``through``, 1 otherwise (the message names each failure — a host leased elsewhere included).
    """
    parser = argparse.ArgumentParser(prog="index-evening", description=main.__doc__)
    parser.add_argument("--since", type=date.fromisoformat, default=None)
    parser.add_argument("--through", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    try:
        outcomes = run_index_close_evening(
            settings=get_settings(),
            clock=SystemClock(),
            through=args.through,
            since=args.since,
            command="dataplatform.ingest.macro.index_evening",
        )
    except IndexEveningError as error:
        print(str(error), file=sys.stderr)
        return 1
    for outcome in outcomes:
        print(outcome.line)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
